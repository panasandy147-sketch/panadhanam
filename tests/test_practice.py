"""Practice day: replaying a real session with no lookahead."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.brokers.paper import PaperBroker
from app.core.models import Candle
from app.practice.session import PracticeState
from app.scheduler import TradingEngine


class _DayFeed:
    """A feed serving one known trading day with a clean trend."""

    name = "dayfeed"
    supports_options = False
    supports_live_orders = False
    DAY = datetime(2026, 9, 17, 9, 15, tzinfo=UTC)

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def get_quote(self, symbol):
        from app.core.models import Quote
        return Quote(symbol=symbol, last_price=1000.0)

    async def get_candles(self, symbol, timeframe, count=200):
        bars = []
        price = 1000.0
        for i in range(75):
            price *= 1.001
            bars.append(Candle(ts=self.DAY + timedelta(minutes=5 * i),
                               open=price * 0.999, high=price * 1.004,
                               low=price * 0.996, close=price,
                               volume=200_000 + i * 1000))
        return bars

    async def get_expiries(self, underlying):
        return []

    async def get_option_chain(self, underlying, expiry=None):
        return None


@pytest.fixture
async def engine(cfg):
    cfg.switch_market("IN")
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    broker.data_source = _DayFeed()

    eng = TradingEngine(broker, cfg)
    eng.risk.set_capital(1_000_000)

    async def _no_news():
        return []

    async def _no_macro():
        from app.core.models import MacroSnapshot
        return MacroSnapshot()

    eng.news.fetch = _no_news
    eng.macro.fetch = _no_macro
    return eng


async def _run_to_completion(eng, timeout: float = 30.0) -> dict:
    started = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - started < timeout:
        await asyncio.sleep(0.2)
        if eng.practice.state in (PracticeState.FINISHED, PracticeState.FAILED):
            break
    return eng.practice.status()


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_practice_day_runs_to_completion(engine):
    eng = engine
    result = await eng.practice.start(symbols=["RELIANCE"], speed=600)
    assert result["started"] is True

    status = await _run_to_completion(eng)
    assert status["state"] == PracticeState.FINISHED
    r = status["result"]
    assert r["bars_total"] > 0
    assert r["bars_done"] == r["bars_total"]
    assert r["progress_pct"] == 100.0


@pytest.mark.asyncio
async def test_it_replays_the_day_the_data_actually_covers(engine):
    eng = engine
    await eng.practice.start(symbols=["RELIANCE"], speed=600)
    status = await _run_to_completion(eng)
    assert status["result"]["trading_day"] == "2026-09-17"


@pytest.mark.asyncio
async def test_a_weekend_or_missing_day_is_refused_with_a_reason(engine):
    eng = engine
    result = await eng.practice.start(trading_day="2026-09-19",
                                      symbols=["RELIANCE"], speed=600)
    assert result["started"] is False
    assert "weekend" in result["reason"].lower() or "no historical" in result["reason"].lower()


@pytest.mark.asyncio
async def test_a_malformed_date_is_rejected(engine):
    result = await engine.practice.start(trading_day="not-a-date",
                                         symbols=["RELIANCE"])
    assert result["started"] is False
    assert "YYYY-MM-DD" in result["reason"]


@pytest.mark.asyncio
async def test_two_sessions_cannot_run_at_once(engine):
    eng = engine
    assert (await eng.practice.start(symbols=["RELIANCE"], speed=1))["started"]
    second = await eng.practice.start(symbols=["RELIANCE"], speed=1)
    assert second["started"] is False
    assert "already running" in second["reason"]
    await eng.practice.stop()


@pytest.mark.asyncio
async def test_practice_is_refused_while_live_positions_are_open(engine):
    """Practice shares the risk desk, so mixing them would corrupt both P&Ls."""
    eng = engine
    eng.risk.state.open_positions = 1
    result = await eng.practice.start(symbols=["RELIANCE"], speed=600)
    assert result["started"] is False
    reason = result["reason"].lower()
    assert "position" in reason and "risk desk" in reason


@pytest.mark.asyncio
async def test_pause_and_resume_toggle_the_state(engine):
    eng = engine
    await eng.practice.start(symbols=["RELIANCE"], speed=1)
    assert (await eng.practice.pause())["state"] == PracticeState.PAUSED
    assert (await eng.practice.pause())["state"] == PracticeState.RUNNING
    await eng.practice.stop()
    assert eng.practice.state == PracticeState.IDLE


@pytest.mark.asyncio
async def test_speed_is_clamped_to_a_sane_range(engine):
    p = engine.practice
    assert p.set_speed(0) == 1
    assert p.set_speed(99_999) == 600
    assert p.set_speed(60) == 60


@pytest.mark.asyncio
async def test_rejections_are_bucketed_so_the_tally_is_readable(engine):
    """Every bar's message carries different numbers; bucketing by cause is what
    turns 'nothing fired' into a lesson."""
    eng = engine
    p = eng.practice
    await p.start(symbols=["RELIANCE"], speed=600)
    await _run_to_completion(eng)

    r = p.status()["result"]
    if r["rejected"]:
        assert r["top_rejections"], "a rejected day must explain itself"
        # Bucketed, not one entry per bar.
        assert len(r["top_rejections"]) <= 6
        assert sum(x["count"] for x in r["top_rejections"]) <= r["rejected"]


@pytest.mark.asyncio
async def test_closed_trades_carry_everything_the_journal_needs(engine):
    eng = engine
    await eng.practice.start(symbols=["RELIANCE"], speed=600)
    status = await _run_to_completion(eng)

    for t in status["result"]["closed"]:
        for key in ("symbol", "side", "entry", "stop", "target", "exit",
                    "quantity", "outcome", "r_multiple", "pnl",
                    "entry_ts", "exit_ts", "bars_held"):
            assert key in t, f"missing {key}"
        assert t["outcome"] in {"STOP", "TARGET", "SQUARE_OFF"}
        # Direction and sign must agree.
        if t["outcome"] == "TARGET":
            assert t["r_multiple"] > 0
        elif t["outcome"] == "STOP":
            assert t["r_multiple"] < 0


@pytest.mark.asyncio
async def test_nothing_is_left_open_at_the_end(engine):
    """The desk squares off; a practice session must not leave a dangling
    position that silently carries into the next run."""
    eng = engine
    await eng.practice.start(symbols=["RELIANCE"], speed=600)
    status = await _run_to_completion(eng)
    assert status["open_positions"] == 0


@pytest.mark.asyncio
async def test_practice_trades_can_be_graded_by_the_post_mortem(engine, tmp_path):
    from app.journal import store
    from app.storage import db

    db.init_db()
    store.init_journal()
    store.JOURNAL_DIR = tmp_path

    eng = engine
    await eng.practice.start(symbols=["RELIANCE"], speed=600)
    status = await _run_to_completion(eng)

    out = await eng.practice.log_to_journal()
    assert out["logged"] == len(status["result"]["closed"])
    if out["logged"]:
        assert len(store.entries(limit=50)) >= out["logged"]
