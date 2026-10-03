"""Synthetic 5m tapes for SJK 50-200: an uptrend above the 200 EMA, a swing high,
a pullback whose low lands on the 50 EMA, then the break of that swing high
(and the mirror for shorts)."""
from __future__ import annotations

from app.strategies.sjk50_200 import ema


def long_tape(deep: bool = False, shallow: bool = False):
    """(highs, lows, closes, i_break): bar i_break is the FIRST close above
    the swing high. deep: the pullback closes below the 200 EMA. shallow:
    it never comes near the 50 EMA."""
    closes = [100 + 0.05 * t for t in range(260)]           # a steady climb
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    # the swing high: one bar pokes 0.4 above the trend
    peak = closes[-1] + 0.05
    closes.append(peak)
    highs.append(peak + 0.4)
    lows.append(peak - 0.1)
    level = highs[-1]
    # the pullback: 8 bars down
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1]
    floor = (e200 - 1.0) if deep else (e50 + 0.6 if shallow else e50)
    start = closes[-1]
    for s in range(1, 9):
        c = start - (start - floor - 0.05) * s / 8
        closes.append(c)
        highs.append(c + 0.08)
        lows.append(c - 0.05 if s < 8 else floor)            # the last bar's low = the floor
    # the bounce: up, still under the swing high, then the break
    for s in range(1, 7):
        c = floor + (level - 0.05 - floor) * s / 6
        closes.append(c)
        highs.append(min(c + 0.05, level - 0.01))
        lows.append(c - 0.08)
    closes.append(level + 0.10)                              # the first close above
    highs.append(level + 0.15)
    lows.append(level - 0.05)
    return highs, lows, closes, len(closes) - 1


def mirror(highs, lows, closes, around: float = 300.0):
    """The same tape upside down: a long setup becomes a short one."""
    return ([around - x for x in lows], [around - x for x in highs],
            [around - x for x in closes])
