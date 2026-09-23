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
# Two-candle range rejections
# --------------------------------------------------------------------------- #
def is_piercing_line(prev: pd.Series, bar: pd.Series) -> bool:
    """Tall red, then a lower open that closes back past the red body's midpoint.

    The textbook asks the second candle to GAP below the first's low. Intraday
    bars gap only at the open, so a strict gap rule would make this a pattern
    that fires once a day at 09:30 and never again. Opening below the previous
    CLOSE is the same idea — sellers had control at the open and lost it — and
    the midpoint close is what actually carries the signal.
    """
    body = float(prev["open"] - prev["close"])
    if prev["close"] >= prev["open"] or body <= 0:
        return False
    if bar["close"] <= bar["open"]:
        return False
    if bar["open"] > prev["close"]:
        return False                       # no rejection: it opened up
    midpoint = float(prev["close"] + body / 2)
    # Past the midpoint but NOT past the whole body — that is an engulfing,
    # which is a different pattern with a different stop.
    return bool(midpoint <= bar["close"] < prev["open"])


def is_dark_cloud_cover(prev: pd.Series, bar: pd.Series) -> bool:
    """The mirror: tall green, a higher open, then a close deep inside it."""
    body = float(prev["close"] - prev["open"])
    if prev["close"] <= prev["open"] or body <= 0:
        return False
    if bar["close"] >= bar["open"]:
        return False
    if bar["open"] < prev["close"]:
        return False
    midpoint = float(prev["open"] + body / 2)
    return bool(prev["open"] < bar["close"] <= midpoint)


# --------------------------------------------------------------------------- #
# Three-candle trend structures
# --------------------------------------------------------------------------- #
def _tall(bar: pd.Series, reference: float) -> bool:
    """A real body worth calling long, against the recent average range."""
    body = abs(float(bar["close"] - bar["open"]))
    return reference > 0 and body >= reference * 0.45


def is_three_black_crows(a: pd.Series, b: pd.Series, c: pd.Series,
                         reference: float = 0.0) -> bool:
    """Three long red bodies, each opening inside the last and closing near
    its low. Distribution in its purest form: every intraday bounce fails."""
    bars = (a, b, c)
    if any(x["close"] >= x["open"] for x in bars):
        return False
    if not (a["close"] > b["close"] > c["close"]):
        return False
    # Each opens inside the previous body...
    for prev, bar in ((a, b), (b, c)):
        if not (prev["close"] <= bar["open"] <= prev["open"]):
            return False
    # ...and closes near its low: a long lower wick is a failed push, not
    # distribution, and it is the difference between this and a capitulation.
    for bar in bars:
        rng, _body, _upper, lower = _parts(bar)
        if not rng or lower / rng > 0.25:
            return False
    return all(_tall(x, reference) for x in bars) if reference else True


def is_three_white_soldiers(a: pd.Series, b: pd.Series, c: pd.Series,
                            reference: float = 0.0) -> bool:
    """The mirror: three long green bodies closing near their highs."""
    bars = (a, b, c)
    if any(x["close"] <= x["open"] for x in bars):
        return False
    if not (a["close"] < b["close"] < c["close"]):
        return False
    for prev, bar in ((a, b), (b, c)):
        if not (prev["open"] <= bar["open"] <= prev["close"]):
            return False
    for bar in bars:
        rng, _body, upper, _lower = _parts(bar)
        if not rng or upper / rng > 0.25:
            return False
    return all(_tall(x, reference) for x in bars) if reference else True


def is_abandoned_baby_bullish(a: pd.Series, star: pd.Series,
                              c: pd.Series) -> bool:
    """An island bottom: a doji stranded below everything around it.

    The gaps are the pattern. Without both of them this is a morning star,
    which is a weaker signal with a different stop, so the overlap tests are
    strict on purpose — an isolated doji is the whole point.
    """
    if a["close"] >= a["open"] or c["close"] <= c["open"]:
        return False
    rng, body, _upper, _lower = _parts(star)
    if not rng or body / rng > 0.25:
        return False                        # the middle candle must be a doji
    if star["high"] >= a["low"]:
        return False                        # no gap down into the island
    if c["low"] <= star["high"]:
        return False                        # no gap up off it
    midpoint = float(a["close"] + (a["open"] - a["close"]) / 2)
    return bool(c["close"] >= midpoint)


def is_abandoned_baby_bearish(a: pd.Series, star: pd.Series,
                              c: pd.Series) -> bool:
    if a["close"] <= a["open"] or c["close"] >= c["open"]:
        return False
    rng, body, _upper, _lower = _parts(star)
    if not rng or body / rng > 0.25:
        return False
    if star["low"] <= a["high"]:
        return False
    if c["high"] >= star["low"]:
        return False
    midpoint = float(a["open"] + (a["close"] - a["open"]) / 2)
    return bool(c["close"] <= midpoint)


# --------------------------------------------------------------------------- #
# Four-candle
# --------------------------------------------------------------------------- #
def is_three_line_strike_bullish(a: pd.Series, b: pd.Series, c: pd.Series,
                                 d: pd.Series) -> bool:
    """Three lower closes, then one candle that takes the whole run back.

    The fourth candle opens at or below the third's low and closes above the
    FIRST candle's open — one session undoing three. Shorts who sat through
    the trend are all offside at once, and covering is what carries it.
    """
    if not (a["close"] < a["open"] and b["close"] < b["open"]
            and c["close"] < c["open"]):
        return False
    if not (a["close"] > b["close"] > c["close"]):
        return False
    if d["close"] <= d["open"]:
        return False
    return bool(d["open"] <= c["low"] and d["close"] > a["open"])


def is_three_line_strike_bearish(a: pd.Series, b: pd.Series, c: pd.Series,
                                 d: pd.Series) -> bool:
    if not (a["close"] > a["open"] and b["close"] > b["open"]
            and c["close"] > c["open"]):
        return False
    if not (a["close"] < b["close"] < c["close"]):
        return False
    if d["close"] >= d["open"]:
        return False
    return bool(d["open"] >= c["high"] and d["close"] < a["open"])


# --------------------------------------------------------------------------- #
# Which way was price going BEFORE the pattern
# --------------------------------------------------------------------------- #
def prior_trend(df: pd.DataFrame, before: int = 0, lookback: int = 10) -> int:
    """+1 up, -1 down, 0 neither, over the bars leading into the pattern.

    Several of these patterns are defined by what they interrupt: three red
    candles after a rally is distribution, and the same three in the middle of
    a range is noise. Measuring the run-in is what keeps that distinction,
    and `before` excludes the pattern's own candles from its own context.
    """
    end = len(df) - before
    window = df.iloc[max(0, end - lookback):end]
    if len(window) < 4:
        return 0
    first = float(window["close"].iloc[0])
    last = float(window["close"].iloc[-1])
    if first <= 0:
        return 0
    move = (last - first) / first
    span = float(window["high"].max() - window["low"].min())
    # The move has to be most of the range it travelled through, or it is a
    # round trip that happens to have ended higher.
    if span > 0 and abs(last - first) / span < 0.5:
        return 0
    if move >= 0.002:
        return 1
    if move <= -0.002:
        return -1
    return 0


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
    # What must have been happening BEFORE it: +1 uptrend, -1 downtrend, 0 no
    # requirement. Three red candles after a rally is distribution; the same
    # three in the middle of a range is noise. The strategy enforces this and
    # says so in writing when it refuses, because a pattern rejected for its
    # context looks identical to one that never printed.
    requires_trend: int = 0


def detect(df: pd.DataFrame, tolerance: float = 0.0015) -> Pattern | None:
    """The strongest pattern completing on the LAST bar, or None.

    Ordered by how much confirmation each one carries: a three-candle
    structure says more than a single wick, so it wins when both are present.
    """
    if len(df) < 2:
        return None
    prev, bar = df.iloc[-2], df.iloc[-1]
    # The average range of the recent tape, so "a long body" means something
    # on a $12 stock and on a $570 one.
    reference = float((df["high"] - df["low"]).tail(20).mean())

    if len(df) >= 4:
        a, b, c = df.iloc[-4], df.iloc[-3], df.iloc[-2]
        if is_three_line_strike_bullish(a, b, c, bar):
            return Pattern(
                "Bullish Three-Line Strike", True,
                float(bar["high"]) + 0.01, float(bar["low"]), bars=4,
                requires_trend=-1,
                note="Three sessions of selling undone by one. Everyone short "
                     "through the run is offside at once, and their covering "
                     "is what carries it.")
        if is_three_line_strike_bearish(a, b, c, bar):
            return Pattern(
                "Bearish Three-Line Strike", False,
                float(bar["low"]) - 0.01, float(bar["high"]), bars=4,
                requires_trend=1,
                note="One candle giving back three sessions of buying, "
                     "stranding everyone who bought the trend.")

    if len(df) >= 3:
        first, star = df.iloc[-3], df.iloc[-2]
        if is_abandoned_baby_bullish(first, star, bar):
            return Pattern(
                "Bullish Abandoned Baby", True, float(bar["high"]),
                float(star["low"]), bars=3, requires_trend=-1,
                note="An island bottom: a doji stranded below everything "
                     "around it, where sellers ran out of inventory outright.")
        if is_abandoned_baby_bearish(first, star, bar):
            return Pattern(
                "Bearish Abandoned Baby", False, float(bar["low"]),
                float(star["high"]), bars=3, requires_trend=1,
                note="An island top: buying exhausted on a stranded doji.")
        if is_three_white_soldiers(first, star, bar, reference):
            return Pattern(
                "Three White Soldiers", True, float(bar["high"]),
                float(star["open"] + (star["close"] - star["open"]) / 2),
                bars=3, requires_trend=-1,
                note="Three long green closes near their highs — systematic "
                     "accumulation rather than one spike that fades.")
        if is_three_black_crows(first, star, bar, reference):
            return Pattern(
                "Three Black Crows", False, float(bar["low"]),
                float(star["high"]), bars=3, requires_trend=1,
                note="Three long red closes near their lows. Every intraday "
                     "bounce failed; this is distribution, not a dip.")
        if is_morning_star(first, star, bar):
            return Pattern(
                "Morning Star", True, float(bar["high"]), float(star["low"]),
                bars=3, requires_trend=-1,
                note="A downtrend exhausting on the middle candle, then buyers "
                     "closing past the midpoint of the first body.")
        if is_evening_star(first, star, bar):
            return Pattern(
                "Evening Star", False, float(bar["low"]), float(star["high"]),
                bars=3, requires_trend=1,
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

    if is_piercing_line(prev, bar):
        return Pattern(
            "Piercing Line", True, float(bar["high"]), float(bar["low"]),
            bars=2, requires_trend=-1,
            note="Sellers had it at the open and lost it — the close came "
                 "back past the midpoint of the red body.")
    if is_dark_cloud_cover(prev, bar):
        return Pattern(
            "Dark Cloud Cover", False, float(bar["low"]), float(bar["high"]),
            bars=2, requires_trend=1,
            note="A push to a new high sold off into the close, deep inside "
                 "the green body it was meant to extend.")

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
