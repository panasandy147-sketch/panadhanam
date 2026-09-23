"""The three entry strategies, their windows, and their invalidation levels."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from panaoptions.engine import indicators as ta
from panaoptions.engine.levels import compute
from panaoptions.engine.strategies import (
    LiquiditySweepReversal,
    OpeningRangeBreakout,
    VwapEmaPullback,
    evaluate_all,
    localise,
)
from panaoptions.models import Candle, Direction, SetupType

# September in New York is UTC-4, so 13:30 UTC is the 09:30 ET bell.
BELL = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
PREMARKET = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def _bar(ts, price, volume=2000.0, high=None, low=None):
    return Candle(ts=ts, open=price, high=high if high is not None else price + 0.3,
                  low=low if low is not None else price - 0.3, close=price,
                  volume=volume)


def _session(*, premarket_prices=None, opening=(100.5, 100.55, 100.6),
             after=(), volumes=None):
    bars = []
    for i, price in enumerate(premarket_prices or [100 + i * 0.02 for i in range(18)]):
        bars.append(_bar(PREMARKET + timedelta(minutes=5 * i), price, 400.0,
                         high=price + 0.2, low=price - 0.2))
    for i, price in enumerate(opening):
        bars.append(_bar(BELL + timedelta(minutes=5 * i), price))
    for i, price in enumerate(after):
        volume = volumes[i] if volumes else 2000.0
        bars.append(_bar(BELL + timedelta(minutes=5 * (len(opening) + i)),
                         price, volume))
    return bars


# --------------------------------------------------------------------------- #
def test_windows_are_read_in_exchange_time_not_utc(cfg):
    # A UTC stamp compared against a New York window shifts everything by four
    # hours, so a 10:00 ET strategy either never fires or fires overnight.
    bars = _session(after=[100.7, 100.8, 100.9])
    utc_frame = ta.to_frame(bars)

    with pytest.raises(ValueError, match="exchange timezone"):
        OpeningRangeBreakout(cfg).in_window(utc_frame)

    local = localise(utc_frame, "America/New_York")
    assert OpeningRangeBreakout(cfg).in_window(local) is True


def test_the_opening_range_is_not_usable_before_it_has_formed(cfg):
    # Two bars is half a range, and a breakout of half a range is not a
    # breakout.
    bars = _session(opening=(100.5, 100.55))
    levels = compute(bars, "America/New_York", BELL.date())
    assert not levels.has_opening_range

    setup = OpeningRangeBreakout(cfg).evaluate(
        "SPY", localise(ta.to_frame(bars), "America/New_York"), None, levels)
    assert not setup.triggered
    assert "opening range" in setup.blockers[0]


# --------------------------------------------------------------------------- #
def test_an_orb_breakout_fires_with_volume_and_names_its_invalidation(cfg):
    bars = _session(after=[100.7, 100.9, 101.2, 101.6, 102.0, 102.4, 102.9],
                    volumes=[2000, 2000, 2000, 2000, 2000, 2000, 9000])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is not None
    assert winner.strategy is SetupType.ORB_VWAP
    assert winner.direction is Direction.LONG

    # The invalidation is the range boundary, on the UNDERLYING — not a
    # percentage of a premium that an IV shift can move on its own.
    assert winner.underlying_support == pytest.approx(levels.opening_range_high)
    assert "opening range" in winner.invalidation_note
    assert winner.underlying_target > levels.opening_range_high


def test_an_orb_breakout_without_volume_is_blocked(cfg):
    bars = _session(after=[100.7, 100.9, 101.2, 101.6, 102.0, 102.4, 102.9],
                    volumes=[2000, 2000, 2000, 2000, 2000, 2000, 50])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is None
    orb = next(a for a in attempts if a.strategy is SetupType.ORB_VWAP)
    assert any("volume" in b for b in orb.blockers)


def test_price_inside_the_range_is_no_breakout(cfg):
    bars = _session(after=[100.7, 100.6, 100.75, 100.65, 100.7, 100.6, 100.7])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is None
    orb = next(a for a in attempts if a.strategy is SetupType.ORB_VWAP)
    assert "beyond the opening range" in orb.blockers[0]


# --------------------------------------------------------------------------- #
def test_the_pullback_strategy_needs_an_established_15m_trend(cfg):
    bars = _session(after=[100.7, 100.9, 101.2])
    levels = compute(bars, "America/New_York", BELL.date())
    frame = localise(ta.to_frame(bars), "America/New_York")

    setup = VwapEmaPullback(cfg).evaluate(
        "SPY", frame, ta.resample(frame, "15min"), levels)
    assert not setup.triggered
    assert "15m" in setup.blockers[0]


# --------------------------------------------------------------------------- #
def test_a_sweep_of_the_premarket_low_that_reclaims_is_a_long(cfg):
    # Dip under the pre-market low, then reclaim it on the next candle.
    premarket = [100 + i * 0.02 for i in range(18)]
    low = min(premarket) - 0.2
    after = [100.5, 100.4, 100.3, 100.2]
    bars = _session(premarket_prices=premarket, after=after)
    # The sweep candle pokes below, the next closes back above.
    bars[-2] = _bar(bars[-2].ts, low - 0.4, 3000.0, low=low - 0.9)
    bars[-1] = _bar(bars[-1].ts, 101.2, 9000.0)
    levels = compute(bars, "America/New_York", BELL.date())

    setup = LiquiditySweepReversal(cfg).evaluate(
        "SPY", localise(ta.to_frame(bars), "America/New_York"), None, levels)

    assert setup.direction is Direction.LONG
    assert setup.strategy is SetupType.LIQUIDITY_SWEEP
    assert setup.underlying_support < levels.premarket_low, \
        "the invalidation is the sweep wick, below the level that was swept"
    assert "sweep wick" in setup.invalidation_note


def test_without_premarket_bars_the_sweep_strategy_abstains(cfg):
    bars = _session(premarket_prices=[])[0:]
    bars = [b for b in bars if b.ts >= BELL]
    levels = compute(bars, "America/New_York", BELL.date())

    setup = LiquiditySweepReversal(cfg).evaluate(
        "SPY", localise(ta.to_frame(bars), "America/New_York"), None, levels)
    assert not setup.triggered
    assert "pre-market" in setup.blockers[0]


# --------------------------------------------------------------------------- #
def test_a_disabled_strategy_never_runs(cfg):
    cfg.data["strategies"]["orb_vwap"]["enabled"] = False
    bars = _session(after=[100.7, 100.9, 101.2, 101.6, 102.0, 102.4, 102.9],
                    volumes=[2000] * 6 + [9000])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is None
    assert not any(a.strategy is SetupType.ORB_VWAP for a in attempts)


def test_every_attempt_is_reported_even_when_none_fire(cfg):
    # The rejections are what tell you whether a rule is selective or simply
    # impossible.
    bars = _session(after=[100.7, 100.6, 100.75])
    levels = compute(bars, "America/New_York", BELL.date())

    winner, attempts = evaluate_all("SPY", bars, levels, cfg)
    assert winner is None
    assert attempts, "silence with no reason is indistinguishable from a bug"
    assert all(a.blockers for a in attempts)
