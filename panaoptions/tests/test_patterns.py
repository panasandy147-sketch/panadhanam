"""The seven reversal patterns, and the location rule that makes them mean
something.

A hammer in the middle of a range is a bar with a wick. These tests hold the
two halves of the rule apart: the pattern is detected here, the level it must
form at is checked next door, and neither on its own is a trade.
"""
from __future__ import annotations

import pandas as pd
import pytest

from panaoptions.engine.levels import KeyLevel, key_levels, nearest_level, swing_levels
from panaoptions.engine.patterns import detect, detect_recent
from panaoptions.models import SessionLevels


def _df(rows) -> pd.DataFrame:
    """rows: (open, high, low, close)."""
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"]).assign(
        volume=2000.0)


# Two filler bars so every pattern has room to be the last one.
FILL = [(100, 100.4, 99.6, 100), (100, 100.4, 99.6, 100)]


# --------------------------------------------------------------------------- #
# Single-candle
# --------------------------------------------------------------------------- #
def test_a_hammer_triggers_on_its_high_and_dies_below_its_low():
    found = detect(_df(FILL + [(98.9, 99.0, 97.6, 98.85)]))
    assert found is not None
    assert found.name == "Hammer" and found.bullish
    # The entry is a break of the high; the stop is the low it rejected from.
    assert found.trigger == pytest.approx(99.0)
    assert found.invalidation == pytest.approx(97.6)


def test_a_shooting_star_is_the_mirror_and_is_bearish():
    found = detect(_df(FILL + [(101.1, 102.4, 101.0, 101.15)]))
    assert found is not None
    assert found.name == "Shooting Star" and not found.bullish
    assert found.trigger == pytest.approx(101.0)     # break of the low
    assert found.invalidation == pytest.approx(102.4)


def test_a_spinning_top_is_not_a_hammer():
    # Long wicks BOTH ways is indecision, not rejection from one side.
    assert detect(_df(FILL + [(100, 101.5, 98.5, 100.05)])) is None


def test_a_dragonfly_doji_still_counts_as_a_hammer():
    # No body at all, but the asymmetry is the signal. A "body must exceed
    # 10% of range" filter would throw away the strongest bar of the seven.
    found = detect(_df(FILL + [(99.0, 99.05, 97.5, 99.0)]))
    assert found is not None and found.name == "Hammer"


# --------------------------------------------------------------------------- #
# Two-candle
# --------------------------------------------------------------------------- #
def test_bullish_engulfing_swallows_the_previous_red_body():
    found = detect(_df(FILL + [(100.0, 100.1, 99.0, 99.1),
                               (99.0, 100.6, 98.9, 100.5)]))
    assert found is not None
    assert found.name == "Bullish Engulfing" and found.bullish
    assert found.trigger == pytest.approx(100.6)
    assert found.invalidation == pytest.approx(98.9)


def test_bearish_engulfing_covers_the_whole_green_body():
    found = detect(_df(FILL + [(99.1, 100.1, 99.0, 100.0),
                               (100.1, 100.3, 98.8, 99.0)]))
    assert found is not None
    assert found.name == "Bearish Engulfing" and not found.bullish


def test_a_tweezer_bottom_is_the_same_low_rejected_twice():
    found = detect(_df(FILL + [(99.5, 99.6, 98.00, 98.6),
                               (98.6, 99.4, 98.01, 99.3)]))
    assert found is not None
    assert found.name == "Tweezer Bottom" and found.bullish
    assert found.invalidation == pytest.approx(98.00)


def test_two_lows_that_are_merely_nearby_are_not_tweezers():
    # 1.5% apart is a different level, not the same one tested twice.
    found = detect(_df(FILL + [(99.5, 99.6, 98.0, 98.6),
                               (98.6, 99.4, 96.5, 99.3)]))
    assert found is None or found.name != "Tweezer Bottom"


# --------------------------------------------------------------------------- #
# Three-candle
# --------------------------------------------------------------------------- #
def test_a_morning_star_needs_the_middle_candle_to_be_indecision():
    found = detect(_df(FILL + [(101.0, 101.1, 98.9, 99.0),      # tall red
                               (98.8, 99.0, 98.2, 98.7),        # small star
                               (98.8, 100.4, 98.7, 100.3)]))    # past midpoint
    assert found is not None
    assert found.name == "Morning Star" and found.bars == 3
    assert found.invalidation == pytest.approx(98.2)   # the star's low


def test_a_third_candle_that_stops_short_of_the_midpoint_is_not_a_star():
    found = detect(_df(FILL + [(101.0, 101.1, 98.9, 99.0),
                               (98.8, 99.0, 98.2, 98.7),
                               (98.8, 99.4, 98.7, 99.3)]))      # short of 100.0
    assert found is None or found.name != "Morning Star"


def test_an_evening_star_is_the_mirror():
    found = detect(_df(FILL + [(99.0, 101.1, 98.9, 101.0),
                               (101.2, 101.8, 101.0, 101.3),
                               (101.2, 101.3, 99.6, 99.7)]))
    assert found is not None
    assert found.name == "Evening Star" and not found.bullish


def test_three_candles_outrank_two_when_both_are_present():
    # The same bars read as a bullish engulfing on the last two. The morning
    # star carries more confirmation, so it is the one reported.
    found = detect(_df(FILL + [(101.0, 101.1, 98.9, 99.0),
                               (98.9, 99.0, 98.2, 98.5),
                               (98.4, 100.4, 98.3, 100.3)]))
    assert found is not None and found.name == "Morning Star"


# --------------------------------------------------------------------------- #
# The trigger arrives later than the pattern
# --------------------------------------------------------------------------- #
def test_a_pattern_is_still_live_once_the_trigger_bar_has_printed():
    """Looking only at the last bar makes the rule unusable.

    The entry is "break above the hammer's high", and that break IS a later
    candle — so at the moment price confirms, the hammer is a bar back. A
    last-bar-only detector can never see both at once.
    """
    frame = _df(FILL + [(98.9, 99.0, 97.6, 98.85),          # the hammer
                        (98.9, 99.6, 98.8, 99.5)])          # the break
    assert detect(frame) is None
    found = detect_recent(frame, within=2)
    assert found is not None
    pattern, ago = found
    assert pattern.name == "Hammer" and ago == 1


def test_a_stale_pattern_falls_out_of_the_window():
    frame = _df(FILL + [(98.9, 99.0, 97.6, 98.85)]
                + [(99.0, 99.4, 98.9, 99.2)] * 3)
    assert detect_recent(frame, within=2) is None


def test_the_newest_pattern_wins_over_an_older_one():
    frame = _df(FILL + [(98.9, 99.0, 97.6, 98.85),           # hammer
                        (99.0, 100.3, 98.95, 100.2),
                        (100.2, 101.4, 100.1, 100.25)])      # shooting star
    found = detect_recent(frame, within=2)
    assert found is not None and found[0].name == "Shooting Star"
    assert found[1] == 0


# --------------------------------------------------------------------------- #
# Location: the larger half of the rule
# --------------------------------------------------------------------------- #
def test_swing_levels_pick_out_the_turns_not_every_bar():
    frame = _df([(100, 100.5, 99.5, 100), (100, 101.0, 99.8, 100.8),
                 (100.8, 102.0, 100.6, 101.8), (101.8, 102.2, 100.2, 100.4),
                 (100.4, 100.6, 99.0, 99.2), (99.2, 99.8, 98.9, 99.6),
                 (99.6, 100.9, 99.4, 100.7), (100.7, 101.2, 100.5, 101.0),
                 (101.0, 101.4, 100.8, 101.2)])
    found = swing_levels(frame, lookback=2)
    assert any(lv.kind == "resistance" and lv.price == pytest.approx(102.2)
               for lv in found)
    assert any(lv.kind == "support" and lv.price == pytest.approx(98.9)
               for lv in found)


def test_a_pattern_in_the_middle_of_the_range_is_at_no_level():
    levels = [KeyLevel(97.9, "support", "swing low"),
              KeyLevel(104.0, "resistance", "swing high")]
    assert nearest_level(100.9, levels, atr=0.5) is None
    assert nearest_level(98.0, levels, atr=0.5) is not None


def test_how_close_counts_as_at_the_level_scales_with_volatility():
    """Half a percent is 'at the level' on TSLA and miles away on SPY.

    Measuring the distance in ATR is what keeps the same rule honest on both.
    """
    levels = [KeyLevel(100.0, "support", "swing low")]
    assert nearest_level(100.4, levels, atr=0.2) is None    # quiet tape
    assert nearest_level(100.4, levels, atr=2.0) is not None  # volatile tape


def test_the_closest_level_is_the_one_reported():
    levels = [KeyLevel(99.0, "support", "VWAP"),
              KeyLevel(99.4, "support", "swing low")]
    found = nearest_level(99.35, levels, atr=2.0)
    assert found is not None and found.source == "swing low"


def test_session_levels_are_offered_alongside_the_swings():
    session = SessionLevels(premarket_high=103.0, premarket_low=97.0,
                            opening_range_high=101.0, opening_range_low=99.5)
    frame = _df([(100, 100.5, 99.5, 100)] * 7)
    sources = {lv.source for lv in key_levels(frame, session, vwap=100.2,
                                              moving_averages={"9 EMA": 100.1})}
    assert {"pre-market high", "pre-market low", "opening range high",
            "opening range low", "VWAP", "9 EMA"} <= sources
