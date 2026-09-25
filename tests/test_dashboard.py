"""The dashboard: a lean page, a chart that shows today's trend in market
time, trade events a person can read, and paper trading that stays on.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.models import Candle

STATIC = Path(__file__).resolve().parents[1] / "dashboard" / "static"


# --------------------------------------------------------------------------- #
# The candles endpoint
# --------------------------------------------------------------------------- #
class _Broker:
    """Three sessions of 5m bars, as a feed returns them (UTC)."""

    def __init__(self, bars):
        self.bars = bars

    async def get_candles(self, symbol, timeframe, count=200):
        return self.bars


def _sessions(open_utc_hour: int, open_utc_min: int, days=(22, 23, 24)):
    bars = []
    for day in days:
        bell = datetime(2026, 9, day, open_utc_hour, open_utc_min, tzinfo=UTC)
        bars += [Candle(ts=bell + timedelta(minutes=5 * i), open=100, high=101,
                        low=99, close=100.5, volume=1000.0) for i in range(75)]
    return bars


@pytest.fixture
def client(cfg):
    from app.api.routes import router

    app = FastAPI()
    app.include_router(router)

    class _Engine:
        broker = _Broker(_sessions(3, 45))          # 09:15 IST

    app.state.engine = _Engine()
    with TestClient(app) as c:
        c.engine = app.state.engine
        yield c


def test_the_chart_shows_the_latest_session_only(client, cfg):
    """Several days on one axis squeeze today into the right-hand edge — the
    part that shows the trend the desk is acting on."""
    from zoneinfo import ZoneInfo

    body = client.get("/api/market/NIFTY/candles?timeframe=5m").json()
    assert body["session_only"] is True
    zone = ZoneInfo(body["timezone"])
    days = {datetime.fromtimestamp(c["time"], tz=UTC).astimezone(zone).date()
            for c in body["candles"]}
    assert len(days) == 1


def test_the_lines_get_warm_up_bars_from_before_the_session(client):
    """An EMA 50 started from nine bars of today is not an EMA 50.

    The earlier bars come back separately so the chart can compute its lines
    over them and still draw only today.
    """
    body = client.get("/api/market/NIFTY/candles?timeframe=5m&warmup=60").json()
    assert len(body["warmup"]) == 60
    assert body["warmup"][-1]["time"] < body["candles"][0]["time"]


def test_the_session_is_the_markets_day_not_utcs(client, cfg):
    """An Indian session opens at 03:45 UTC. Splitting on the UTC date would
    be fine here — but a US session crosses UTC midnight at the close, and
    New York's evening would land on tomorrow. The market's own zone is the
    only one that keeps a session in one piece."""
    body = client.get("/api/market/NIFTY/candles?timeframe=5m").json()
    assert body["timezone"] == cfg.market.timezone


def test_daily_bars_are_not_trimmed_to_one_day(client):
    """Across days is the point of a daily chart."""
    body = client.get("/api/market/NIFTY/candles?timeframe=1d").json()
    assert body["session_only"] is False
    assert len(body["candles"]) > 75


def test_the_full_history_is_there_on_request(client):
    body = client.get("/api/market/NIFTY/candles?timeframe=5m&session=false").json()
    assert body["session_only"] is False
    assert len(body["candles"]) > 75


# --------------------------------------------------------------------------- #
# Trade events a person can read
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_order_event_says_what_was_bought(cfg, monkeypatch):
    """The event used to carry only a signal id, so the most a notification
    or a log line could ever say was "an order was placed"."""
    from app.agents.dispatcher import Dispatcher
    from app.core.bus import Topic, bus
    from app.core.models import Instrument, Side, SignalStatus, TradeSignal

    published: list[tuple[str, object]] = []

    async def _capture(topic, payload):
        published.append((topic, payload))

    monkeypatch.setattr(bus, "publish", _capture)
    cfg.settings.setdefault("execution", {})["auto_place_orders"] = True

    class _Order:
        ok, order_id, message = True, "P-1", "filled"

        def dict(self):
            return {"ok": True, "order_id": "P-1", "message": "filled"}

    class _Broker:
        is_paper_account = True
        name = "paper"

        async def place_signal(self, signal, **_kw):
            return _Order()

    dispatcher = Dispatcher(broker=_Broker(), cfg=cfg)
    monkeypatch.setattr(dispatcher, "_external_alert", _noop)
    signal = TradeSignal(
        id="S-1", symbol="RELIANCE",
        instrument=Instrument(symbol="RELIANCE", tradingsymbol="RELIANCE"),
        side=Side.BUY, entry=2890.5, stop_loss=2870.0, target=2931.0,
        quantity=10, status=SignalStatus.APPROVED)
    await dispatcher.dispatch(signal)

    opened = [p for t, p in published
              if t == Topic.POSITION_UPDATE and isinstance(p, dict)]
    assert opened, "placing an order must publish a position update"
    event = opened[0]
    assert event["event"] == "opened"
    assert event["symbol"] == "RELIANCE" and event["side"] == "BUY"
    assert event["quantity"] == 10 and event["entry"] == 2890.5
    assert event["paper"] is True


async def _noop(*_a, **_k):
    return None


# --------------------------------------------------------------------------- #
# Paper trading stays on
# --------------------------------------------------------------------------- #
def _run_ensure(monkeypatch, tmp_path, broker: str, env_value: str | None,
                alpaca_paper: str = "true"):
    import run
    from app.core import config as config_mod

    env = tmp_path / ".env"
    if env_value is not None:
        env.write_text(f"AUTO_PLACE_ORDERS={env_value}\n", encoding="utf-8")
    monkeypatch.setattr(run, "__file__", str(tmp_path / "run.py"))
    (tmp_path / ".env.example").write_text("AUTO_PLACE_ORDERS=true\n",
                                           encoding="utf-8")
    monkeypatch.setenv("ALPACA_PAPER", alpaca_paper)
    if env_value is None:
        monkeypatch.delenv("AUTO_PLACE_ORDERS", raising=False)
    else:
        monkeypatch.setenv("AUTO_PLACE_ORDERS", env_value)

    cfg = config_mod.get_config()
    cfg.reload()
    cfg.settings.setdefault("execution", {})["broker"] = broker
    cfg.settings["execution"]["auto_place_orders"] = (env_value or "").lower() == "true"
    monkeypatch.setattr(config_mod, "get_config", lambda: cfg)
    assert run._ensure_paper_orders() == 0
    return env.read_text(encoding="utf-8") if env.exists() else ""


def test_a_stale_false_on_a_paper_account_is_turned_on(monkeypatch, tmp_path):
    """.env.example used to ship AUTO_PLACE_ORDERS=false, and .env wins over
    settings.yaml — so a desk whose .env came from the template approved every
    good signal and placed nothing, while the config said it would trade."""
    written = _run_ensure(monkeypatch, tmp_path, "paper", "false")
    assert "AUTO_PLACE_ORDERS=true" in written


@pytest.mark.parametrize("broker", ["zerodha", "upstox", "angelone"])
def test_a_real_money_broker_is_never_touched(monkeypatch, tmp_path, broker):
    written = _run_ensure(monkeypatch, tmp_path, broker, "false")
    assert "AUTO_PLACE_ORDERS=false" in written


def test_alpaca_on_real_money_is_never_touched(monkeypatch, tmp_path):
    written = _run_ensure(monkeypatch, tmp_path, "alpaca", "false",
                          alpaca_paper="false")
    assert "AUTO_PLACE_ORDERS=false" in written


def test_alpaca_paper_counts_as_paper(monkeypatch, tmp_path):
    written = _run_ensure(monkeypatch, tmp_path, "alpaca", "false",
                          alpaca_paper="true")
    assert "AUTO_PLACE_ORDERS=true" in written


def test_the_template_ships_paper_trading_on():
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    assert re.search(r"^AUTO_PLACE_ORDERS=true$", example, re.M)


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #
def test_the_page_keeps_only_what_the_desk_needs_on_screen():
    """Seventeen panels became six. The ones that remain are the ones a
    running paper desk is read from; the rest are reachable from the API."""
    page = (STATIC / "index.html").read_text()
    headings = re.findall(r"<h2>([^<]+)", page)
    headings = [h.strip() for h in headings]
    assert headings == ["Account", "Watching", "Activity Log", "Open Positions",
                        "Signals", "Today", "Weekly Review"]
    for gone in ("Price Action", 'id="chart"', "LightweightCharts",
                 "Trade Opportunities", "Agent Desk", "Position Sizing",
                 "News &amp; Sentiment", "Macro Backdrop", "Option Chain",
                 "Agent Scorecard", "Trade Journal", "Historical Replay"):
        assert gone not in page, gone


def test_the_header_holds_status_and_the_reviews_only():
    page = (STATIC / "index.html").read_text()
    header = page[page.index("<header>"):page.index("</header>")]
    for gone in ("btn-cycle", "btn-premarket", "btn-pause"):
        assert gone not in page, gone
    assert 'id="btn-day-review"' in header and 'id="btn-weekly"' in header
    # The start button is for the exception and is hidden until needed.
    assert re.search(r'id="btn-td-start"[^>]*hidden', header)


def test_every_element_the_script_asks_for_exists():
    """A missing id is a null dereference on first render, which blanks the
    whole page rather than one panel."""
    page = (STATIC / "index.html").read_text()
    script = (STATIC / "app.js").read_text()
    ids = set(re.findall(r'\$\("([a-zA-Z0-9_-]+)"\)', script))
    ids.discard("btn-edit-capital")       # rendered by the script itself
    missing = sorted(i for i in ids if f'id="{i}"' not in page)
    assert not missing, f"app.js looks up ids the page does not have: {missing}"


def test_no_function_is_defined_twice():
    """A later declaration silently wins in JavaScript. The previous file had
    a whole block pasted twice, and the OLDER copy was the one that ran — so
    the auto-arm indicator's code was dead while looking alive."""
    script = (STATIC / "app.js").read_text()
    names = re.findall(r"^(?:async )?function (\w+)", script, re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, dupes


def test_the_activity_log_sits_where_the_chart_was():
    """What the desk decided, and why, is what a paper desk is read from."""
    page = (STATIC / "index.html").read_text()
    assert page.index("Activity Log") < page.index("Open Positions")
    script = (STATIC / "app.js").read_text()
    assert "loadChart" not in script and "initChart" not in script


def test_notifications_fire_on_trades_only():
    script = (STATIC / "app.js").read_text()
    body = script[script.index("function notifyTrade"):]
    body = body[:body.index("\n}\n")]
    assert '"opened", "closed"' in body
    assert "signal.approved" not in body


def test_the_decisions_log_shows_why_a_symbol_was_passed_over():
    """A quiet day must not look like a desk that is not running."""
    script = (STATIC / "app.js").read_text()
    assert "function isNewPass" in script
    assert "no trade:" in script


def test_template_risk_defaults_in_env_are_retired_but_choices_are_kept(tmp_path):
    """.env beats settings.yaml, and the template shipped 1% / 3% / 2R — so a
    .env copied from it would pin the old risk for ever. A value someone
    actually chose is left exactly as it is."""
    import run

    env = tmp_path / ".env"
    env.write_text("TOTAL_CAPITAL=100000\nRISK_PER_TRADE_PCT=1.0\n"
                   "MAX_DAILY_LOSS_PCT=4.5\nMIN_RISK_REWARD=2.0\n", encoding="utf-8")
    retired = run._retire_template_risk(env)
    text = env.read_text(encoding="utf-8")
    assert set(retired) == {"RISK_PER_TRADE_PCT", "MIN_RISK_REWARD"}
    assert "\nMAX_DAILY_LOSS_PCT=4.5\n" in text           # a choice — kept
    assert "TOTAL_CAPITAL=100000" in text
    assert not any(line.startswith("RISK_PER_TRADE_PCT") for line in text.splitlines())
    assert run._retire_template_risk(env) == []            # idempotent
