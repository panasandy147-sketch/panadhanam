"""The Previous Day Liquidity Sweep and the execution-filter changes.

  1. A 5m candle sweeps the PDL (calls) / PDH (puts) within 0.25% and the
     NEXT candle closes back inside, as a tweezer or swing; rising call/put OI.
  2. Premium x NSE lot everywhere (HDFCBANK 650, INFY 400).
  3. 09:15-10:00 IST: 0-4 DTE contracts may quote up to 12% wide.
  4. The delta fallback reaches down to 0.25.
  5. Single-leg grace when a spread has no liquid short leg.
  6. Stop at the sweep wick, target >= 1:3, verified before the order.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from panaoptions import clock, fno, markets
from panaoptions.engine import contracts, strategies
from panaoptions.engine.liquidity import spread_limit
from panaoptions.models import Candle, Direction, SessionLevels, SetupType
from tests.test_debit_spreads import opt

ET = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
DAY = datetime(2026, 9, 23).date()

YESTERDAY = ([(100.0, 101.0, 99.5, 100.2)] + [(100.2, 100.6, 99.6, 100.0)] * 76
             + [(100.0, 100.3, 99.0, 100.0)])          # PDH 101.00, PDL 99.00
TODAY = [
    (99.6, 99.9, 99.3, 99.5),      # 09:30
    (99.5, 99.7, 99.2, 99.4),      # 09:35
    (99.4, 99.5, 98.85, 98.95),    # 09:40  sweeps the PDL by 0.15%
    (98.95, 99.4, 98.9, 99.3),     # 09:45  closes back inside — tweezer bottom
]


def bars(today=TODAY, yesterday=YESTERDAY, tz=ET, open_hm=(9, 30)):
    out = []
    y0 = datetime(2026, 9, 22, *open_hm, tzinfo=tz)
    for i, (o, h, lo, c) in enumerate(yesterday):
        out.append(Candle(ts=(y0 + timedelta(minutes=5 * i)).astimezone(UTC), open=o,
                          high=h, low=lo, close=c, volume=5000))
    t0 = datetime(2026, 9, 23, *open_hm, tzinfo=tz)
    for i, (o, h, lo, c) in enumerate(today):
        out.append(Candle(ts=(t0 + timedelta(minutes=5 * i)).astimezone(UTC), open=o,
                          high=h, low=lo, close=c, volume=8000))
    return out


def mirror(rows, axis=200.0):
    return [(axis - o, axis - lo, axis - h, axis - c) for o, h, lo, c in rows]


def frame(candles, tz="America/New_York"):
    from panaoptions.engine import indicators as ta
    return strategies.localise(ta.to_frame(candles), tz)


def session(candles, tz="America/New_York", open_="09:30", close="16:00"):
    from panaoptions.engine import levels
    return levels.compute(candles, tz, DAY, open_, close)


@pytest.fixture
def only_sweep(cfg):
    for key in ("orb_vwap", "vwap_ema_pullback", "liquidity_sweep", "candlestick_at_level",
                "va_rejection", "lvn_acceleration", "poc_bounce"):
        cfg.data["strategies"].setdefault(key, {})["enabled"] = False
    return cfg


# --------------------------------------------------------------------------- #
# 1. The sweep
# --------------------------------------------------------------------------- #
def test_the_previous_session_is_on_the_tape():
    lv = session(bars())
    assert (lv.previous_high, lv.previous_low) == (101.0, 99.0)


def test_a_pdl_sweep_within_the_band_and_a_reclaim_is_found():
    found = strategies.sweep_of_previous_day(frame(bars()), session(bars()), 0.25)
    assert found["direction"] == 1 and found["wick"] == 98.85 and found["reclaim"] == 99.3
    assert found["tweezer"] and found["swing"]


def test_a_sweep_deeper_than_the_band_is_not_a_sweep():
    deep = TODAY[:2] + [(99.4, 99.5, 98.6, 98.7), (98.7, 99.4, 98.65, 99.3)]   # 0.40%
    assert strategies.sweep_of_previous_day(frame(bars(deep)), session(bars(deep)),
                                            0.25) is None


def test_only_the_first_test_of_the_level_today_is_a_sweep():
    """The stops beyond the PDL are run once. A second poke through the same
    level later in the day is chop, not a fresh trap."""
    again = TODAY + [(99.3, 99.4, 99.1, 99.2),
                     (99.2, 99.25, 98.9, 98.95),                # 10:00 pokes the PDL again
                     (98.95, 99.4, 98.92, 99.35)]               # 10:05 closes back inside
    tape, lv = frame(bars(again)), session(bars(again))
    assert strategies.sweep_of_previous_day(tape, lv, 0.25) is None
    found = strategies.sweep_of_previous_day(tape, lv, 0.25, first_test=False)
    assert found and found["wick"] == 98.9


def test_the_next_candle_must_close_back_inside():
    stays = TODAY[:3] + [(98.95, 99.0, 98.8, 98.9)]             # closes below the PDL
    assert strategies.sweep_of_previous_day(frame(bars(stays)), session(bars(stays)),
                                            0.25) is None


def test_the_mirror_is_a_pdh_sweep():
    b = bars(mirror(TODAY), mirror(YESTERDAY))
    found = strategies.sweep_of_previous_day(frame(b), session(b), 0.25)
    assert found["direction"] == -1 and found["wick"] == pytest.approx(101.15)


def test_the_strategy_fires_a_long_call_with_a_wick_stop_and_1_to_3(only_sweep):
    setup, _ = strategies.evaluate_all("SPY", bars(), session(bars()), only_sweep)
    assert setup is not None and setup.strategy is SetupType.PD_LIQUIDITY_SWEEP
    assert setup.direction is Direction.LONG
    assert setup.pattern == "PDL Sweep — Tweezer Bottom"
    assert setup.underlying_support == pytest.approx(98.83)       # wick - 2 ticks
    risk = setup.entry_trigger - setup.underlying_support
    assert (setup.underlying_target - setup.entry_trigger) / risk >= 3.0 - 1e-9
    assert setup.key_level_source == "previous day low"


def test_the_put_mirror(only_sweep):
    b = bars(mirror(TODAY), mirror(YESTERDAY))
    setup, _ = strategies.evaluate_all("SPY", b, session(b), only_sweep)
    assert setup.direction is Direction.SHORT and setup.pattern.startswith("PDH Sweep")
    assert setup.underlying_support == pytest.approx(101.17)


def test_the_confluence_needs_rising_oi_on_a_sweep(cfg, only_sweep):
    from panaoptions.engine import confluence
    setup, _ = strategies.evaluate_all("SPY", bars(), session(bars()), only_sweep)
    up = fno.OiRead(call_oi=900, put_oi=500, call_change=400, put_change=0, basis="x")
    down = fno.OiRead(call_oi=900, put_oi=500, call_change=-50, put_change=0, basis="x")
    why, notes = confluence.check(setup, session(bars()), up, cfg, frame(bars()))
    assert why == "" and "swept the PDL 99.00" in notes[0]
    why, _ = confluence.check(setup, session(bars()), down, cfg, frame(bars()))
    assert "call OI is not rising" in why


def test_a_reversal_signal_without_the_sweep_is_refused(cfg, only_sweep):
    from panaoptions.engine import confluence
    setup, _ = strategies.evaluate_all("SPY", bars(), session(bars()), only_sweep)
    setup.strategy = SetupType.CANDLESTICK_AT_LEVEL
    flat = TODAY[:2] + [(99.4, 99.5, 99.1, 99.2), (99.2, 99.4, 99.1, 99.3)]  # no sweep
    why, _ = confluence.check(setup, session(bars(flat)),
                              fno.OiRead(900, 500, 400, 0, "x"), cfg, frame(bars(flat)))
    assert "no sweep of the previous-day low (PDL) within 0.25%" in why


# --------------------------------------------------------------------------- #
# 2. Lots
# --------------------------------------------------------------------------- #
@pytest.fixture
def india(tmp_path, monkeypatch):
    from panaoptions import config as config_mod
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    cfg = config_mod.Config(market="IN")
    # The lot arithmetic below is written against ₹3,50,000 at 20% / 25%;
    # the shipped ₹4,37,500 at 30% is tested in test_throttles.
    cfg.data["account"]["starting_capital"] = 350000.0
    cfg.data["risk"].update(max_capital_deployed_pct=20.0,
                            index_max_capital_deployed_pct=25.0,
                            max_total_deployed_pct=45.0)
    yield cfg
    markets.activate("US")


def test_hdfcbank_and_infy_cost_premium_times_their_lot(india):
    from panaoptions import verify
    assert india.lot_size("HDFCBANK") == 650 and india.lot_size("INFY") == 400
    rows = {r["symbol"]: r for r in verify.lot_dry_fire(india, ["HDFCBANK", "INFY"], 20.0)}
    assert rows["HDFCBANK"]["cost_per_lot"] == 13000.0
    assert rows["INFY"]["cost_per_lot"] == 8000.0
    # Whole lots inside the 20% (₹70,000) deployment cap AND the 2% (₹7,000)
    # risk cap at the stop — the tighter decides: 5 and 8 lots by deployment,
    # 2 and 3 by risk (0.45 delta x ₹10 to the stop x the lot).
    assert rows["HDFCBANK"]["lots_bought"] == 2 and rows["HDFCBANK"]["deployed"] == 26000.0
    assert rows["INFY"]["lots_bought"] == 3 and rows["INFY"]["deployed"] == 24000.0


def test_the_gate_and_a_spread_use_the_lot_too(india):
    from panaoptions.risk.gatekeeper import RiskGatekeeper
    leg = opt(1700, 0.45, 150.0, symbol="HDFCBANK", dte=5)      # ₹97,500 a lot
    leg.multiplier = 650
    gate = RiskGatekeeper(india).review(
        {"symbol": "HDFCBANK", "direction": "LONG", "trigger_price": 1700.0,
         "invalidation_level": 1690.0, "confidence_score": 0.7}, leg, (0.4, 0.5))
    assert any("97,500" in r for r in gate.rejected)
    short = opt(1720, 0.30, 90.0, symbol="HDFCBANK", dte=5)
    short.multiplier = 650
    assert contracts.make_spread(leg, short).cost(100) == pytest.approx(60.0 * 650)


# --------------------------------------------------------------------------- #
# 3. The opening spread allowance
# --------------------------------------------------------------------------- #
def test_twelve_percent_from_0915_to_1000_ist_for_0_to_4_dte(india):
    near, far = opt(24000, 0.45, 100, dte=2), opt(24000, 0.45, 100, dte=10)
    at = lambda h, m: datetime(2026, 9, 23, h, m, tzinfo=IST)   # noqa: E731
    assert spread_limit(india, near, at(9, 20)) == 12.0
    assert spread_limit(india, near, at(9, 59)) == 12.0
    assert spread_limit(india, near, at(10, 0)) == 7.0
    assert spread_limit(india, far, at(9, 20)) == 7.0


def test_the_picker_takes_a_ten_percent_quote_only_at_the_open(india):
    wide = opt(24000, 0.45, 100.0, width=10.0, dte=2, symbol="NIFTY")   # 10% wide
    wide.multiplier = 75
    early = contracts.choose("NIFTY", [wide], Direction.LONG, india, budget=100000,
                             now=datetime(2026, 9, 23, 9, 30, tzinfo=IST))
    late = contracts.choose("NIFTY", [wide], Direction.LONG, india, budget=100000,
                            now=datetime(2026, 9, 23, 10, 30, tzinfo=IST))
    assert early.chosen is not None and late.chosen is None


# --------------------------------------------------------------------------- #
# 4 & 5. Delta 0.25 and single-leg grace
# --------------------------------------------------------------------------- #
def test_the_fallback_reaches_0_25_delta(cfg):
    cfg.data["contracts"].update(min_dte=0, max_dte=4, max_contract_price=10.0,
                                 debit_spread={"enabled": False})
    chain = [opt(500, 0.45, 6.00), opt(508, 0.27, 2.60)]
    search = contracts.choose("SPY", chain, Direction.LONG, cfg, budget=400.0)
    assert search.tier == "secondary_delta" and abs(search.chosen.delta) == 0.27
    assert "0.25-0.39" in search.note


def test_single_leg_grace_when_the_spread_has_no_liquid_short_leg(cfg):
    # The 0.45 call is $6.00 a share — over the $5.00 per-share ceiling — but
    # its $600 lot premium fits the $700 budget; no short strike is liquid.
    cfg.data["contracts"].update(min_dte=0, max_dte=4, max_contract_price=5.0,
                                 index_max_contract_price=5.0,
                                 budget_fallback_min_delta=0)
    chain = [opt(500, 0.45, 6.00), opt(505, 0.30, 3.40, oi=2, volume=0)]
    search = contracts.choose("SPY", chain, Direction.LONG, cfg, budget=700.0)
    assert search.tier == "single_leg_grace" and search.chosen.strike == 500
    assert "SINGLE-LEG GRACE" in search.note
    # Its lot premium over the budget: no grace.
    none = contracts.choose("SPY", chain, Direction.LONG, cfg, budget=500.0)
    assert none.chosen is None and "single-leg grace" in none.note


# --------------------------------------------------------------------------- #
# Pre-open cache and exchange OI change
# --------------------------------------------------------------------------- #
def test_pdh_pdl_are_known_before_todays_first_bar():
    lv = session(bars(today=[]))
    assert (lv.previous_high, lv.previous_low) == (101.0, 99.0)


def test_nse_change_in_oi_is_read_directly(tmp_path):
    from panaoptions.data.nse import parse_chain
    payload = {"records": {"underlyingValue": 1700.0, "data": [
        {"expiryDate": "27-Oct-2026", "strikePrice": 1700,
         "CE": {"bidprice": 30, "askPrice": 31, "impliedVolatility": 20,
                "openInterest": 5000, "changeinOpenInterest": 1200,
                "totalTradedVolume": 900},
         "PE": {"bidprice": 28, "askPrice": 29, "impliedVolatility": 21,
                "openInterest": 4000, "changeinOpenInterest": -300,
                "totalTradedVolume": 700}}]}}
    chain = parse_chain(payload, "HDFCBANK", 650, now=datetime(2026, 10, 20, 10, tzinfo=IST))
    assert {c.right.value: c.oi_change for c in chain} == {"CALL": 1200, "PUT": -300}
    read = fno.record_oi("HDFCBANK", datetime(2026, 10, 20, 10, tzinfo=IST), chain)
    assert read.calls_rising is True and read.puts_rising is False
    assert "exchange" in read.basis


# --------------------------------------------------------------------------- #
# On the desk
# --------------------------------------------------------------------------- #
class SweepFeed:
    async def connect(self):
        return True

    async def close(self):
        return None

    async def quote(self, symbol):
        return {"symbol": symbol, "last_price": 99.3, "previous_close": 100.0,
                "pre_market_price": 99.6, "volume": 5_000_000}

    async def candles(self, symbol, interval="5m", include_prepost=False):
        return bars()

    async def expiries(self, symbol):
        return []

    async def chain_for_window(self, symbol, spot, min_dte, max_dte):
        return [opt(99, 0.45, 0.90, symbol=symbol, dte=2, oi=900, volume=300,
                    expiry="2026-09-25"),
                opt(101, 0.25, 0.40, symbol=symbol, dte=2, oi=900, volume=300,
                    expiry="2026-09-25")]


@pytest.fixture
def desk(only_sweep, monkeypatch, tmp_path):
    from panaoptions.app import OptionsDesk
    from panaoptions.ledger import store
    from panaoptions.models import PreMarketRead

    cfg = only_sweep
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "s.db")
    cfg.data["agents"]["enabled"] = False
    cfg.data["contracts"].update(min_dte=0, max_dte=4, max_contract_price=5.0,
                                 min_contract_price=0.10)
    cfg.data["universe"]["symbols"] = ["SPY"]
    monkeypatch.setattr(clock, "now", lambda tz: datetime(2026, 9, 23, 9, 50, tzinfo=ET))

    async def _screen(feed, cfg, now, symbols=None):
        return [PreMarketRead(symbol="SPY", passed=True)]

    monkeypatch.setattr("panaoptions.app.screen", _screen)
    return OptionsDesk(cfg=cfg, feed=SweepFeed())


def test_a_sweep_with_call_oi_building_is_traded_at_1_to_3(desk):
    fno.record_oi("SPY", datetime(2026, 9, 22, 15, 0, tzinfo=ET),
                  [opt(99, 0.45, 0.9, oi=500)])               # yesterday: 500 calls
    asyncio.run(desk.cycle())
    kinds = {e["kind"]: e for e in desk.activity.recent(200)}
    assert "PD Liquidity Sweep" in kinds["setup.fired"]["detail"]
    assert "call OI rising" in kinds["confluence.ok"]["detail"]
    [trade] = desk.ledger.open_trades.values()
    assert trade.side_tag == "LONG_CALL" and trade.strategy is SetupType.PD_LIQUIDITY_SWEEP
    pic = fno.pictures("2026-09-23")[0]
    assert pic["pdh"] == 101.0 and pic["pdl"] == 99.0 and pic["ingested_at"]


def test_the_same_sweep_with_call_oi_falling_is_refused(desk):
    fno.record_oi("SPY", datetime(2026, 9, 22, 15, 0, tzinfo=ET),
                  [opt(99, 0.45, 0.9, oi=5000)])              # 5,000 -> 1,800: unwinding
    asyncio.run(desk.cycle())
    kinds = {e["kind"]: e for e in desk.activity.recent(200)}
    assert not desk.ledger.open_trades
    assert "call OI is not rising" in kinds["confluence.refused"]["detail"]


def test_the_ingest_runs_before_the_open(desk, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda tz: datetime(2026, 9, 23, 9, 5, tzinfo=ET))
    asyncio.run(desk.cycle())
    [pic] = fno.pictures("2026-09-23")
    assert pic["ingested_at"].startswith("2026-09-23T09:05")
    assert (pic["pdh"], pic["pdl"]) == (101.0, 99.0)


# --------------------------------------------------------------------------- #
# The comparison backtest
# --------------------------------------------------------------------------- #
class History:
    """Yesterday and today (the sweep), then price runs up through 3R."""

    async def candles(self, symbol, interval="5m", include_prepost=False):
        if interval == "1d":
            start = datetime(2026, 8, 17, 20, tzinfo=UTC)
            return [Candle(ts=start + timedelta(days=i), open=100, high=101, low=99,
                           close=100 * (1.01 if i % 2 else 0.99), volume=1e7)
                    for i in range(40)]
        run = [(99.3 + 0.1 * i, 99.45 + 0.1 * i, 99.25 + 0.1 * i, 99.4 + 0.1 * i)
               for i in range(20)]
        return bars(TODAY + run)


def test_the_new_rules_trade_the_sweep_the_old_ones_never_saw(only_sweep):
    from panaoptions import backtest_spreads as bt
    only_sweep.data["contracts"].update(min_dte=0, max_dte=4, max_contract_price=10.0,
                                        index_max_contract_price=10.0)
    only_sweep.data["fno"]["confluence"]["enabled"] = True
    runs = asyncio.run(bt.compare(["SPY"], 1, History(), only_sweep,
                                  datetime(2026, 9, 24, 9, 0, tzinfo=ET)))
    before, after = runs["before"]["summary"], runs["after"]["summary"]
    assert before["setups"] == 0                  # the sweep strategy did not exist
    assert after["setups"] >= 1 and after["filled"] >= 1
    [r] = [r for r in runs["after"]["results"] if r.filled][:1]
    assert r.exit_reason == "TARGET" and r.pnl > 0
    md = bt.compare_markdown(runs, only_sweep, "compare")
    assert "| before |" in md and "| after |" in md


def test_opening_quotes_are_modelled_wider(cfg, india):
    from panaoptions import backtest_spreads as bt
    peak = float(india.get("backtest.opening_widen", 2.5))
    assert bt.opening_widen(india, datetime(2026, 9, 23, 9, 15, tzinfo=IST)) == peak > 1
    assert bt.opening_widen(india, datetime(2026, 9, 23, 10, 0, tzinfo=IST)) == 1.0
    mid = bt.opening_widen(india, datetime(2026, 9, 23, 9, 37, tzinfo=IST))
    assert 1.0 < mid < peak


def test_levels_default_session_is_empty_without_history():
    assert SessionLevels().previous_high == 0.0
