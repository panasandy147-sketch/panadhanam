"""Candlestick and chart-pattern detection.

Each detector returns (found, strength 0..1, note). Strength matters: a textbook
engulfing on 3x volume is not the same trade as a marginal one on thin volume.
"""
from __future__ import annotations

import pandas as pd

from app.indicators.ta import ema, volume_surge


def _body(row) -> float:
    return abs(row["close"] - row["open"])


def _range(row) -> float:
    return max(row["high"] - row["low"], 1e-9)


def _upper_wick(row) -> float:
    return row["high"] - max(row["close"], row["open"])


def _lower_wick(row) -> float:
    return min(row["close"], row["open"]) - row["low"]


def _bullish(row) -> bool:
    return row["close"] > row["open"]


# --------------------------------------------------------------------------- #
# Single / multi candle patterns
# --------------------------------------------------------------------------- #
def bullish_engulfing(df: pd.DataFrame) -> tuple[bool, float, str]:
    if len(df) < 2:
        return False, 0.0, ""
    prev, cur = df.iloc[-2], df.iloc[-1]
    ok = (not _bullish(prev) and _bullish(cur)
          and cur["close"] >= prev["open"] and cur["open"] <= prev["close"])
    if not ok:
        return False, 0.0, ""
    ratio = _body(cur) / max(_body(prev), 1e-9)
    strength = min(1.0, 0.4 + 0.2 * ratio)
    return True, round(strength, 2), f"Bullish engulfing, body {ratio:.1f}x prior"


def bearish_engulfing(df: pd.DataFrame) -> tuple[bool, float, str]:
    if len(df) < 2:
        return False, 0.0, ""
    prev, cur = df.iloc[-2], df.iloc[-1]
    ok = (_bullish(prev) and not _bullish(cur)
          and cur["close"] <= prev["open"] and cur["open"] >= prev["close"])
    if not ok:
        return False, 0.0, ""
    ratio = _body(cur) / max(_body(prev), 1e-9)
    return True, round(min(1.0, 0.4 + 0.2 * ratio), 2), f"Bearish engulfing, body {ratio:.1f}x prior"


def hammer(df: pd.DataFrame) -> tuple[bool, float, str]:
    if df.empty:
        return False, 0.0, ""
    r = df.iloc[-1]
    rng = _range(r)
    body, lower, upper = _body(r), _lower_wick(r), _upper_wick(r)
    # A hammer is a small body at the TOP of the range with a long lower wick.
    # The upper wick is judged against the candle's range, not its body — a
    # body-relative test rejects the textbook hammer, whose body is tiny.
    ok = lower >= 2 * body and lower / rng > 0.55 and upper / rng < 0.15
    if not ok:
        return False, 0.0, ""
    return True, round(min(1.0, lower / rng), 2), "Hammer — rejection of lows"


def shooting_star(df: pd.DataFrame) -> tuple[bool, float, str]:
    if df.empty:
        return False, 0.0, ""
    r = df.iloc[-1]
    rng = _range(r)
    body, lower, upper = _body(r), _lower_wick(r), _upper_wick(r)
    # Mirror image of the hammer: small body at the BOTTOM, long upper wick.
    ok = upper >= 2 * body and upper / rng > 0.55 and lower / rng < 0.15
    if not ok:
        return False, 0.0, ""
    return True, round(min(1.0, upper / rng), 2), "Shooting star — rejection of highs"


def doji(df: pd.DataFrame) -> tuple[bool, float, str]:
    if df.empty:
        return False, 0.0, ""
    r = df.iloc[-1]
    if _body(r) / _range(r) < 0.1:
        return True, 0.3, "Doji — indecision, wait for resolution"
    return False, 0.0, ""


def morning_star(df: pd.DataFrame) -> tuple[bool, float, str]:
    if len(df) < 3:
        return False, 0.0, ""
    a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    ok = (not _bullish(a) and _body(b) / _range(b) < 0.4 and _bullish(c)
          and c["close"] > (a["open"] + a["close"]) / 2)
    return (True, 0.75, "Morning star reversal") if ok else (False, 0.0, "")


def evening_star(df: pd.DataFrame) -> tuple[bool, float, str]:
    if len(df) < 3:
        return False, 0.0, ""
    a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    ok = (_bullish(a) and _body(b) / _range(b) < 0.4 and not _bullish(c)
          and c["close"] < (a["open"] + a["close"]) / 2)
    return (True, 0.75, "Evening star reversal") if ok else (False, 0.0, "")


def inside_bar(df: pd.DataFrame) -> tuple[bool, float, str]:
    if len(df) < 2:
        return False, 0.0, ""
    prev, cur = df.iloc[-2], df.iloc[-1]
    ok = cur["high"] <= prev["high"] and cur["low"] >= prev["low"]
    return (True, 0.45, "Inside bar — compression before expansion") if ok else (False, 0.0, "")


# --------------------------------------------------------------------------- #
# Structural patterns
# --------------------------------------------------------------------------- #
def flag(df: pd.DataFrame, lookback: int = 20) -> tuple[bool, float, str]:
    """Sharp impulse then a tight, shallow pullback — a continuation setup."""
    if len(df) < lookback + 5:
        return False, 0.0, ""
    window = df.tail(lookback)
    pole = window.head(lookback // 2)
    flagpart = window.tail(lookback // 2)
    pole_move = (pole["close"].iloc[-1] - pole["close"].iloc[0]) / max(pole["close"].iloc[0], 1e-9)
    flag_range = (flagpart["high"].max() - flagpart["low"].min()) / max(flagpart["close"].mean(), 1e-9)
    if abs(pole_move) > 0.004 and flag_range < abs(pole_move) * 0.6:
        direction = "Bull" if pole_move > 0 else "Bear"
        return True, 0.65, f"{direction} flag — {abs(pole_move)*100:.2f}% pole, tight consolidation"
    return False, 0.0, ""


def breakout(df: pd.DataFrame, lookback: int = 20, vol_mult: float = 1.8) -> tuple[bool, float, str]:
    """Range break WITH volume confirmation. Without volume this scores down."""
    if len(df) < lookback + 2:
        return False, 0.0, ""
    prior = df.iloc[-(lookback + 1):-1]
    cur = df.iloc[-1]
    hi, lo = prior["high"].max(), prior["low"].min()
    surge = volume_surge(df, lookback)
    if cur["close"] > hi:
        strength = 0.8 if surge >= vol_mult else 0.35
        note = (f"Broke {lookback}-bar high {hi:.2f} on {surge:.1f}x volume"
                if surge >= vol_mult else
                f"Broke {lookback}-bar high {hi:.2f} but volume only {surge:.1f}x — suspect")
        return True, strength, note
    if cur["close"] < lo:
        strength = 0.8 if surge >= vol_mult else 0.35
        note = (f"Broke {lookback}-bar low {lo:.2f} on {surge:.1f}x volume"
                if surge >= vol_mult else
                f"Broke {lookback}-bar low {lo:.2f} but volume only {surge:.1f}x — suspect")
        return True, -strength, note
    return False, 0.0, ""


def vwap_pullback(df: pd.DataFrame) -> tuple[bool, float, str]:
    """Trend intact, price returns to VWAP and holds — the classic intraday entry."""
    from app.indicators.ta import vwap as _vwap
    if len(df) < 20:
        return False, 0.0, ""
    v = _vwap(df)
    price = float(df["close"].iloc[-1])
    vwap_now = float(v.iloc[-1])
    if vwap_now <= 0:
        return False, 0.0, ""
    dist = abs(price - vwap_now) / vwap_now
    e21 = float(ema(df["close"], 21).iloc[-1])
    if dist < 0.0025:
        if price > e21:
            return True, 0.6, f"Price back at VWAP {vwap_now:.2f} holding above EMA21 — long pullback"
        return True, -0.6, f"Price back at VWAP {vwap_now:.2f} capped by EMA21 — short pullback"
    return False, 0.0, ""


# Registry of directional patterns: name -> (fn, direction_sign)
_PATTERNS = {
    "bullish_engulfing": (bullish_engulfing, +1),
    "bearish_engulfing": (bearish_engulfing, -1),
    "hammer": (hammer, +1),
    "shooting_star": (shooting_star, -1),
    "doji": (doji, 0),
    "morning_star": (morning_star, +1),
    "evening_star": (evening_star, -1),
    "inside_bar": (inside_bar, 0),
}


def scan(df: pd.DataFrame, enabled: list[str] | None = None) -> list[dict]:
    """Run every enabled detector and return a flat list of hits."""
    out: list[dict] = []
    if df.empty:
        return out
    names = enabled or list(_PATTERNS)
    for name in names:
        entry = _PATTERNS.get(name)
        if not entry:
            continue
        fn, sign = entry
        found, strength, note = fn(df)
        if found:
            out.append({"name": name, "direction": sign,
                        "strength": abs(strength), "note": note})

    if "flag" in names or enabled is None:
        found, strength, note = flag(df)
        if found:
            sign = 1 if "Bull" in note else -1
            out.append({"name": "flag", "direction": sign, "strength": abs(strength), "note": note})

    found, strength, note = breakout(df)
    if found:
        out.append({"name": "breakout", "direction": 1 if strength > 0 else -1,
                    "strength": abs(strength), "note": note})

    found, strength, note = vwap_pullback(df)
    if found:
        out.append({"name": "vwap_pullback", "direction": 1 if strength > 0 else -1,
                    "strength": abs(strength), "note": note})
    return out
