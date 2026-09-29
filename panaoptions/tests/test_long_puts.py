"""Long puts, symmetrical with long calls.

  * A 5m Tweezer Top or a double rejection at the opening-range high (or a
    swing high) is a LONG_PUT; the mirror image at the opening-range low is a
    LONG_CALL. The same rules, reflected.
  * Over budget, a put becomes a bear put debit spread (buy the higher
    strike, sell the lower) exactly as a call becomes a bull call spread.
  * Every setup is recorded as executed outright, converted to a debit
    spread, or skipped by a hard risk gate — skips included, in the audit.
  * The history backtest replays sessions through the scanner and reports
    calls and puts separately.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from panaoptions import clock
from panaoptions.engine import contracts, patterns, strategies
from panaoptions.engine import levels as levels_mod
from panaoptions.models import (
    Candle,
    Direction,
    OptionRight,
    SessionLevels,
    execution_kind,
    option_side,
)
from tests.test_debit_spreads import opt

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 9, 23, tzinfo=ET).date()

# Yesterday: a quiet range, so today's levels are today's.
YESTERDAY = [(100.0, 100.2, 99.7, 99.9)] * 30
# Today, 09:30 onward. Opening range 99.5-101.0.
TWEEZER_TOP = [
    (100.0, 100.6, 99.5, 100.4),     # 09:30
    (100.4, 101.0, 100.2, 100.8),    # 09:35
    (100.8, 100.9, 100.3, 100.5),    # 09:40  -> OR high 101.00
    (100.5, 100.7, 100.2, 100.6),    # 09:45
    (100.6, 100.8, 100.4, 100.7),    # 09:50
    (100.7, 101.0, 100.6, 100.95),   # 09:55  green, high 101.00
    (100.95, 101.0, 100.55, 100.85), # 10:00  red, high 101.00: tweezer top
    (100.6, 100.65, 100.3, 100.4),   # 10:05  breaks the trigger (100.55)
]
DOUBLE_TOP = [
    (100.0, 100.6, 99.5, 100.4),     # 09:30
    (100.4, 101.0, 100.2, 100.4),    # 09:35  first rejection of 101.00
    (100.4, 100.6, 100.1, 100.3),    # 09:40  pulls back
    (100.3, 100.7, 100.3, 100.6),    # 09:45
    (100.6, 100.8, 100.5, 100.75),   # 09:50
    (100.75, 101.0, 100.5, 100.62),  # 09:55  second rejection of 101.00
    (100.6, 100.62, 100.3, 100.4),   # 10:00  breaks the trigger (100.50)
]


def _candles(rows, day=DAY, start="09:30", before=YESTERDAY):
    out = []
    prev = datetime.combine(day - timedelta(days=1), datetime.min.time(), tzinfo=ET)
    for i, (o, h, lo, c) in enumerate(before):
        ts = prev.replace(hour=13) + timedelta(minutes=5 * i)
        out.append(Candle(ts=ts.astimezone(UTC), open=o, high=h, low=lo, close=c, volume=4000))
    hh, mm = (int(x) for x in start.split(":"))
    t0 = datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=hh, minute=mm)
    for i, (o, h, lo, c) in enumerate(rows):
        out.append(Candle(ts=(t0 + timedelta(minutes=5 * i)).astimezone(UTC), open=o,
                          high=h, low=lo, close=c, volume=6000))
    return out


def mirror(rows, axis=200.0):
    """The same session upside down: every top becomes a bottom."""
    return [(axis - o, axis - lo, axis - h, axis - c) for o, h, lo, c in rows]


@pytest.fixture
def scan_cfg(cfg):
    for name in ("orb_vwap", "vwap_ema_pullback", "liquidity_sweep", "va_rejection",
                 "lvn_acceleration", "poc_bounce"):
        cfg.data["strategies"].setdefault(name, {})["enabled"] = False
    c = cfg.data["strategies"]["candlestick_at_level"]
    c.update({"enabled": True, "from": "09:45", "to": "15:00", "timeframe": "5m"})
    return cfg


def _session(bars):
    return levels_mod.compute(bars, "America/New_York", DAY, "09:30", "16:00")


def _evaluate(rows, cfg, before=YESTERDAY):
    bars = _candles(rows, before=before)
    winner, _ = strategies.evaluate_all("SPY", bars, _session(bars), cfg)
    return winner


# --------------------------------------------------------------------------- #
# 1. Symmetrical scanning
# --------------------------------------------------------------------------- #
def test_a_tweezer_top_at_the_opening_range_high_is_a_long_put(scan_cfg):
    setup = _evaluate(TWEEZER_TOP, scan_cfg)
    assert setup is not None and setup.direction is Direction.SHORT
    assert setup.pattern == "Tweezer Top"
    assert setup.key_level_source == "opening range high"
    assert option_side(setup.direction) == "LONG_PUT"


def test_a_double_rejection_at_the_opening_range_high_is_a_long_put(scan_cfg):
    setup = _evaluate(DOUBLE_TOP, scan_cfg)
    assert setup is not None and setup.direction is Direction.SHORT
    assert setup.pattern == "Double Rejection Top"
    # 101.00 is both the opening-range high and the swing high the first
    # rejection made: resistance either way.
    assert setup.key_level_source in {"opening range high", "swing high"}
    assert setup.underlying_support == pytest.approx(101.0)     # above both highs


@pytest.mark.parametrize("rows,name", [(TWEEZER_TOP, "Tweezer Bottom"),
                                       (DOUBLE_TOP, "Double Rejection Bottom")])
def test_the_mirror_image_at_the_opening_range_low_is_a_long_call(scan_cfg, rows, name):
    setup = _evaluate(mirror(rows), scan_cfg, before=mirror(YESTERDAY))
    assert setup is not None and setup.direction is Direction.LONG
    assert setup.pattern == name
    assert setup.key_level_source in {"opening range low", "swing low"}
    assert option_side(setup.direction) == "LONG_CALL"


def test_every_pattern_is_held_to_the_0_40_0_50_primary_band(scan_cfg):
    assert _evaluate(TWEEZER_TOP, scan_cfg).delta_band == (0.40, 0.50)
    assert _evaluate(mirror(TWEEZER_TOP), scan_cfg,
                     before=mirror(YESTERDAY)).delta_band == (0.40, 0.50)


def test_a_double_rejection_needs_a_real_pullback_between_the_tests():
    frame = pd.DataFrame([(100.0, 101.0, 99.0, 99.5)] * 3
                         + [(100.4, 101.0, 100.2, 100.4)] * 4,
                         columns=["open", "high", "low", "close"])
    assert patterns.double_rejection(frame, top=True) == 0      # a flat run is not one
    rows = DOUBLE_TOP[:6]
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    assert patterns.double_rejection(frame, top=True) == 4


def test_vwap_is_resistance_from_below_and_support_from_above():
    df = pd.DataFrame([(99.0, 99.2, 98.8, 99.0)] * 5, columns=["open", "high", "low", "close"])
    below = levels_mod.key_levels(df, SessionLevels(), vwap=100.0)
    assert [lv.kind for lv in below if lv.source == "VWAP"] == ["resistance"]
    df["close"] = 101.0
    above = levels_mod.key_levels(df, SessionLevels(), vwap=100.0)
    assert [lv.kind for lv in above if lv.source == "VWAP"] == ["support"]


# --------------------------------------------------------------------------- #
# 2 & 3. Both directions through the same ladder
# --------------------------------------------------------------------------- #
@pytest.fixture
def band(cfg):
    cfg.data["contracts"].update(min_dte=0, max_dte=4, min_delta=0.40, max_delta=0.50,
                                 max_contract_price=10.0, min_contract_price=0.10)
    return cfg


def puts():
    P = OptionRight.PUT
    return [opt(500, 0.46, 6.20, P), opt(498, 0.39, 5.00, P), opt(495, 0.30, 3.60, P),
            opt(492, 0.22, 2.60, P), opt(503, 0.60, 8.40, P)]


def test_a_put_that_fits_is_bought_outright(band):
    search = contracts.choose("SPY", puts(), Direction.SHORT, band, budget=700.0)
    assert search.tier == "primary" and search.chosen.strike == 500
    assert execution_kind(search.chosen, Direction.SHORT) == "OUTRIGHT_LONG_PUT"


def test_an_over_budget_put_becomes_a_bear_put_spread(band):
    search = contracts.choose("SPY", puts(), Direction.SHORT, band, budget=400.0)
    c = search.chosen
    assert c.structure == "bear put spread"
    assert c.long_leg.strike == 500 and c.short_leg.strike < 500   # buy higher, sell lower
    assert c.cost(100) <= 400.0
    assert execution_kind(c, Direction.SHORT) == "BEAR_PUT_DEBIT_SPREAD"
    assert "CONVERTED to a bear put debit spread" in search.note


def test_the_put_secondary_tier_rejects_a_spread_over_7_percent(band):
    band.data["contracts"]["debit_spread"] = {"enabled": False}
    chain = puts()
    for c in chain:
        if c.strike in (498, 495):
            c.bid, c.ask = round(c.mid * 0.95, 2), round(c.mid * 1.05, 2)   # 10% wide
    search = contracts.choose("SPY", chain, Direction.SHORT, band, budget=400.0)
    assert search.chosen is None and "SKIPPED — hard risk failure" in search.note


def test_calls_and_puts_are_tagged(band):
    from panaoptions.models import Signal
    ts = datetime(2026, 9, 23, 10, tzinfo=ET)
    put = contracts.choose("SPY", puts(), Direction.SHORT, band, budget=700.0).chosen
    sig = Signal(id="S", ts=ts, symbol="SPY", direction=Direction.SHORT, contract=put)
    assert (sig.side_tag, sig.execution) == ("LONG_PUT", "OUTRIGHT_LONG_PUT")
    spread = contracts.make_spread(opt(500, 0.45, 6.0), opt(505, 0.3, 3.4))
    sig = Signal(id="S", ts=ts, symbol="SPY", direction=Direction.LONG, contract=spread)
    assert (sig.side_tag, sig.execution) == ("LONG_CALL", "BULL_CALL_DEBIT_SPREAD")


# --------------------------------------------------------------------------- #
# 4. Execution logging, on the desk
# --------------------------------------------------------------------------- #
class PutFeed:
    """The Tweezer Top session, and a put chain priced off it."""

    def __init__(self, put_mid=0.80, liquid=True):
        self.put_mid, self.liquid = put_mid, liquid

    async def connect(self):
        return True

    async def close(self):
        return None

    async def quote(self, symbol):
        return {"symbol": symbol, "last_price": 100.4, "previous_close": 99.9,
                "pre_market_price": 100.0, "volume": 5_000_000}

    async def candles(self, symbol, interval="5m", include_prepost=False):
        return _candles(TWEEZER_TOP)

    async def expiries(self, symbol):
        return []

    async def chain_for_window(self, symbol, spot, min_dte, max_dte):
        oi, vol = (900, 300) if self.liquid else (2, 0)
        P = OptionRight.PUT
        return [opt(100, 0.45, self.put_mid, P, symbol=symbol, dte=2, oi=oi, volume=vol),
                opt(98, 0.28, self.put_mid * 0.45, P, symbol=symbol, dte=2, oi=oi, volume=vol)]


@pytest.fixture
def desk(scan_cfg, monkeypatch, tmp_path):
    from panaoptions.app import OptionsDesk
    from panaoptions.data import premarket
    from panaoptions.ledger import store
    from panaoptions.models import PreMarketRead

    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "p.db")
    scan_cfg.data["agents"]["enabled"] = False        # the execution path, not the vote
    # These tests are about how a put executes. The 1:3 gate and the PDH/PDL
    # confluence filter have their own tests (tests/test_fno_confluence.py).
    scan_cfg.data["risk"]["min_reward_risk"] = 0
    scan_cfg.data.setdefault("fno", {})["confluence"] = {"enabled": False}
    scan_cfg.data["contracts"].update(min_dte=0, max_dte=4, max_contract_price=5.0,
                                      min_contract_price=0.10)
    scan_cfg.data["universe"]["symbols"] = ["SPY"]
    monkeypatch.setattr(clock, "now", lambda tz: datetime(2026, 9, 23, 10, 10, tzinfo=ET))

    async def _screen(feed, cfg, now, symbols=None):
        return [PreMarketRead(symbol="SPY", passed=True)]

    monkeypatch.setattr("panaoptions.app.screen", _screen)
    assert premarket is not None
    return OptionsDesk(cfg=scan_cfg, feed=PutFeed())


def _events(desk):
    return desk.activity.recent(200)


def test_the_desk_executes_a_long_put_outright_and_says_so(desk):
    from panaoptions import audit

    asyncio.run(desk.cycle())
    fired = [e for e in _events(desk) if e["kind"] == "setup.fired"]
    assert fired and "SPY LONG_PUT" in fired[0]["detail"]
    [trade] = desk.ledger.open_trades.values()
    assert (trade.side_tag, trade.execution) == ("LONG_PUT", "OUTRIGHT_LONG_PUT")
    opened = next(e for e in _events(desk) if e["kind"] == "trade.open")
    assert "EXECUTED LONG_PUT as outright long option (OUTRIGHT_LONG_PUT)" in opened["detail"]
    [buy] = [e for e in audit.entries(DAY) if e["event"] == "BUY"]
    assert buy["side"] == "LONG_PUT" and buy["execution"] == "OUTRIGHT_LONG_PUT"


def test_an_over_budget_put_is_executed_as_a_bear_put_spread(desk):
    desk.feed = PutFeed(put_mid=1.60)                  # $160 > the $100 budget
    asyncio.run(desk.cycle())
    [trade] = desk.ledger.open_trades.values()
    assert trade.execution == "BEAR_PUT_DEBIT_SPREAD" and trade.short_label.endswith("98P")
    opened = next(e for e in _events(desk) if e["kind"] == "trade.open")
    assert "as converted debit spread (BEAR_PUT_DEBIT_SPREAD)" in opened["detail"]


def test_a_skip_is_recorded_in_the_audit_and_the_week(desk):
    from panaoptions import audit
    from panaoptions.journal import weekly

    desk.feed = PutFeed(put_mid=1.60, liquid=False)
    asyncio.run(desk.cycle())
    assert not desk.ledger.open_trades
    [skip] = [e for e in audit.entries(DAY) if e["event"] == "SKIP"]
    assert skip["side"] == "LONG_PUT" and skip["execution"] == "SKIPPED_HARD_RISK"
    assert skip["gate"] == "contract ladder" and "hard risk failure" in skip["reason"]
    [day] = audit.by_day(DAY, DAY)
    assert (day["skipped"], day["buys"]) == (1, 0)
    review = asyncio.run(weekly.build(desk.cfg, with_coach=False))
    md = weekly.to_markdown(review, desk.cfg)
    assert "1 skipped" in md and "SKIPPED_HARD_RISK" in md
    assert "setup(s) skipped by a hard risk gate" in audit.day_markdown(DAY)


# --------------------------------------------------------------------------- #
# The history backtest, both directions
# --------------------------------------------------------------------------- #
class TwoWayHistory:
    """SPY prints the Tweezer Top session; 'MIRR' prints its mirror image."""

    async def candles(self, symbol, interval="5m", include_prepost=False):
        if interval == "1d":
            start = datetime(2026, 8, 17, 20, tzinfo=UTC)
            base = 100.0
            return [Candle(ts=start + timedelta(days=i), open=base, high=base * 1.01,
                           low=base * 0.99, close=base * (1.01 if i % 2 else 0.99),
                           volume=1e7) for i in range(40)]
        rows = TWEEZER_TOP + [(100.4, 100.5, 100.0, 100.1)] * 6
        if symbol == "MIRR":
            return _candles(mirror(rows), before=mirror(YESTERDAY))
        return _candles(rows)


def test_the_history_backtest_finds_calls_and_puts_and_how_each_executes(scan_cfg):
    from panaoptions import backtest_spreads as bt

    scan_cfg.data["contracts"].update(min_dte=0, max_dte=4, max_contract_price=10.0,
                                      index_max_contract_price=10.0)
    now = datetime(2026, 9, 24, 9, 0, tzinfo=ET)
    items = asyncio.run(bt.scan_history(["SPY", "MIRR"], 1, TwoWayHistory(), scan_cfg, now))
    sides = {(b.symbol, option_side(Direction.LONG if b.direction == "LONG"
                                    else Direction.SHORT), b.pattern) for b in items}
    assert ("SPY", "LONG_PUT", "Tweezer Top") in sides
    assert ("MIRR", "LONG_CALL", "Tweezer Bottom") in sides
    results = asyncio.run(bt.replay(items, TwoWayHistory(), scan_cfg))
    summary = bt.summarise(results)
    assert set(summary["by_side"]) == {"LONG_CALL", "LONG_PUT"}
    for r in results:
        assert r.execution in {"OUTRIGHT_LONG_CALL", "OUTRIGHT_LONG_PUT",
                               "BULL_CALL_DEBIT_SPREAD", "BEAR_PUT_DEBIT_SPREAD",
                               "SKIPPED_HARD_RISK"}
        assert r.side == ("LONG_PUT" if r.direction == "SHORT" else "LONG_CALL")
    md = bt.to_markdown(results, summary, scan_cfg, title="scanner")
    assert "**LONG_PUT**" in md and "**LONG_CALL**" in md
