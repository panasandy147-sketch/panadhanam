"""Every panaoptions panel, in a real browser, through a trading session.

Starts the real server with the real desk loop, opens the dashboard in
Chromium, and lets the desk run: screen, setup, fill, exit. After each step
it asserts that every panel refreshed on its own — no reload. Only the
market data is scripted (tests.test_app.FakeFeed) and the clock pinned, so
each step is deterministic. Skipped when no Chromium is available.
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

pytest.importorskip("playwright.sync_api")

ET = ZoneInfo("America/New_York")
NOW = {"t": datetime(2026, 9, 23, 9, 50, tzinfo=ET)}


def _chromium() -> str | None:
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return os.getenv("PLAYWRIGHT_CHROMIUM") or (found[-1] if found else None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
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

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)


@pytest.fixture(scope="module")
def desk(tmp_path_factory):
    if not _chromium() and not os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
        pytest.skip("no Chromium available for browser tests")
    tmp = tmp_path_factory.mktemp("panaoptions")
    mp = pytest.MonkeyPatch()

    from panaoptions import clock
    from panaoptions import config as config_mod
    from panaoptions.app import OptionsDesk
    from panaoptions.journal import store as journal_store
    from panaoptions.journal import weekly as journal_weekly
    from panaoptions.ledger import store
    from panaoptions.web.server import create_app
    from tests.test_app import FakeFeed

    mp.setattr(config_mod, "DATA_DIR", tmp / "data")
    mp.setattr(config_mod, "ENV_PATH", tmp / "absent.env")
    for key in ("PANAOPTIONS_CAPITAL", "PANAOPTIONS_PROVIDER", "PANAOPTIONS_PROFILE"):
        mp.delenv(key, raising=False)
    mp.setattr(store, "DATA_DIR", tmp / "data")
    from panaoptions import watchlist
    (tmp / "data").mkdir(exist_ok=True)
    mp.setattr(watchlist, "STORE", tmp / "data" / "watchlist.json")
    mp.setattr(store, "_conn", None)
    mp.setattr(journal_store, "JOURNAL_DIR", tmp / "journal")
    mp.setattr(journal_weekly, "DAILY_DIR", tmp / "journal" / "daily")
    mp.setattr(journal_weekly, "WEEKLY_DIR", tmp / "journal" / "weekly")
    mp.setattr(clock, "now", lambda tz: NOW["t"])

    cfg = config_mod.Config()
    cfg.data["contracts"]["max_contract_price"] = 2.00     # FakeFeed's $0.80 chain
    mp.setattr(config_mod, "_config", cfg)
    feed = FakeFeed()
    desk = OptionsDesk(cfg=cfg, feed=feed)
    app = create_app(desk, cycle_seconds=2)

    port = _free_port()
    server = _Server(app, port)
    server.start()
    yield {"url": f"http://127.0.0.1:{port}", "desk": desk, "feed": feed}
    server.stop()
    mp.undo()


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
        pg.errors = errors
        yield pg
        browser.close()


def _text(pg, selector: str) -> str:
    return pg.inner_text(selector)


def _wait_text(pg, selector: str, needle: str, timeout: float = 20000) -> None:
    pg.wait_for_function(
        "([s, n]) => (document.querySelector(s)?.innerText || '').toLowerCase()"
        ".includes(n.toLowerCase())",
        arg=[selector, needle], timeout=timeout)


# --------------------------------------------------------------------------- #
def test_every_panel_renders_on_load(page):
    _wait_text(page, "#b-paper", "paper")
    for badge in ("#b-phase", "#b-clock", "#b-capital"):
        page.wait_for_function("s => document.querySelector(s).innerText.trim() !== '—'",
                               arg=badge, timeout=20000)
    for body in ("#config-body", "#account-tiles", "#strategies-body", "#screen-body",
                 "#contracts-body", "#ledger-tiles", "#journal-tiles", "#activity-body",
                 "#why-body"):
        page.wait_for_function(
            "s => (document.querySelector(s)?.innerText || '').trim().length > 0 && "
            "!/^(Loading…|Checking…|—)$/.test(document.querySelector(s).innerText.trim())",
            arg=body, timeout=20000)
    assert page.input_value("#watch-input").strip()


def test_the_desk_trades_and_every_panel_shows_it(page):
    """The desk loop fires on its own; the page must follow without a reload."""
    _wait_text(page, "#activity-body", "trade.open", timeout=30000)
    _wait_text(page, "#open-body", "Exit if stock")
    _wait_text(page, "#cand-why", "Filled")
    _wait_text(page, "#ledger-tiles", "at work")
    # Must follow the fill promptly, not on a 30-second timer.
    _wait_text(page, "#why-body", "bought", timeout=10000)
    _wait_text(page, "#strategies-body", "live")


def test_an_exit_shows_in_every_panel(desk, page):
    desk["feed"].contract_mid = 2.00          # +150%: through both targets
    _wait_text(page, "#activity-body", "trade.exit", timeout=30000)
    _wait_text(page, "#trades-body", "TARGET", timeout=30000)
    page.wait_for_function(
        "document.getElementById('ledger-tiles').innerText.toLowerCase().includes('closed')",
        timeout=20000)
    # The closed trade is graded into the journal, and the Learning panel
    # shows it without a reload.
    page.wait_for_function(
        "/graded\\s*\\n?\\s*[1-9]/i.test(document.getElementById('journal-tiles').innerText)",
        timeout=20000)
    assert _text(page, "#rejections-body").strip()


def test_the_watchlist_box_saves(page):
    page.fill("#watch-input", "SPY, QQQ, AAPL")
    page.click("#btn-watch")
    _wait_text(page, "#watch-status", "Scanning 3")
    _wait_text(page, "#watch-meta", "3")


def test_the_auto_top_10_button_ranks_and_shows_why(page, desk, monkeypatch):
    from tests.test_auto_watchlist import FakeDiscovery, _market

    monkeypatch.delenv("PANAOPTIONS_AUTO_WATCHLIST", raising=False)
    desk["desk"].discovery = FakeDiscovery(_market())
    page.reload()
    page.click("#btn-watch-auto")
    _wait_text(page, "#watch-status", "Auto: scanning the top 10")
    _wait_text(page, "#watch-meta", "auto top 10")
    _wait_text(page, "#watch-auto", "S00")
    assert "RVOL" in _text(page, "#watch-auto")
    page.click("#btn-watch-reset")
    _wait_text(page, "#watch-meta", "from settings.yaml")


def test_switching_to_india_shows_the_india_watchlist_at_once(page, desk, monkeypatch):
    """The watchlist box followed the toggle only at its next poll, so right
    after switching it still showed the US names and the US ranking time."""
    from tests.test_auto_watchlist import FakeDiscovery

    monkeypatch.delenv("PANAOPTIONS_AUTO_WATCHLIST", raising=False)
    d, feed = desk["desk"], desk["feed"]
    d.discovery = FakeDiscovery({})
    d._feed_factory = lambda cfg: feed            # never the real NSE/Yahoo feed
    d.ledger.open_trades.clear()                  # a switch waits for flat
    page.reload()
    try:
        page.click("#market-toggle button[data-mode='IN']")
        # Seconds, not the next poll: the switch waits for the cycle lock.
        _wait_text(page, "#watch-meta", "09:00", timeout=15000)
        assert "NIFTY" in page.input_value("#watch-input")
        assert "SPY" not in page.input_value("#watch-input")
    finally:
        page.click("#market-toggle button[data-mode='US']")
        # The US names, not "first ranking 08:45": inside US hours the list has
        # already been ranked and the meta reads "next HH:MM" instead.
        page.wait_for_function(
            "() => document.getElementById('watch-input').value.includes('SPY')",
            timeout=15000)


def test_todays_review_builds(page):
    if page.is_checked("#d-coach"):
        page.uncheck("#d-coach")
    page.click("#btn-daily")
    page.wait_for_function(
        "document.getElementById('daily-body').innerText.trim().length > 40", timeout=30000)
    assert "could not" not in _text(page, "#daily-body").lower()


def test_weekly_review_builds(page, desk):
    from panaoptions import audit
    # Each test has its own journal folder, so put this week's buy in it.
    audit._write(desk["desk"].cfg, {
        "event": "BUY", "trade_id": "PT-BROWSER", "symbol": "SPY",
        "contract": "SPY 2026-10-02 110C", "strategy": "orb_vwap", "quantity": 1,
        "entry": 0.8, "cost": 80.0, "confirmations": ["opening-range break"]})
    if page.is_checked("#w-coach"):
        page.uncheck("#w-coach")
    page.click("#btn-weekly")
    page.wait_for_function(
        "document.getElementById('weekly-body').innerText.trim().length > 40", timeout=30000)
    assert "could not" not in _text(page, "#weekly-body").lower()
    # This week's buys and sells are in the review, date by date.
    _wait_text(page, "#weekly-body .audit-days", "audit log — day by day", timeout=5000)
    assert "BUY" in _text(page, "#weekly-body .audit-days")


def test_the_rules_popup_opens_fills_and_closes(page):
    page.click("#btn-rules")
    page.wait_for_function("document.getElementById('rules-dialog').open", timeout=5000)
    _wait_text(page, "#rules-body", "the strategies")
    body = _text(page, "#rules-body").lower()
    for part in ("how a trade happens", "which contract it buys", "how much it buys",
                 "when it sells"):
        assert part in body, part
    page.keyboard.press("Escape")
    page.wait_for_function("!document.getElementById('rules-dialog').open", timeout=5000)


def test_phone_width_has_no_sideways_scroll(page):
    page.set_viewport_size({"width": 390, "height": 900})
    time.sleep(0.3)
    assert page.evaluate("document.documentElement.scrollWidth") <= 392
    page.set_viewport_size({"width": 1400, "height": 1000})


def test_the_api_the_page_polls_all_answer(desk):
    for path in ("/api/status", "/api/screen", "/api/trades?days=30", "/api/contracts",
                 "/api/strategies", "/api/journal?days=30", "/api/why", "/api/candidate",
                 "/api/activity?decisions=true", "/api/watchlist", "/api/rules",
                 "/api/config-check"):
        r = httpx.get(desk["url"] + path, timeout=30)
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"


def test_no_script_errors_during_the_whole_session(page):
    assert page.errors == [], page.errors
