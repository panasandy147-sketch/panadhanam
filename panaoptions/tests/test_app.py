"""The loop: session windows, and the order the desk does things in."""
from __future__ import annotations

from datetime import datetime, timedelta
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
        self.calls.append(f"candles:{symbol}")
        base = datetime(2026, 9, 22, 9, 30)
        out, price = [], 100.0
        for i in range(30):
            price *= 1.002
            out.append(Candle(ts=base + timedelta(minutes=5 * i), open=price * 0.999,
                              high=price * 1.003, low=price * 0.997,
                              close=price, volume=1000.0))
        prev = out[-1]
        out.append(Candle(ts=prev.ts + timedelta(minutes=5), open=prev.close * 1.001,
                          high=prev.close * 1.002, low=prev.close * 0.996,
                          close=prev.close * 0.997, volume=900.0))
        p2 = out[-1]
        out.append(Candle(ts=p2.ts + timedelta(minutes=5), open=p2.close * 0.999,
                          high=p2.close * 1.012, low=p2.close * 0.998,
                          close=p2.close * 1.011, volume=4000.0))
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
    return datetime(2026, 9, 22, hh, mm, tzinfo=ET)


# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hh,mm,expected", [
    (8, 0, "premarket"), (9, 34, "premarket"), (9, 35, "entry_window"),
    (10, 29, "entry_window"), (10, 30, "managing"), (15, 44, "managing"),
    (15, 45, "closed"), (20, 0, "closed"),
])
def test_the_session_phases_follow_the_clock(cfg, hh, mm, expected):
    assert clock.session_phase(cfg, _at(hh, mm)) == expected


def test_a_weekend_is_never_a_trading_session(cfg):
    assert clock.session_phase(cfg, datetime(2026, 9, 26, 9, 40, tzinfo=ET)) == "weekend"


def test_the_opening_five_minutes_are_skipped_deliberately(cfg):
    # 09:30-09:35 has the widest spreads of the day. Entering there gives up
    # more in slippage than the setup is worth.
    assert clock.session_phase(cfg, _at(9, 31)) == "premarket"
    assert clock.session_phase(cfg, _at(9, 36)) == "entry_window"


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_setup_inside_the_window_becomes_one_paper_trade(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 40))
    await desk.cycle()

    assert len(desk.ledger.open_trades) == 1
    trade = next(iter(desk.ledger.open_trades.values()))
    assert trade.symbol in desk.cfg.symbols
    assert trade.entry_price > 0


@pytest.mark.asyncio
async def test_no_entries_outside_the_window(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(11, 0))
    await desk.cycle()
    assert not desk.ledger.open_trades


@pytest.mark.asyncio
async def test_only_one_position_is_ever_open(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 40))
    await desk.cycle()
    await desk.cycle()
    assert len(desk.ledger.open_trades) == 1


@pytest.mark.asyncio
async def test_open_positions_are_managed_before_new_ones_are_hunted(desk, monkeypatch):
    # Hunting first is how a desk doubles down while a loser runs.
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 40))
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
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 40))
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
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 40))
    desk.risk.roll_day("2026-09-22")
    desk.risk.record_pnl(-60.0)
    assert desk.risk.state.halted

    await desk.cycle()
    assert not desk.ledger.open_trades


@pytest.mark.asyncio
async def test_everything_is_squared_off_at_the_close(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 40))
    await desk.cycle()
    assert desk.ledger.open_trades

    monkeypatch.setattr(clock, "now", lambda tz: _at(16, 0))
    await desk.cycle()
    assert not desk.ledger.open_trades
    assert desk.ledger.closed


@pytest.mark.asyncio
async def test_an_unaffordable_chain_takes_no_trade_and_records_why(desk, monkeypatch):
    desk.feed = FakeFeed(contract_mid=6.00)     # $600 a contract
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 40))

    result = await desk.cycle()
    assert not desk.ledger.open_trades
    assert any("budget" in a or "cannot both hold" in a
               for a in result["actions"]), \
        "an empty result must carry its reason, or it reads as a quiet market"


@pytest.mark.asyncio
async def test_stops_are_tightened_to_breakeven_after_the_midday_time(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 40))
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
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 40))
    await desk.cycle()
    await desk.cycle()
    assert len(calls) == 1
