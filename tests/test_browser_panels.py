"""Every dashboard panel, in a real browser, through a whole trading day.

The unit tests prove the pieces; this proves the page. It starts the real
app server, opens the dashboard in Chromium, and drives a session through
the API — arm, trade, exit, a burst of events, the reviews, the rules
pop-up, a market switch — asserting after each step that every panel
updated LIVE, with no reload. A panel that only refreshes on a timer, a
socket that silently stops, a stale summary from another market: each has
happened on the desk, and each is a failure here.

Only the analysts' votes and the quote are scripted, so each step is
deterministic; the pipeline, risk, broker, outcome tracker, journal, event
bus, websocket and page are all real. Skipped when no Chromium is available.
"""
from __future__ import annotations

import asyncio
import glob
import os
import socket
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

ET = ZoneInfo("America/New_York")
# The US session, open — on TODAY's date. Trades are stamped with the real
# date, and a fixed date here made "today's" record empty from the next day
# on (these tests passed on 28 Sept and failed from 29 Sept, 00:00 ET).
MARKET_TIME = datetime.now(ET).replace(hour=10, minute=30, second=0, microsecond=0)

VOTES: dict[str, dict[str, tuple[float, float]]] = {}
PRICES: dict[str, float] = {}


def _chromium() -> str | None:
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return os.getenv("PLAYWRIGHT_CHROMIUM") or (found[-1] if found else None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """The real FastAPI app on a real port, in its own thread and loop."""

    def __init__(self, app, port: int) -> None:
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.server.serve())

    def start(self) -> None:
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(60)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)


# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def desk():
    if not _chromium() and not os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
        pytest.skip("no Chromium available for browser tests")

    mp = pytest.MonkeyPatch()
    from app.agents import base as base_mod
    from app.brokers import paper as paper_mod
    from app.core import clock
    from app.core.config import get_config
    from app.core.models import AgentReport, Bias, Quote

    mp.setenv("AUTOSTART_ENGINE", "false")
    mp.setattr(clock, "market_now", lambda tz: MARKET_TIME.astimezone(ZoneInfo(tz)))

    cfg = get_config()
    cfg.reload()
    # These panels are about the desk trading named symbols; the screener
    # has its own tests (tests/test_screener.py).
    for settings in (cfg._base_settings, cfg.settings):
        settings.setdefault("screener", {})["enabled"] = False
    cfg.switch_market("US")
    # Every day is a session here, so the suite also runs at the weekend.
    cfg.settings.setdefault("system", {})["trading_days"] = [0, 1, 2, 3, 4, 5, 6]
    cfg.settings.setdefault("data", {})["use_real_data"] = False
    cfg.settings["consensus"]["trend_filter"] = False       # votes are scripted
    cfg.settings["risk"]["reentry_cooldown_minutes"] = 0
    cfg.settings["execution"]["auto_place_orders"] = True

    async def scripted_run(self, ctx):
        vote = VOTES.get(ctx.symbol, {}).get(self.agent_id)
        if self.agent_id == "fundamental":
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               confidence=1.0, extra={"eligible": True})
        score, conf = vote or (0.0, 0.5)
        report = AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol, score=score, confidence=conf,
            bias=Bias.BULLISH if score > 0 else Bias.BEARISH if score < 0 else Bias.NEUTRAL,
            rationale=f"scripted {self.agent_id} {score:+.2f}")
        if self.agent_id == "candlestick" and vote and ctx.quote:
            report.invalidation_level = round(
                ctx.quote.last_price * (0.99 if score > 0 else 1.01), 2)
        return report

    real_quote = paper_mod.PaperBroker.get_quote

    async def scripted_quote(self, symbol):
        if symbol in PRICES:
            return Quote(symbol=symbol, last_price=PRICES[symbol])
        return await real_quote(self, symbol)

    mp.setattr(base_mod.BaseAgent, "run", scripted_run)
    mp.setattr(paper_mod.PaperBroker, "get_quote", scripted_quote)

    from app.main import app

    port = _free_port()
    server = _Server(app, port)
    server.start()
    yield {"url": f"http://127.0.0.1:{port}", "server": server, "app": app, "cfg": cfg}
    server.stop()
    mp.undo()
    cfg.reload()
    cfg.switch_market("IN")


@pytest.fixture(scope="module")
def page(desk):
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as p:
        exe = _chromium()
        browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
        pg = browser.new_page(viewport={"width": 1400, "height": 1000})
        pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errors.append(f"console: {m.text}")
              if m.type == "error" else None)
        pg.goto(desk["url"])
        pg.wait_for_function("document.getElementById('conn-text').textContent === 'live'",
                             timeout=15000)
        pg.errors = errors
        yield pg
        browser.close()


def _api(desk, method: str, path: str, **kw):
    r = httpx.request(method, desk["url"] + path, timeout=60, **kw)
    assert r.status_code < 400, f"{method} {path} -> {r.status_code}: {r.text[:300]}"
    return r.json()


def _text(pg, selector: str) -> str:
    return pg.inner_text(selector)


def _wait_text(pg, selector: str, needle: str, timeout: float = 15000) -> None:
    """Wait until the element's text contains `needle`, ignoring case (the
    stylesheet uppercases labels, so what is rendered is not what is typed)."""
    pg.wait_for_function(
        "([s, n]) => (document.querySelector(s)?.innerText || '').toLowerCase()"
        ".includes(n.toLowerCase())",
        arg=[selector, needle], timeout=timeout)


# --------------------------------------------------------------------------- #
# 1. The page loads, every panel renders, the socket is live
# --------------------------------------------------------------------------- #
def test_every_panel_renders_on_load(page):
    for selector in ("#risk-stats", "#record-stats", "#focus-body", "#log",
                     "#positions", "#signals"):
        assert page.is_visible(selector), selector
    assert len(page.query_selector_all("#risk-stats .stat")) >= 6
    assert "capital" in _text(page, "#risk-stats").lower()
    assert page.inner_text("#conn-text").lower() == "live"
    for header in ("#market-clock", "#mode-badge", "#td-badge"):
        assert page.inner_text(header).strip(), header
    assert not page.is_visible("#s-today") and not page.is_visible("#s-weekly")


# --------------------------------------------------------------------------- #
# 2. Arming shows up live in the header and the log
# --------------------------------------------------------------------------- #
def test_arming_updates_header_and_log_live(desk, page):
    _api(desk, "POST", "/api/trading-day/start")
    page.wait_for_function(
        "document.getElementById('td-badge').innerText.toLowerCase().includes('armed')",
        timeout=10000)
    _wait_text(page, "#log", "armed")


# --------------------------------------------------------------------------- #
# 3. A trade: log, open positions, signals, paper record — all live
# --------------------------------------------------------------------------- #
def test_a_trade_appears_in_every_panel_without_a_reload(desk, page):
    PRICES["SPY"] = 600.0
    VOTES["SPY"] = {"candlestick": (0.7, 0.9), "derivatives": (0.6, 0.8)}
    result = _api(desk, "POST", "/api/cycle/run", json={"symbols": ["SPY"]})
    [spy] = result["results"]
    assert spy["signal_id"], spy["rejected"]

    _wait_text(page, "#log", "PAPER BUY")
    _wait_text(page, "#positions", "SPY")
    assert "why it bought" in _text(page, "#positions").lower()
    assert "still held" in _text(page, "#positions").lower()
    _wait_text(page, "#signals", "SPY")
    page.wait_for_function(
        "document.querySelector('#record-stats .stat .value')?.innerText.trim() === '1'",
        timeout=15000)
    _wait_text(page, "#record-stats", "SPY")


def test_the_live_candidate_shows_the_trade_and_its_chart(desk, page):
    """The trade just taken, as the live candidate: BUY · SPY, its entry,
    stop and target, why — and the symbol's chart drawn beside it."""
    page.evaluate("loadCandidate()")
    _wait_text(page, "#cand-why", "BUY · SPY")
    why = _text(page, "#cand-why").lower()
    assert "taken" in why and "entry" in why and "stop" in why and "target" in why
    assert "what kills it" in why
    _wait_text(page, "#cand-symbol", "SPY · 5m")
    assert page.query_selector("#cand-chart canvas") is not None
    shots = os.getenv("PANEL_SHOTS")
    if shots:
        page.query_selector("#s-candidate").screenshot(path=f"{shots}/candidate.png")


# --------------------------------------------------------------------------- #
# 4. The regression from 28 Sept: a burst must not silence the log
# --------------------------------------------------------------------------- #
def test_the_log_survives_a_burst_of_events(desk, page):
    from app.core.bus import bus

    async def burst():
        for i in range(3000):
            await bus.publish("agent.report", {"agent_id": "x", "symbol": "Q", "score": 0, "i": i})
        await bus.publish("trading_day.state", {"armed": True, "orders_will_be_placed": True,
                                                "armed_by": "burst-check"})

    before = page.inner_text("#log").count("armed")
    desk["server"].call(burst())
    page.wait_for_function(
        "n => document.getElementById('log').innerText.split('armed').length - 1 > n",
        arg=before, timeout=15000)
    assert page.inner_text("#conn-text").lower() == "live"


# --------------------------------------------------------------------------- #
# 5. The exit: target reached — log, positions, paper record, "since this code"
# --------------------------------------------------------------------------- #
def test_the_exit_appears_in_every_panel_without_a_reload(desk, page):
    engine = desk["app"].state.engine
    from app.storage import db

    [row] = [r for r in db.open_signals() if r["symbol"] == "SPY"]
    PRICES["SPY"] = row["target"] + 1.0
    VOTES["SPY"] = {}
    desk["server"].call(engine.outcomes.poll())

    _wait_text(page, "#log", "CLOSED SPY")
    _wait_text(page, "#log", "Target")
    page.wait_for_function(
        "!document.getElementById('positions').innerText.includes('SPY')", timeout=15000)
    _wait_text(page, "#record-current", "1 closed")
    _wait_text(page, "#record-current", "Today")
    _wait_text(page, "#record-meta", "today")
    _wait_text(page, "#record-trades", "Target")

    # The audit log has both halves of the trade, with the reasons.
    events = _api(desk, "GET", "/api/audit")["events"]
    mine = [e for e in events if e.get("signal_id") == row["id"]]
    assert {e["event"] for e in mine} == {"BUY", "SELL"}
    buy = next(e for e in mine if e["event"] == "BUY")
    assert buy["why"]["headline"] and buy["analysts"]
    assert next(e for e in mine if e["event"] == "SELL")["why_sold"].startswith("Target")


# --------------------------------------------------------------------------- #
# 6. The focus list
# --------------------------------------------------------------------------- #
def test_the_watching_panel_fills_after_a_ranking(desk, page):
    _api(desk, "POST", "/api/cycle/run", json={})
    _wait_text(page, "#focus-body", "Band A", timeout=30000)
    for band in ("Band B", "Band C"):
        assert band.lower() in _text(page, "#focus-body").lower()
    assert "top 5 of each band" in _text(page, "#focus-meta").lower()
    _wait_text(page, "#log", "watching the top")


# --------------------------------------------------------------------------- #
# 7. The reviews
# --------------------------------------------------------------------------- #
def test_todays_review_is_this_market_and_explains_the_trade(page):
    page.click("#btn-day-review")
    page.wait_for_selector("#s-today", state="visible", timeout=15000)
    _wait_text(page, "#today-meta", "US")
    _wait_text(page, "#today-body", "SPY")
    assert "target" in _text(page, "#today-body").lower()


def test_weekly_review_builds(page, monkeypatch):
    from datetime import timedelta

    from app.core import audit, clock
    from app.core.config import get_config
    # On a weekend "today" is outside the Monday-Friday week under review:
    # run on the latest weekday's clock (the server shares this process).
    real = clock.market_now

    def weekday(tz):
        now = real(tz)
        while now.weekday() > 4:
            now -= timedelta(days=1)
        return now

    monkeypatch.setattr(clock, "market_now", weekday)
    # A buy in this week's audit log, for the review to show day by day.
    audit._write(get_config(), {
        "event": "BUY", "signal_id": "SIG-BROWSER", "symbol": "SPY",
        "tradingsymbol": "SPY", "quantity": 5, "entry": 570.0, "stop_loss": 566.0,
        "target": 578.0, "why": {"headline": "3 analysts agreed on a long"}})
    page.click("#btn-weekly")
    page.wait_for_selector("#s-weekly", state="visible", timeout=15000)
    page.wait_for_function(
        "document.getElementById('weekly-body').innerText.trim().length > 20",
        timeout=60000)
    assert "Could not" not in _text(page, "#weekly-body")
    # The week's audit log, day by day, is part of the review.
    page.wait_for_selector("#weekly-audit", state="attached", timeout=5000)
    audit_text = _text(page, "#weekly-audit")
    assert "Audit log — day by day" in audit_text and "SPY" in audit_text
    assert "3 analysts agreed" in audit_text


# --------------------------------------------------------------------------- #
# 8. The rules pop-up
# --------------------------------------------------------------------------- #
def test_the_rules_popup_opens_fills_and_closes(desk, page):
    page.click("#btn-rules")
    page.wait_for_function("document.getElementById('rules-dialog').open", timeout=5000)
    _wait_text(page, "#rules-body", "How a trade happens")
    body = _text(page, "#rules-body").lower()
    for heading in ("the vote", "how much it buys", "when it sells", "the analysts"):
        assert heading in body, heading
    page.keyboard.press("Escape")
    page.wait_for_function("!document.getElementById('rules-dialog').open", timeout=5000)


# --------------------------------------------------------------------------- #
# 9. A market switch resets what belongs to the old market
# --------------------------------------------------------------------------- #
def test_a_market_switch_updates_the_page_live(desk, page):
    _api(desk, "POST", "/api/markets/IN")
    page.wait_for_function(
        "document.documentElement.dataset.market === 'IN'", timeout=15000)
    assert not page.is_visible("#s-today")
    _wait_text(page, "#risk-stats", "₹")
    _wait_text(page, "#log", "now trading")


def test_a_summary_for_another_market_does_not_take_over_today(desk, page):
    from app.core.bus import bus

    desk["server"].call(bus.publish("trading_day.summary", {
        "date": "2026-09-28", "market": "US", "trades": [], "top_rejections": [],
        "signals_generated": 0, "trades_taken": 0, "trades_closed": 0,
        "still_open": 0, "win_rate": 0, "total_r": 0, "pnl": 0}))
    time.sleep(1.0)
    assert not page.is_visible("#s-today")


# --------------------------------------------------------------------------- #
# 10. Theme, phone width, and no errors anywhere along the way
# --------------------------------------------------------------------------- #
def test_theme_toggle_and_phone_width(desk, page):
    before = page.evaluate("document.documentElement.dataset.theme")
    page.click("#btn-theme")
    assert page.evaluate("document.documentElement.dataset.theme") != before
    page.set_viewport_size({"width": 390, "height": 900})
    time.sleep(0.3)
    assert page.evaluate("document.documentElement.scrollWidth") <= 392
    page.set_viewport_size({"width": 1400, "height": 1000})


def test_no_script_errors_during_the_whole_session(page):
    assert page.errors == [], page.errors
