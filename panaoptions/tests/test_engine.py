"""Indicators, patterns and the setup rules."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from panaoptions.data.greeks import atm_premium_estimate, delta
from panaoptions.engine import indicators as ta
from panaoptions.engine import patterns
from panaoptions.models import Candle

BASE = datetime(2026, 9, 22, 9, 30)


def _bar(o, h, low, c, v=1000.0, i=0):
    return Candle(ts=BASE + timedelta(minutes=5 * i), open=o, high=h,
                  low=low, close=c, volume=v)


# --------------------------------------------------------------------------- #
def test_vwap_resets_each_session(bars):
    two_days = bars + [Candle(ts=b.ts + timedelta(days=1), open=b.open, high=b.high,
                              low=b.low, close=b.close, volume=b.volume)
                       for b in bars]
    df = ta.to_frame(two_days)
    line = ta.vwap(df)

    day_two_first = line.iloc[len(bars)]
    assert abs(day_two_first - two_days[len(bars)].close) < 1.0, \
        "carrying yesterday's volume anchors VWAP to a price nobody trades"


def test_the_current_bar_is_excluded_from_its_own_volume_average(cfg, bars):
    spike = bars[:-1] + [Candle(ts=bars[-1].ts, open=bars[-1].open,
                                high=bars[-1].high, low=bars[-1].low,
                                close=bars[-1].close, volume=50_000.0)]
    snapshot = ta.compute(ta.to_frame(spike), cfg)
    assert snapshot.rvol > 10, \
        "a big candle must not inflate the benchmark it is measured against"


def test_resampling_five_minute_bars_into_fifteen(bars):
    df = ta.to_frame(bars)
    out = ta.resample(df, "15min")

    # 40 bars is 13 full buckets plus a partial one, and the partial bucket is
    # KEPT — that is the 15m candle currently printing, which is the one you
    # are actually trading against.
    assert len(out) == 14
    assert out.iloc[-1]["volume"] == df.iloc[39:]["volume"].sum()
    assert out.iloc[0]["open"] == df.iloc[0]["open"]
    assert out.iloc[0]["close"] == df.iloc[2]["close"]
    assert out.iloc[0]["volume"] == df.iloc[:3]["volume"].sum()


# --------------------------------------------------------------------------- #
def test_a_hammer_is_measured_against_the_range_not_the_body():
    # Measuring the wick against the BODY calls every small-bodied bar a
    # hammer. Measuring against the range is what makes the pattern mean
    # something: a long-legged doji has wicks on both sides and is rejected.
    long_legged_doji = pd.Series(
        {"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.02})
    assert not patterns.is_hammer(long_legged_doji)

    hammer = pd.Series({"open": 100.0, "high": 100.2, "low": 98.8, "close": 100.1})
    assert patterns.is_hammer(hammer)


def test_a_small_body_does_not_disqualify_a_hammer():
    # A textbook hammer's body is about 7% of its range. A minimum-body filter
    # would reject the pattern it exists to find.
    assert patterns.is_hammer(
        pd.Series({"open": 100.0, "high": 100.1, "low": 98.6, "close": 100.05}))
    # A dragonfly doji — open equals close over a long lower wick — is a
    # stronger reversal than most hammers, not a weaker one.
    assert patterns.is_hammer(
        pd.Series({"open": 100.0, "high": 100.0, "low": 98.5, "close": 100.0}))


def test_a_shooting_star_is_a_hammer_upside_down():
    star = pd.Series({"open": 100.0, "high": 101.4, "low": 99.9, "close": 99.95})
    assert patterns.is_shooting_star(star)
    assert not patterns.is_hammer(star)


def test_engulfing_needs_the_previous_body_covered():
    down = pd.Series({"open": 101.0, "high": 101.2, "low": 99.8, "close": 100.0})
    engulfs = pd.Series({"open": 99.9, "high": 101.5, "low": 99.8, "close": 101.2})
    inside = pd.Series({"open": 100.1, "high": 100.8, "low": 100.0, "close": 100.7})

    assert patterns.is_bullish_engulfing(down, engulfs)
    assert not patterns.is_bullish_engulfing(down, inside)


def test_a_zero_range_bar_is_no_pattern_at_all():
    flat = pd.Series({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0})
    assert not patterns.is_hammer(flat)
    assert not patterns.is_shooting_star(flat)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
def test_black_scholes_delta_behaves_at_the_boundaries():
    assert delta(230, 230, 10, 0.25, True) == pytest.approx(0.5, abs=0.05)
    assert delta(230, 180, 10, 0.25, True) == pytest.approx(1.0, abs=0.01)
    assert delta(230, 300, 10, 0.25, True) == pytest.approx(0.0, abs=0.01)
    # Put-call parity: call delta minus put delta is exactly 1.
    assert (delta(230, 235, 10, 0.25, True)
            - delta(230, 235, 10, 0.25, False)) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("spot,strike,dte,iv", [
    (0, 230, 10, 0.25), (230, 0, 10, 0.25), (230, 230, 0, 0.25), (230, 230, 10, 0),
])
def test_an_undefined_delta_is_zero_not_a_guess(spot, strike, dte, iv):
    assert delta(spot, strike, dte, iv, True) == 0.0


def test_the_atm_estimate_shows_why_a_hundred_dollars_is_not_enough():
    # This is the arithmetic behind the whole budget problem.
    for spot, iv in ((570, 0.14), (230, 0.25), (180, 0.50)):
        per_contract = atm_premium_estimate(spot, iv, 10) * 100
        assert per_contract > 100, \
            f"an ATM contract at spot {spot} costs ${per_contract:,.0f}"
