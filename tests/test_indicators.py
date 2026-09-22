"""Indicator and pattern maths."""
from __future__ import annotations

import pandas as pd
import pytest

from app.indicators import patterns, ta


@pytest.fixture
def df(candles):
    return ta.candles_to_df(candles)


def test_ema_converges_on_a_constant_series():
    s = pd.Series([100.0] * 50)
    assert ta.ema(s, 9).iloc[-1] == pytest.approx(100.0)


def test_rsi_is_bounded_and_high_in_an_uptrend(df):
    r = ta.rsi(df["close"], 14)
    assert r.between(0, 100).all()
    assert r.iloc[-1] > 55        # the fixture is a steady uptrend


def test_atr_is_positive(df):
    assert ta.atr(df, 14).iloc[-1] > 0


def test_vwap_sits_inside_the_price_range(df):
    v = ta.vwap(df).iloc[-1]
    assert df["low"].min() <= v <= df["high"].max()


def test_volume_surge_detects_the_breakout_bar(df):
    # Last bar has 5x the volume of the rest.
    assert ta.volume_surge(df, 20) > 2.0


def test_detect_regime_reads_an_uptrend(df):
    assert ta.detect_regime(df) in {"trending_up", "volatile"}


def test_compute_all_returns_the_expected_keys(df):
    out = ta.compute_all(df, {"emas": [9, 21, 50], "rsi_period": 14, "atr_period": 14})
    for key in ("last_close", "rsi", "atr", "vwap", "ema9", "ema21", "ema50",
                "regime", "above_vwap", "volume_surge"):
        assert key in out


def test_empty_input_never_raises():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert ta.compute_all(empty) == {}
    assert patterns.scan(empty) == []
    assert ta.candles_to_df([]).empty


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
def _mk(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_bullish_engulfing_is_detected():
    df = _mk([[100, 101, 98, 98.5, 1000], [98, 103, 97.5, 102.5, 2000]])
    found, strength, _ = patterns.bullish_engulfing(df)
    assert found and strength > 0


def test_bearish_engulfing_is_detected():
    df = _mk([[100, 102, 99.5, 101.5, 1000], [102, 102.5, 97, 99, 2000]])
    found, _, _ = patterns.bearish_engulfing(df)
    assert found


def test_hammer_needs_a_long_lower_wick():
    df = _mk([[100, 100.5, 94, 100.2, 1000]])
    found, _, _ = patterns.hammer(df)
    assert found
    # A plain candle is not a hammer.
    assert patterns.hammer(_mk([[100, 103, 99, 102, 1000]]))[0] is False


def test_breakout_scores_lower_without_volume():
    base = [[100, 101, 99, 100, 100_000] for _ in range(25)]
    strong = _mk(base + [[100, 106, 100, 105, 500_000]])
    weak = _mk(base + [[100, 106, 100, 105, 40_000]])
    _, s_strong, _ = patterns.breakout(strong)
    _, s_weak, _ = patterns.breakout(weak)
    assert abs(s_strong) > abs(s_weak)


def test_scan_returns_well_formed_hits(df):
    for hit in patterns.scan(df):
        assert set(hit) == {"name", "direction", "strength", "note"}
        assert hit["direction"] in {-1, 0, 1}
        assert 0.0 <= hit["strength"] <= 1.0
