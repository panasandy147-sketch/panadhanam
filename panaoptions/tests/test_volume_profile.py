"""Volume profile: the engine, the three strategies, and the agent confluence.

  1. POC, VAH and VAL from price/volume arrays (and from OHLCV bars, RTH only).
  2. Value Area rejection: a VAH failure buys a PUT, a VAL bounce buys a CALL,
     both targeting the POC.
  3. LVN pocket acceleration: volume gaps are found, and a shelf break that
     closes into one on RVOL >= 1.5x triggers a fast momentum entry.
  plus the POC bounce, the +0.30 confluence boost and the HVN wall.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from panaoptions import alpha
from panaoptions.agents import consensus, technical
from panaoptions.engine import indicators as ta
from panaoptions.engine.strategies import evaluate_all, localise
from panaoptions.indicators import volume_profile as vpi
from panaoptions.models import Candle, Direction, Indicators, SessionLevels, Setup, SetupType
from panaoptions.strategies import volume_profile_strategies as vps
from panaoptions.strategies.volume_profile_strategies import Bar


def _prof(prices, volumes, bin_size=1.0):
    return vpi.from_arrays(prices, volumes, bin_size)


# A bell-shaped session: value 100-104 around a 102 POC, thin tails.
BELL_PRICES = [98, 99, 100, 101, 102, 103, 104, 105, 106]
BELL_VOLUMES = [2, 5, 15, 20, 30, 18, 12, 4, 2]


# =========================================================================== #
# 1. The engine
# =========================================================================== #
def test_poc_vah_val_from_price_and_volume_arrays():
    prof = _prof([100, 101, 102, 103, 104], [10, 20, 50, 15, 5])
    assert prof.poc == 102
    # 50 at the POC; 20 below beats 15 above → 70 of 100 = 70%.
    assert (prof.val, prof.vah) == (101, 102)
    assert prof.total_volume == 100


def test_the_value_area_holds_70pct_and_grows_toward_the_heavier_side():
    prof = _prof(BELL_PRICES, BELL_VOLUMES)
    assert prof.poc == 102
    inside = sum(v for p, v in zip(BELL_PRICES, BELL_VOLUMES, strict=True)
                 if prof.val <= p <= prof.vah)
    assert inside / sum(BELL_VOLUMES) >= 0.70
    assert (prof.val, prof.vah) == (100, 103)


def test_prints_on_the_same_price_add_up():
    prof = _prof([10, 10, 11, 12, 12, 12], [1, 1, 5, 2, 2, 2])
    assert prof.poc == 12          # 6 at 12 beats 5 at 11
    assert prof.volumes == (2.0, 5.0, 6.0)


def test_bars_spread_their_volume_across_their_range():
    bars = [{"high": 101.0, "low": 100.0, "volume": 1000}] * 4 + \
           [{"high": 104.0, "low": 103.0, "volume": 100}]
    prof = vpi.from_bars(bars, bin_size=0.5)
    assert 100.0 <= prof.poc <= 101.0
    assert prof.total_volume == pytest.approx(4100)
    assert prof.val >= 100.0 and prof.vah <= 101.0


def test_only_regular_trading_hours_count():
    et = datetime(2026, 9, 23, 13, 0, tzinfo=UTC)        # 09:00 ET: pre-market
    premarket = [Candle(ts=et + timedelta(minutes=5 * i), open=90, high=91, low=89,
                        close=90, volume=1_000_000) for i in range(6)]
    bell = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)      # 09:30 ET
    session = [Candle(ts=bell + timedelta(minutes=5 * i), open=100, high=100.5,
                      low=99.5, close=100, volume=1000) for i in range(10)]
    grouped = vpi.rth_sessions(premarket + session)
    assert list(grouped.values())[0] == session
    prof = vpi.session_profiles(premarket + session)["current"]
    assert 99.5 <= prof.poc <= 100.5                      # the 90s never counted


def test_lvns_and_hvns_are_found_and_edges_are_not_pockets():
    # Two accepted areas (100-101 and 104-105) with a gap at 102-103.
    prof = _prof([99, 100, 101, 102, 103, 104, 105, 106],
                 [1, 40, 45, 2, 1, 42, 38, 1])
    assert len(prof.lvns) == 1
    pocket = prof.lvns[0]
    assert (pocket.low, pocket.high) == (101.5, 103.5)
    assert prof.lvn_at(102.4) is pocket and prof.lvn_at(100.5) is None
    assert len(prof.hvns) == 2
    # The 1-contract tails at 99 and 106 are the edges, not pockets.
    assert all(z.low > 99 and z.high < 106 for z in prof.lvns)


# =========================================================================== #
# 2. Value Area Boundary Rejection
# =========================================================================== #
PROFILE = {"prior": _prof(BELL_PRICES, BELL_VOLUMES)}     # VAL 100, POC 102, VAH 103
ATR = 1.0


def test_a_vah_rejection_buys_a_put_targeting_the_poc():
    bars = [Bar(102.6, 103.0, 102.4, 102.8), Bar(102.8, 103.1, 102.7, 103.0),
            # pokes to 103.9 over the 103 VAH, closes back at 102.7: a star
            Bar(103.0, 103.9, 102.65, 102.7)]
    found = vps.detect_value_area_rejection(bars, PROFILE, ATR)
    assert found is not None and found.direction == -1 and found.right == "PUT"
    assert found.level_name == "prior VAH" and found.target == 102
    assert found.invalidation == pytest.approx(103.9)


def test_a_bearish_engulfing_at_the_vah_counts_too():
    bars = [Bar(102.9, 103.0, 102.8, 102.95),
            Bar(102.9, 103.6, 102.85, 103.4),        # green, above the VAH
            Bar(103.5, 103.55, 102.5, 102.6)]        # engulfs it, back inside
    found = vps.detect_value_area_rejection(bars, PROFILE, ATR)
    assert found is not None and found.direction == -1
    assert "bearish engulfing" in found.confirmations[1]


def test_a_val_bounce_buys_a_call_targeting_the_poc():
    bars = [Bar(100.8, 100.9, 100.4, 100.5), Bar(100.5, 100.6, 100.2, 100.3),
            # tests 99.95 against the 100 VAL, closes back at 100.6: a hammer
            Bar(100.3, 100.65, 99.95, 100.6)]
    found = vps.detect_value_area_rejection(bars, PROFILE, ATR)
    assert found is not None and found.direction == 1 and found.right == "CALL"
    assert found.level_name == "prior VAL" and found.target == 102
    assert found.invalidation == pytest.approx(99.95)


def test_a_poke_that_holds_above_the_vah_is_not_a_rejection():
    bars = [Bar(102.8, 103.1, 102.7, 103.0), Bar(103.0, 103.9, 102.95, 103.8)]
    assert vps.detect_value_area_rejection(bars, PROFILE, ATR) is None


def test_a_doji_is_not_a_rejection():
    bars = [Bar(102.8, 103.1, 102.7, 103.0), Bar(102.9, 103.6, 102.2, 102.9)]
    assert vps.detect_value_area_rejection(bars, PROFILE, ATR) is None


# =========================================================================== #
# 3. LVN pocket acceleration
# =========================================================================== #
GAP = {"prior": _prof([99, 100, 101, 102, 103, 104, 105, 106],
                      [1, 40, 45, 2, 1, 42, 38, 1])}      # LVN 101.5-103.5


def _shelf(level: float, n: int = 3) -> list[Bar]:
    return [Bar(level - 0.1, level + 0.1, level - 0.2, level) for _ in range(n)]


def test_a_shelf_break_into_the_lvn_on_volume_buys_calls():
    bars = _shelf(101.3) + [Bar(101.3, 102.3, 101.25, 102.2, volume=3000)]
    found = vps.detect_lvn_acceleration(bars, GAP, ATR, rvol=2.4)
    assert found is not None and found.direction == 1 and found.right == "CALL"
    assert found.strategy == "lvn_acceleration"
    assert found.target == pytest.approx(103.5)            # the far edge
    assert found.invalidation == pytest.approx(101.25)     # back into the shelf


def test_a_break_down_into_the_lvn_buys_puts():
    bars = _shelf(103.7) + [Bar(103.7, 103.75, 102.6, 102.7, volume=3000)]
    found = vps.detect_lvn_acceleration(bars, GAP, ATR, rvol=1.8)
    assert found is not None and found.direction == -1
    assert found.target == pytest.approx(101.5)


def test_no_volume_no_acceleration():
    bars = _shelf(101.3) + [Bar(101.3, 102.3, 101.25, 102.2)]
    assert vps.detect_lvn_acceleration(bars, GAP, ATR, rvol=1.2) is None


def test_a_close_that_stays_on_the_shelf_is_not_a_break():
    bars = _shelf(101.0) + [Bar(101.0, 101.4, 100.9, 101.3)]
    assert vps.detect_lvn_acceleration(bars, GAP, ATR, rvol=3.0) is None


def test_relative_volume_is_measured_from_the_bars_when_not_given():
    bars = [Bar(101.3, 101.4, 101.1, 101.3, volume=1000)] * 3 + \
           [Bar(101.3, 102.3, 101.25, 102.2, volume=2000)]
    assert vps.relative_volume(bars) == pytest.approx(2.0)
    assert vps.detect_lvn_acceleration(bars, GAP, ATR) is not None


# =========================================================================== #
# POC magnet / bounce
# =========================================================================== #
def test_a_retest_of_the_poc_from_above_buys_calls():
    run = [Bar(102.2 + i * 0.3, 102.5 + i * 0.3, 102.1 + i * 0.3, 102.4 + i * 0.3)
           for i in range(6)]                                 # up to ~104
    back = [Bar(103.5, 103.6, 102.6, 102.7),
            Bar(102.4, 102.7, 101.9, 102.6)]                  # hammer at the 102 POC
    found = vps.detect_poc_bounce(run + back, PROFILE, ATR)
    assert found is not None and found.direction == 1
    assert found.level == 102 and found.target == pytest.approx(max(b.high for b in run + back[:1]))


def test_a_retest_from_below_buys_puts():
    run = [Bar(101.8 - i * 0.3, 101.9 - i * 0.3, 101.5 - i * 0.3, 101.6 - i * 0.3)
           for i in range(6)]
    back = [Bar(100.5, 101.4, 100.4, 101.3),
            Bar(101.6, 102.1, 101.55, 101.6)]                 # star at the POC
    found = vps.detect_poc_bounce(run + back, PROFILE, ATR)
    assert found is not None and found.direction == -1


# =========================================================================== #
# In the strategy pipeline, on a real two-session tape
# =========================================================================== #
BELL_UTC = datetime(2026, 9, 22, 13, 30, tzinfo=UTC)          # 09:30 ET


def _tape() -> list[Candle]:
    """A prior session accepted at 100-101 (POC ~100.5), then today tests the
    prior VAL and prints a hammer."""
    out = []
    for i in range(78):                                        # prior session
        mid = 100.5 + (0.3 if i % 3 == 0 else -0.3 if i % 3 == 1 else 0.0)
        out.append(Candle(ts=BELL_UTC + timedelta(minutes=5 * i), open=mid,
                          high=mid + 0.25, low=mid - 0.25, close=mid, volume=1000))
    today = BELL_UTC + timedelta(days=1, minutes=30)           # 10:00 ET
    path = [100.6, 100.5, 100.45, 100.4, 100.35, 100.3, 100.25, 100.2]
    for i, p in enumerate(path):
        out.append(Candle(ts=today + timedelta(minutes=5 * i), open=p + 0.03,
                          high=p + 0.08, low=p - 0.08, close=p, volume=1000))
    # The hammer: tests below the prior VAL (~100.32), closes back above it.
    out.append(Candle(ts=today + timedelta(minutes=5 * len(path)), open=100.3,
                      high=100.42, low=99.9, close=100.4, volume=1500))
    return out


def test_the_value_area_strategy_fires_in_the_pipeline(cfg):
    tape = _tape()
    df5 = localise(ta.to_frame(tape), "America/New_York")
    prof = vps.profiles_for(df5, cfg)
    assert {"prior", "current"} <= set(prof)
    setup = vps.ValueAreaRejection(cfg).evaluate("SPY", df5, None, SessionLevels())
    assert setup.triggered, setup.blockers
    assert setup.strategy is SetupType.VA_REJECTION
    assert setup.direction is Direction.LONG
    assert setup.key_level_source == "prior VAL"
    assert setup.underlying_target == pytest.approx(prof["prior"].poc)
    signal = alpha.from_setup(setup)                          # a valid five-key signal
    assert signal is not None and alpha.validate(signal.to_dict()) == []

    cfg.data["strategies"]["poc_bounce"]["enabled"] = True     # off by default since 29 Sept
    _, attempts = evaluate_all("SPY", tape, SessionLevels(), cfg)
    assert {a.strategy for a in attempts} >= {SetupType.VA_REJECTION,
                                              SetupType.LVN_ACCELERATION,
                                              SetupType.POC_BOUNCE}


# =========================================================================== #
# Confluence in the Technical agent: +0.30 at a level, HVN wall penalty/veto
# =========================================================================== #
def test_level_alignment_calls_at_val_puts_at_vah():
    assert vps.level_alignment(100.1, +1, PROFILE, ATR) == "prior VAL 100.00"
    assert vps.level_alignment(102.9, -1, PROFILE, ATR) == "prior VAH 103.00"
    assert vps.level_alignment(102.0, +1, PROFILE, ATR).startswith("prior POC")
    assert vps.level_alignment(101.4, +1, PROFILE, ATR) == ""
    assert vps.level_alignment(100.1, -1, PROFILE, ATR) == ""    # a put at the VAL


def test_an_hvn_wall_straight_ahead_is_a_veto_further_off_a_penalty():
    # HVN 103.5-105.5 ahead of a call.
    assert vps.hvn_wall(103.4, +1, GAP, ATR)[0] == "veto"
    assert vps.hvn_wall(102.8, +1, GAP, ATR)[0] == "penalty"
    assert vps.hvn_wall(101.9, +1, GAP, ATR)[0] == ""            # 1.6 ATR away
    # ...unless it is where the trade is going.
    assert vps.hvn_wall(103.4, +1, GAP, ATR, target=104.0)[0] == ""


def _setup_at(price: float, rvol: float = 2.0) -> Setup:
    return Setup(symbol="SPY", ts=datetime(2026, 9, 23, 14, 15, tzinfo=UTC),
                 direction=Direction.LONG, strategy=SetupType.ORB_VWAP, pattern="x",
                 confirmations=["a", "b"], trend_aligned=True,
                 indicators=Indicators(close=price, atr=0.3, rvol=rvol),
                 entry_trigger=price, underlying_support=price - 0.4)


def test_a_call_at_the_val_scores_030_higher(cfg):
    tape = _tape()
    val = vps.profiles_for(localise(ta.to_frame(tape), "America/New_York"),
                           cfg)["prior"].val
    at_val = _setup_at(val)
    signal = alpha.from_setup(at_val)
    with_profile = asyncio.run(technical.vote(signal, at_val, tape, cfg, 2.0))
    without = asyncio.run(technical.vote(signal, at_val, [], cfg, 2.0))
    assert any("volume profile" in r and "VAL" in r for r in with_profile.reasons)
    # The 15m trend read differs with/without the tape, so compare the boost.
    confluence = consensus.profile_confluence(signal, at_val, tape, cfg)
    assert confluence.boost == pytest.approx(0.30)
    assert with_profile.score >= without.score or with_profile.score == 1.0
