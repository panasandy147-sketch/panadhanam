"""Candle patterns, measured against the bar's RANGE rather than its body.

Measuring a wick against the body is the classic mistake: a doji has a tiny
body, so every doji looks like a hammer and the pattern never means anything.
Range is the stable denominator.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# A hammer's lower wick must be at least this share of the range...
_LONG_WICK = 0.50
# ...and the opposite wick no more than this, or it is a spinning top.
_SHORT_WICK = 0.20

# There is deliberately NO minimum body size here. A textbook hammer has a body
# around 7% of its range, so a "body must exceed 10%" filter rejects the very
# pattern it is meant to find — and a dragonfly doji, where open equals close
# above a long lower wick, is a stronger reversal signal than most hammers, not
# a weaker one. What separates a hammer from indecision is the ASYMMETRY of the
# wicks, and the two thresholds above test exactly that: a long-legged doji has
# both wicks large and fails the opposite-wick check on its own.


def _parts(bar: pd.Series) -> tuple[float, float, float, float]:
    rng = float(bar["high"] - bar["low"])
    if rng <= 0:
        return 0.0, 0.0, 0.0, 0.0
    body = abs(float(bar["close"] - bar["open"]))
    upper = float(bar["high"] - max(bar["close"], bar["open"]))
    lower = float(min(bar["close"], bar["open"]) - bar["low"])
    return rng, body, upper, lower


def is_bullish_engulfing(prev: pd.Series, bar: pd.Series) -> bool:
    return (prev["close"] < prev["open"]
            and bar["close"] > bar["open"]
            and bar["close"] >= prev["open"]
            and bar["open"] <= prev["close"])


def is_bearish_engulfing(prev: pd.Series, bar: pd.Series) -> bool:
    return (prev["close"] > prev["open"]
            and bar["close"] < bar["open"]
            and bar["close"] <= prev["open"]
            and bar["open"] >= prev["close"])


def is_hammer(bar: pd.Series) -> bool:
    rng, _body, upper, lower = _parts(bar)
    if not rng:
        return False
    return lower / rng >= _LONG_WICK and upper / rng <= _SHORT_WICK


def is_shooting_star(bar: pd.Series) -> bool:
    rng, _body, upper, lower = _parts(bar)
    if not rng:
        return False
    return upper / rng >= _LONG_WICK and lower / rng <= _SHORT_WICK


# --------------------------------------------------------------------------- #
# Multi-candle patterns
# --------------------------------------------------------------------------- #
def is_morning_star(first: pd.Series, star: pd.Series, last: pd.Series) -> bool:
    """Tall red, a small indecisive middle, then a green close past the
    midpoint of the first body. The middle candle is where the downtrend runs
    out of sellers; the third is buyers taking the level back."""
    first_range = abs(float(first["open"] - first["close"]))
    if first["close"] >= first["open"] or first_range <= 0:
        return False

    star_body = abs(float(star["close"] - star["open"]))
    if star_body > first_range * 0.5:
        return False                       # not indecision, just another leg
    if min(star["open"], star["close"]) > min(first["open"], first["close"]):
        return False                       # the star must push lower

    midpoint = float(first["close"] + first_range / 2)
    return bool(last["close"] > last["open"] and last["close"] >= midpoint)


def is_evening_star(first: pd.Series, star: pd.Series, last: pd.Series) -> bool:
    """The mirror: tall green, a stalling middle, then a red close back inside
    the first body."""
    first_range = abs(float(first["close"] - first["open"]))
    if first["close"] <= first["open"] or first_range <= 0:
        return False

    star_body = abs(float(star["close"] - star["open"]))
    if star_body > first_range * 0.5:
        return False
    if max(star["open"], star["close"]) < max(first["open"], first["close"]):
        return False                       # the star must stall at the top

    midpoint = float(first["open"] + first_range / 2)
    return bool(last["close"] < last["open"] and last["close"] <= midpoint)


def _matching(a: float, b: float, reference: float, tolerance: float) -> bool:
    """Two prices are 'the same level' within a fraction of the price.

    Exact equality almost never happens on real ticks, so an exact-match rule
    would make tweezers a pattern that exists in books and not in data.
    """
    if reference <= 0:
        return False
    return abs(a - b) <= reference * tolerance


def is_tweezer_bottom(prev: pd.Series, bar: pd.Series,
                      tolerance: float = 0.0015) -> bool:
    """Two candles rejecting the same low: red then green."""
    if not (prev["close"] < prev["open"] and bar["close"] > bar["open"]):
        return False
    return _matching(float(prev["low"]), float(bar["low"]),
                     float(bar["close"]), tolerance)


def is_tweezer_top(prev: pd.Series, bar: pd.Series,
                   tolerance: float = 0.0015) -> bool:
    """Two candles rejecting the same high: green then red."""
    if not (prev["close"] > prev["open"] and bar["close"] < bar["open"]):
        return False
    return _matching(float(prev["high"]), float(bar["high"]),
                     float(bar["close"]), tolerance)


# --------------------------------------------------------------------------- #
# What just printed, and what it implies
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Pattern:
    """A pattern on the last bar, with everything the entry rule needs.

    `trigger` and `invalidation` are prices on the UNDERLYING: a pattern is
    not an entry on its own, and the stop belongs to the structure that would
    prove it wrong — not to a percentage of the option premium.
    """
    name: str
    bullish: bool
    trigger: float          # the price that confirms it
    invalidation: float     # the price that kills it
    bars: int = 1
    note: str = ""


def detect(df: pd.DataFrame, tolerance: float = 0.0015) -> Pattern | None:
    """The strongest pattern completing on the LAST bar, or None.

    Ordered by how much confirmation each one carries: a three-candle
    structure says more than a single wick, so it wins when both are present.
    """
    if len(df) < 2:
        return None
    prev, bar = df.iloc[-2], df.iloc[-1]

    if len(df) >= 3:
        first, star = df.iloc[-3], df.iloc[-2]
        if is_morning_star(first, star, bar):
            return Pattern(
                "Morning Star", True, float(bar["high"]), float(star["low"]),
                bars=3,
                note="A downtrend exhausting on the middle candle, then buyers "
                     "closing past the midpoint of the first body.")
        if is_evening_star(first, star, bar):
            return Pattern(
                "Evening Star", False, float(bar["low"]), float(star["high"]),
                bars=3,
                note="Buying ran out at the star, and sellers closed back "
                     "inside the first body.")

    if is_bullish_engulfing(prev, bar):
        return Pattern(
            "Bullish Engulfing", True, float(bar["high"]), float(bar["low"]),
            bars=2,
            note="Aggressive buying swallowed the previous red body outright.")
    if is_bearish_engulfing(prev, bar):
        return Pattern(
            "Bearish Engulfing", False, float(bar["low"]), float(bar["high"]),
            bars=2,
            note="Sellers took complete charge, covering the whole green body.")

    if is_tweezer_bottom(prev, bar, tolerance):
        return Pattern(
            "Tweezer Bottom", True, float(bar["high"]),
            float(min(prev["low"], bar["low"])), bars=2,
            note="The same low tested and rejected on two consecutive candles.")
    if is_tweezer_top(prev, bar, tolerance):
        return Pattern(
            "Tweezer Top", False, float(bar["low"]),
            float(max(prev["high"], bar["high"])), bars=2,
            note="The same high rejected twice in a row.")

    if is_hammer(bar):
        return Pattern(
            "Hammer", True, float(bar["high"]), float(bar["low"]),
            note="Sellers drove it down and buyers took the whole move back "
                 "before the close.")
    if is_shooting_star(bar):
        return Pattern(
            "Shooting Star", False, float(bar["low"]), float(bar["high"]),
            note="A rally into the close met selling that rejected all of it.")
    return None


def detect_recent(df: pd.DataFrame, within: int = 2,
                  tolerance: float = 0.0015) -> tuple[Pattern, int] | None:
    """The most recent pattern completing within the last `within` bars.

    The entry rule is "break above the hammer's high", and that break happens
    on a LATER candle — so by the time price confirms, the pattern is already
    one or two bars back. Looking only at the last bar would mean the trigger
    can never be seen at the same time as the thing it triggers.

    Returns (pattern, bars_ago). Newest first: a fresher pattern supersedes an
    older one rather than both being live at once.
    """
    for ago in range(0, max(within, 0) + 1):
        window = df.iloc[: len(df) - ago] if ago else df
        if len(window) < 2:
            continue
        found = detect(window, tolerance)
        if found is not None:
            return found, ago
    return None


def bullish(df: pd.DataFrame) -> str:
    """The bullish pattern on the last bar, or ''. Kept for the older rules."""
    found = detect(df)
    return found.name if found and found.bullish else ""


def bearish(df: pd.DataFrame) -> str:
    found = detect(df)
    return found.name if found and not found.bullish else ""
