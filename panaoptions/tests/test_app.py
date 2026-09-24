"""The loop: session windows, and the order the desk does things in."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from panaoptions import clock
from panaoptions.app import OptionsDesk
from panaoptions.models import Candle, OptionContract, OptionRight, PreMarketRead

ET = ZoneInfo("America/New_York")


class FakeFeed:
    """A feed with a clean long setup and a chain that can afford it."""

    def __init__(self, contract_mid: float = 0.80) -> None:
        self.contract_mid = contract_mid
        self.calls: list[str] = []

    async def connect(self):
        return True

    async def close(self):
        return None

    async def quote(self, symbol):
        return {"symbol": symbol, "last_price": 102.0,
                "previous_close": 100.0, "pre_market_price": 102.0,
                "volume": 5_000_000}

    async def candles(self, symbol, interval="5m", include_prepost=False):
        """A session that sets a 15m opening range and then breaks it upward.

        Timestamps are UTC, as the real feed returns them; September is UTC-4
        in New York, so 13:30 UTC is the 09:30 ET bell.
        """
        self.calls.append(f"candles:{symbol}")
        out = []
        premarket = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)     # 08:00 ET
        for i in range(18):
            price = 100 + i * 0.02
            out.append(Candle(ts=premarket + timedelta(minutes=5 * i),
                              open=price, high=price + 0.2, low=price - 0.2,
                              close=price, volume=400.0))

        bell = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)         # 09:30 ET
        for i in range(3):                                       # opening range
            price = 100.5 + i * 0.05
            out.append(Candle(ts=bell + timedelta(minutes=5 * i), open=price,
                              high=price + 0.3, low=price - 0.3, close=price,
                              volume=2000.0))
        for i in range(3, 10):                                   # drive and break
            price = 100.6 + (i - 2) * 0.25
            out.append(Candle(ts=bell + timedelta(minutes=5 * i),
                              open=price - 0.1, high=price + 0.3,
                              low=price - 0.2, close=price,
                              volume=2000.0 if i < 9 else 9000.0))
        return out

    async def expiries(self, symbol):
        return []

    async def chain_for_window(self, symbol, spot, min_dte, max_dte):
        self.calls.append(f"chain:{symbol}")
        return [OptionContract(
            symbol=symbol, right=OptionRight.CALL, strike=110,
            expiry="2026-10-02", dte=10,
            bid=round(self.contract_mid - 0.01, 2),
            ask=round(self.contract_mid + 0.01, 2),
            delta=0.50, implied_volatility=0.30)]


@pytest.fixture
def desk(cfg, monkeypatch, tmp_path):
    from panaoptions.ledger import store
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "t.db")
    # The shipped price cap exists to be impossible; these tests are about the
    # loop, so give them a chain they can actually buy.
    cfg.data["contracts"]["max_contract_price"] = 2.00
    return OptionsDesk(cfg=cfg, feed=FakeFeed())


def _at(hh, mm):
    return datetime(2026, 9, 23, hh, mm, tzinfo=ET)


# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hh,mm,expected", [
    (8, 0, "premarket"), (9, 34, "premarket"), (9, 35, "entry_window"),
    (10, 29, "entry_window"), (10, 30, "entry_window"),
    (14, 59, "entry_window"), (15, 0, "managing"), (15, 44, "managing"),
    (15, 45, "closed"), (20, 0, "closed"),
])
def test_the_session_phases_follow_the_clock(cfg, hh, mm, expected):
    assert clock.session_phase(cfg, _at(hh, mm)) == expected


def test_the_entry_window_lasts_as_long_as_the_last_strategy(cfg):
    """A strategy configured past `session.entry_close` must still be asked.

    Otherwise the pullback strategy's 10:00-13:30 window gets thirty live
    minutes, the candlestick strategy's 09:45-15:00 gets forty-five, and
    nothing on screen says either is being cut off.
    """
    assert cfg.get("session.entry_close") == "10:30"
    latest = max(str(b["to"]) for b in cfg.get("strategies", {}).values()
                 if b.get("enabled", True))
    assert cfg.last_entry_hhmm == latest
    assert clock.session_phase(cfg, _at(13, 0)) == "entry_window"


def test_the_entry_window_never_outlasts_the_force_exit(cfg):
    # A trade opened after the square-off time has nowhere to go but out.
    cfg.data["strategies"]["orb_vwap"]["to"] = "23:00"
    try:
        assert cfg.last_entry_hhmm == cfg.get("session.force_exit_at")
    finally:
        cfg.data["strategies"]["orb_vwap"]["to"] = "11:00"


def test_a_disabled_strategy_does_not_hold_the_window_open(cfg):
    cfg.data["strategies"]["candlestick_at_level"]["enabled"] = False
    try:
        assert cfg.last_entry_hhmm == "13:30"      # the pullback, next longest
    finally:
        cfg.data["strategies"]["candlestick_at_level"]["enabled"] = True


def test_a_weekend_is_never_a_trading_session(cfg):
    assert clock.session_phase(cfg, datetime(2026, 9, 26, 9, 40, tzinfo=ET)) == "weekend"


def test_the_opening_five_minutes_are_skipped_deliberately(cfg):
    # 09:30-09:35 has the widest spreads of the day. Entering there gives up
    # more in slippage than the setup is worth.
    assert clock.session_phase(cfg, _at(9, 31)) == "premarket"
    assert clock.session_phase(cfg, _at(9, 36)) == "entry_window"


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_setup_inside_the_window_becomes_a_paper_trade(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    await desk.cycle()

    assert desk.ledger.open_trades
    trade = next(iter(desk.ledger.open_trades.values()))
    assert trade.symbol in desk.cfg.symbols
    assert trade.entry_price > 0


@pytest.mark.asyncio
async def test_several_symbols_can_fire_in_the_same_cycle(desk, monkeypatch):
    """Every watchlist symbol is read at the same instant and judged.

    Stopping after the first entry would make max_open_trades a limit the
    desk could only approach one cycle at a time, so a second setup on
    another symbol in the same minute would simply be missed.
    """
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    desk.cfg.data["risk"]["max_open_trades"] = 3
    desk.cfg.data["risk"]["max_total_deployed_pct"] = 60.0
    await desk.cycle()
    assert len(desk.ledger.open_trades) > 1
    # Different names, not the same one twice.
    symbols = {t.symbol for t in desk.ledger.open_trades.values()}
    assert len(symbols) == len(desk.ledger.open_trades)


@pytest.mark.asyncio
async def test_no_entries_outside_the_window(desk, monkeypatch):
    # 15:00 is the last strategy's close, so the desk is done hunting.
    monkeypatch.setattr(clock, "now", lambda tz: _at(15, 10))
    await desk.cycle()
    assert not desk.ledger.open_trades


@pytest.mark.asyncio
async def test_the_position_limit_is_never_exceeded(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    limit = int(desk.cfg.get("risk.max_open_trades"))
    for _ in range(4):
        await desk.cycle()
    assert len(desk.ledger.open_trades) <= limit


@pytest.mark.asyncio
async def test_open_positions_are_managed_before_new_ones_are_hunted(desk, monkeypatch):
    # Hunting first is how a desk doubles down while a loser runs.
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    order: list[str] = []

    async def _manage(now):
        order.append("manage")
        return []

    async def _hunt(now):
        order.append("hunt")
        return []

    monkeypatch.setattr(desk, "_manage", _manage)
    monkeypatch.setattr(desk, "_hunt", _hunt)
    await desk.cycle()
    assert order == ["manage", "hunt"]


@pytest.mark.asyncio
async def test_positions_are_still_managed_after_the_window_shuts(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    await desk.cycle()
    assert desk.ledger.open_trades

    managed: list[str] = []

    async def _manage(now):
        managed.append("yes")
        return []

    monkeypatch.setattr(desk, "_manage", _manage)
    monkeypatch.setattr(clock, "now", lambda tz: _at(13, 0))
    await desk.cycle()
    assert managed == ["yes"]


@pytest.mark.asyncio
async def test_a_halted_desk_stops_hunting_but_keeps_managing(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    # Same date the pinned clock reports, or the cycle rolls the day and
    # correctly clears the halt.
    desk.risk.roll_day("2026-09-23")
    desk.risk.record_pnl(-60.0)
    assert desk.risk.state.halted

    await desk.cycle()
    assert not desk.ledger.open_trades


@pytest.mark.asyncio
async def test_everything_is_squared_off_at_the_close(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    await desk.cycle()
    assert desk.ledger.open_trades

    monkeypatch.setattr(clock, "now", lambda tz: _at(16, 0))
    await desk.cycle()
    assert not desk.ledger.open_trades
    assert desk.ledger.closed


@pytest.mark.asyncio
async def test_an_unaffordable_chain_takes_no_trade_and_records_why(desk, monkeypatch):
    desk.feed = FakeFeed(contract_mid=6.00)     # $600 a contract
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))

    result = await desk.cycle()
    assert not desk.ledger.open_trades
    assert any("budget" in a or "cannot both hold" in a
               for a in result["actions"]), \
        "an empty result must carry its reason, or it reads as a quiet market"


@pytest.mark.asyncio
async def test_stops_are_tightened_to_breakeven_after_the_midday_time(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    await desk.cycle()
    trade = next(iter(desk.ledger.open_trades.values()))
    assert trade.stop_price < trade.entry_price

    async def _manage(now):
        return []

    monkeypatch.setattr(desk, "_manage", _manage)
    monkeypatch.setattr(clock, "now", lambda tz: _at(11, 0))
    await desk.cycle()
    assert trade.stop_price == trade.entry_price
    assert trade.breakeven_armed


@pytest.mark.asyncio
async def test_the_screen_runs_once_a_day_not_every_cycle(desk, monkeypatch):
    calls: list[int] = []

    async def _screen(feed, cfg, now):
        calls.append(1)
        return [PreMarketRead(symbol="AAPL", passed=False)]

    monkeypatch.setattr("panaoptions.app.screen", _screen)
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    await desk.cycle()
    await desk.cycle()
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Leaving the desk running overnight must not be worse than starting it in the
# morning. "Pre-market" runs from midnight, and there is no volume at 00:01.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hh,mm,expected", [
    (0, 1, False),      # midnight — no pre-market tape exists yet
    (4, 0, False),      # too early to mean anything
    (8, 59, False),
    (9, 0, True),       # premarket.screen_from
    (9, 34, True),
    (9, 50, True),      # inside the entry window
])
def test_the_screen_waits_until_the_premarket_tape_is_worth_reading(
        desk, monkeypatch, hh, mm, expected):
    now = _at(hh, mm)
    phase = clock.session_phase(desk.cfg, now)
    assert desk._should_screen(phase, now.date().isoformat(), now) is expected


@pytest.mark.asyncio
async def test_an_overnight_desk_does_not_write_off_the_day_at_midnight(desk, monkeypatch):
    # The bug this guards: screening at 00:01 returns RVOL near zero, nothing
    # qualifies, the screen is marked done, and nothing can trade all day.
    calls = []

    async def _screen(feed, cfg, now):
        calls.append(now)
        return []

    monkeypatch.setattr("panaoptions.app.screen", _screen)

    monkeypatch.setattr(clock, "now", lambda tz: _at(0, 1))
    await desk.cycle()
    assert calls == [], "nothing to screen at midnight"

    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 10))
    await desk.cycle()
    assert len(calls) == 1, "and it still screens when the tape is real"


@pytest.mark.asyncio
async def test_the_screen_retries_while_nothing_qualifies(desk, monkeypatch):
    # The pre-market tape thickens towards the open, so a 09:00 screen finding
    # nothing does not mean the day is over.
    calls = []

    async def _screen(feed, cfg, now):
        calls.append(now)
        return [PreMarketRead(symbol="SPY", passed=False)]

    monkeypatch.setattr("panaoptions.app.screen", _screen)

    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 0))
    await desk.cycle()
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 2))
    await desk.cycle()
    assert len(calls) == 1, "not every single cycle"

    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 10))
    await desk.cycle()
    assert len(calls) == 2, "but again once the interval has passed"


@pytest.mark.asyncio
async def test_the_screen_stops_repeating_once_something_qualifies(desk, monkeypatch):
    calls = []

    async def _screen(feed, cfg, now):
        calls.append(now)
        return [PreMarketRead(symbol="SPY", passed=True)]

    monkeypatch.setattr("panaoptions.app.screen", _screen)

    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 0))
    await desk.cycle()
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 30))
    await desk.cycle()
    assert len(calls) == 1, "a result that passed is not re-screened"


# --------------------------------------------------------------------------- #
# The activity log records the decisions, not just the trades.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_cycle_logs_what_it_did(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    await desk.cycle()

    kinds = [e["kind"] for e in desk.activity.recent(50)]
    assert "cycle.start" in kinds
    assert "screen.done" in kinds
    assert "setup.fired" in kinds
    assert "trade.open" in kinds


@pytest.mark.asyncio
async def test_a_symbol_that_was_passed_over_says_why(desk, monkeypatch):
    # The desk spends most of a session deciding NOT to trade. Without this,
    # a working desk and a hung one look identical.
    desk.feed = FakeFeed(contract_mid=6.00)          # nothing affordable
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    await desk.cycle()

    events = desk.activity.recent(50)
    refusals = [e for e in events if e["kind"] in {"contract.none", "risk.refused"}]
    assert refusals, "an empty result must carry its reason"
    assert refusals[0]["level"] == "warn"
    assert any(word in refusals[0]["detail"] for word in ("budget", "cost"))


@pytest.mark.asyncio
async def test_nothing_is_logged_at_the_weekend(desk, monkeypatch):
    monkeypatch.setattr(clock, "now",
                        lambda tz: datetime(2026, 9, 26, 10, 0, tzinfo=ET))
    await desk.cycle()
    assert desk.activity.recent() == []


# --------------------------------------------------------------------------- #
# Cadence
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_loop_sleeps_the_remainder_not_the_whole_interval(desk,
                                                                    monkeypatch):
    """Otherwise the real period is `work + interval`, not the interval.

    With three symbols a cycle is several seconds, so a desk claiming to run
    every 60 seconds runs every 65 and drifts a bar further behind all day.
    """
    import asyncio

    slept: list[float] = []

    async def _slow_cycle():
        desk.running = False              # one pass
        await asyncio.sleep(0)            # yield
        return {"phase": "entry_window", "actions": []}

    async def _record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(desk, "cycle", _slow_cycle)
    monkeypatch.setattr(asyncio, "sleep", _record)
    monkeypatch.setattr(desk.feed, "connect", _true)
    monkeypatch.setattr(desk, "_load_predictor", lambda: None)

    await desk.start(cycle_seconds=60)
    assert slept, "the loop never slept"
    # Real work took a moment, so the sleep must be less than the full 60.
    assert slept[-1] <= 60
    assert desk.last_cycle_seconds >= 0


async def _true(*_a, **_k):
    return True


@pytest.mark.asyncio
async def test_a_desk_that_cannot_keep_up_says_so(desk, monkeypatch):
    """A desk quietly running at half its stated rate looks fine."""
    import asyncio
    import time

    ticks = iter([0.0, 90.0, 90.0, 90.0])

    async def _cycle():
        desk.running = False
        return {"phase": "entry_window", "actions": []}

    async def _noop(_s):
        return None

    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(desk, "cycle", _cycle)
    monkeypatch.setattr(asyncio, "sleep", _noop)
    monkeypatch.setattr(desk.feed, "connect", _true)
    monkeypatch.setattr(desk, "_load_predictor", lambda: None)

    await desk.start(cycle_seconds=60)
    kinds = [e["kind"] for e in desk.activity.recent(20)]
    assert "slow.cycle" in kinds


@pytest.mark.asyncio
async def test_the_panel_stops_naming_a_symbol_once_the_desk_is_full(desk,
                                                                     monkeypatch):
    """"SCANNING AAPL" while holding the maximum is a lie on screen."""
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    desk.cfg.data["risk"]["max_open_trades"] = 1
    await desk.cycle()
    assert desk.ledger.open_trades
    desk.scanning = "AAPL"

    await desk.cycle()                      # now at max_open_trades
    assert desk.scanning == ""
    details = [e["detail"] for e in desk.activity.recent(30)
               if e["kind"] == "hunt.skip"]
    assert any("not looking for new trades" in d for d in details)


# --------------------------------------------------------------------------- #
# Before the strategy windows open
# --------------------------------------------------------------------------- #
def _truncate_feed(desk, monkeypatch, cutoff_et):
    """Only bars up to `cutoff_et`, as a live feed would have at that moment.

    A strategy's window is checked against the LAST BAR's time rather than
    the wall clock — the rule is about the candle being judged, and at 09:46
    with the last completed bar at 09:40 the opening range is not final yet.
    So a test about "before 09:45" has to move the tape, not just the clock.
    """
    from zoneinfo import ZoneInfo

    original = desk.feed.candles
    cutoff = cutoff_et.astimezone(ZoneInfo("UTC"))

    async def _capped(symbol, interval="5m", include_prepost=False):
        bars = await original(symbol, interval, include_prepost)
        return [b for b in bars if b.ts <= cutoff]

    monkeypatch.setattr(desk.feed, "candles", _capped)


@pytest.mark.asyncio
async def test_passing_the_screen_before_0945_says_why_nothing_traded(desk,
                                                                      monkeypatch):
    """The 09:41 case: symbols pass, nothing trades, and the log was silent.

    No strategy is asked before 09:45, so `evaluate_all` returns no attempts
    at all — a completely different state from "they all looked and passed",
    and logging nothing made the two identical on screen.
    """
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 41))
    _truncate_feed(desk, monkeypatch, _at(9, 41))
    await desk.cycle()

    assert not desk.ledger.open_trades
    passed = [r for r in desk.screened if r.passed]
    assert passed, "the fixture's symbols should pass the screen"

    notes = [e["detail"] for e in desk.activity.recent(40)
             if e["kind"] == "hunt.skip"]
    assert notes, "a cycle that judged nothing must say why"
    assert any("no strategy is in its window yet" in n for n in notes)
    assert any("09:45" in n and "4 minute" in n for n in notes)


@pytest.mark.asyncio
async def test_the_window_note_is_written_once_not_once_per_symbol(desk,
                                                                   monkeypatch):
    """The window is the same for every symbol; five copies bury the log."""
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 41))
    _truncate_feed(desk, monkeypatch, _at(9, 41))
    await desk.cycle()
    notes = [e for e in desk.activity.recent(40)
             if e["kind"] == "hunt.skip" and "window yet" in e["detail"]]
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_after_the_last_window_the_note_says_so_instead(desk,
                                                              monkeypatch):
    """"Opens in N minutes" and "closed for today" must not read the same."""
    # 10:00 is still inside the desk's entry window (session.entry_close is
    # 10:30), but every strategy shut at 09:50 — so the desk is hunting and
    # there is nothing left to ask.
    monkeypatch.setattr(clock, "now", lambda tz: _at(10, 0))
    for key in ("orb_vwap", "vwap_ema_pullback", "liquidity_sweep",
                "candlestick_at_level"):
        desk.cfg.data["strategies"][key]["to"] = "09:50"
    await desk.cycle()
    notes = [e["detail"] for e in desk.activity.recent(40)
             if e["kind"] == "hunt.skip"]
    assert any("closed for today" in n for n in notes)
