"""The ML pipeline. The parts that matter are the ones that prevent lookahead."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from panaoptions.ml.features import FEATURE_COLUMNS, build, rsi
from panaoptions.ml.labels import triple_barrier


def _frame(closes, highs=None, lows=None, start="2026-09-22 09:30"):
    idx = pd.date_range(start, periods=len(closes), freq="5min")
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "open": closes,
        "high": closes + 0.3 if highs is None else highs,
        "low": closes - 0.3 if lows is None else lows,
        "close": closes,
        "volume": np.full(len(closes), 1000.0),
    }, index=idx)


# --------------------------------------------------------------------------- #
def test_the_last_bars_are_unlabelled_not_labelled_zero():
    # Their outcome has not happened yet. Calling an unknown outcome a failure
    # teaches the model to be pessimistic about the end of every session.
    labels = triple_barrier(_frame(np.linspace(100, 110, 60)), horizon=6)
    assert labels.tail(6).isna().all()
    assert labels.head(50).notna().all()


def test_a_bar_that_touches_both_barriers_is_scored_as_a_loss():
    # Without tick data the order is unknowable, and the optimistic reading
    # turns losers into winners across the whole training set.
    closes = np.full(20, 100.0)
    highs, lows = closes + 0.2, closes - 0.2
    highs[5] = 130.0      # target and stop both inside one bar's range
    lows[5] = 70.0
    labels = triple_barrier(_frame(closes, highs, lows), horizon=6)
    assert labels.iloc[4] == 0.0


def test_the_stop_being_hit_first_is_a_loss_even_if_the_target_comes_later():
    closes = np.full(30, 100.0)
    highs, lows = closes + 0.2, closes - 0.2
    lows[3] = 60.0        # stop on bar 3 ...
    highs[5] = 140.0      # ... target on bar 5, which must not count
    labels = triple_barrier(_frame(closes, highs, lows), horizon=6)
    assert labels.iloc[2] == 0.0


def test_the_target_counts_when_it_comes_first():
    closes = np.full(30, 100.0)
    highs, lows = closes + 0.2, closes - 0.2
    highs[3] = 140.0
    lows[5] = 60.0
    labels = triple_barrier(_frame(closes, highs, lows), horizon=6)
    assert labels.iloc[2] == 1.0


def test_nothing_inside_the_horizon_is_a_loss_not_a_gap():
    labels = triple_barrier(_frame(np.full(30, 100.0)), horizon=6)
    assert (labels.dropna() == 0.0).all()


def test_an_empty_frame_does_not_explode():
    assert triple_barrier(pd.DataFrame()).empty


# --------------------------------------------------------------------------- #
def test_every_declared_feature_is_actually_built():
    frame = build(_frame(np.linspace(100, 108, 120)))
    assert list(frame.columns) == FEATURE_COLUMNS


def test_features_never_contain_infinities():
    # A flat series makes several denominators zero; inf would poison training.
    frame = build(_frame(np.full(120, 100.0)))
    numeric = frame.select_dtypes("number")
    assert not np.isinf(numeric.to_numpy(dtype=float)).any()


def test_rsi_reports_a_pure_uptrend_as_extreme_not_neutral():
    # Zero average loss makes the ratio undefined. Filling it with 50 tells the
    # model the strongest trend in the data was perfectly balanced.
    assert rsi(pd.Series(np.linspace(100, 150, 60))).iloc[-1] == pytest.approx(100.0)
    assert rsi(pd.Series(np.linspace(150, 100, 60))).iloc[-1] < 5


def test_a_feature_row_only_uses_bars_up_to_itself():
    # Truncating the data must not change any feature value that was already
    # computable. If it does, something downstream is reading the future.
    full = build(_frame(np.linspace(100, 120, 120)))
    truncated = build(_frame(np.linspace(100, 120, 120)[:80]))

    row = FEATURE_COLUMNS
    a = full.iloc[79][row].astype(float)
    b = truncated.iloc[79][row].astype(float)
    pd.testing.assert_series_equal(a, b, check_names=False, rtol=1e-9)


def test_the_overnight_gap_uses_yesterdays_close_not_todays():
    day1 = _frame(np.full(30, 100.0), start="2026-09-21 09:30")
    day2 = _frame(np.full(30, 104.0), start="2026-09-22 09:30")
    frame = build(pd.concat([day1, day2]))

    assert pd.isna(frame["overnight_gap_pct"].iloc[0]), \
        "the first session has no previous close to gap from"
    assert frame["overnight_gap_pct"].iloc[-1] == pytest.approx(4.0, abs=0.01)


def test_minutes_into_session_is_zero_at_the_open():
    frame = build(_frame(np.linspace(100, 105, 40)))
    assert frame["minutes_into_session"].iloc[0] == 0.0
    assert frame["minutes_into_session"].iloc[6] == 30.0


# --------------------------------------------------------------------------- #
def test_the_trainer_explains_itself_when_the_packages_are_missing():
    from panaoptions.ml.train import MissingDependencies, _require
    try:
        _require()
    except MissingDependencies as exc:
        assert "requirements-ml.txt" in str(exc)
        assert "runs without them" in str(exc)


def test_a_short_history_is_refused_with_the_number_of_sessions_needed(cfg):
    pytest.importorskip("xgboost")
    from panaoptions.ml.train import dataset, walk_forward

    frame = dataset(_frame(np.linspace(100, 130, 300)), None, cfg)
    with pytest.raises(ValueError, match="sessions"):
        walk_forward(frame, cfg)
