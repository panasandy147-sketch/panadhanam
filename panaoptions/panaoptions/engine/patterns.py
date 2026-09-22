"""Candle patterns, measured against the bar's RANGE rather than its body.

Measuring a wick against the body is the classic mistake: a doji has a tiny
body, so every doji looks like a hammer and the pattern never means anything.
Range is the stable denominator.
"""
from __future__ import annotations

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


def bullish(df: pd.DataFrame) -> str:
    """The bullish pattern on the last bar, or ''."""
    if len(df) < 2:
        return ""
    prev, bar = df.iloc[-2], df.iloc[-1]
    if is_bullish_engulfing(prev, bar):
        return "Bullish Engulfing"
    if is_hammer(bar):
        return "Hammer"
    return ""


def bearish(df: pd.DataFrame) -> str:
    if len(df) < 2:
        return ""
    prev, bar = df.iloc[-2], df.iloc[-1]
    if is_bearish_engulfing(prev, bar):
        return "Bearish Engulfing"
    if is_shooting_star(bar):
        return "Shooting Star"
    return ""
