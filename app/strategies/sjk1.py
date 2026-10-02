"""SJK 1 — the 50 / 200 EMA pullback continuation (the user's strategy, 2 Oct
2026), as panadhanam trades it: the shares (or the stock's option), stop at
the pullback's swing, target at 1:sjk1.rr, sold there.

    LONG   price above the 200 EMA; a pullback down to the 50 EMA (its low
           touches or tests it) that never CLOSES below the 200 EMA; then the
           first 5m close above the swing high made before the pullback.
           Stop: the pullback's swing low. Target: entry + rr x risk.
    SHORT  the mirror.

The detector (ema, swing_highs/_lows, detect) is the same code as
panaoptions' strategies/sjk1.py — the two apps share no code, so it is
carried in both. One trade per swing point: detect() fires only on the bar
whose close FIRST crosses the swing level.

Wiring: data/market.py puts detect_candles() in ctx.indicators["sjk1"]; the
candlestick analyst reports it as a complete setup (agents/candlestick.py);
the risk desk takes its exact stop and 1:rr target (agents/risk.py,
_sweep_levels) and judges it at that rr, not 1:3; the outcome tracker sells
at the target and, if sjk1.breakeven_r is set, moves the stop to breakeven.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

SETUP_NAME = "SJK 1 · 50/200 EMA Pullback"


# --------------------------------------------------------------------------- #
# 1. The EMAs
# --------------------------------------------------------------------------- #
def ema(values: Sequence[float], n: int) -> list[float]:
    """Exponential moving average, seeded with the first value (the same
    recursion as the dashboard's chart and pandas' ewm(adjust=False))."""
    k = 2.0 / (n + 1)
    out: list[float] = []
    prev = 0.0
    for i, v in enumerate(values):
        prev = float(v) if i == 0 else float(v) * k + prev * (1 - k)
        out.append(prev)
    return out


# --------------------------------------------------------------------------- #
# 2. Swing points
# --------------------------------------------------------------------------- #
def swing_highs(highs: Sequence[float], lookback: int, start: int = 0) -> list[int]:
    """Indexes of confirmed swing highs: higher than each of the `lookback`
    bars before, and not exceeded by any of the `lookback` bars after
    (searched from `start`)."""
    out = []
    for k in range(max(lookback, start), len(highs) - lookback):
        left = max(highs[k - lookback:k])
        right = max(highs[k + 1:k + lookback + 1])
        if highs[k] > left and highs[k] >= right:
            out.append(k)
    return out


def swing_lows(lows: Sequence[float], lookback: int, start: int = 0) -> list[int]:
    """Indexes of confirmed swing lows (the mirror of swing_highs)."""
    out = []
    for k in range(max(lookback, start), len(lows) - lookback):
        left = min(lows[k - lookback:k])
        right = min(lows[k + 1:k + lookback + 1])
        if lows[k] < left and lows[k] <= right:
            out.append(k)
    return out


# --------------------------------------------------------------------------- #
# 3. The setup
# --------------------------------------------------------------------------- #
def detect(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
           fast: int = 50, slow: int = 200, lookback: int = 3,
           touch_pct: float = 0.15, max_age: int = 36, rr: float = 2.5,
           tick: float = 0.0) -> dict[str, Any] | None:
    """The SJK 1 setup on the LATEST bar (index -1), or None.

    `touch_pct`: how close (% of price) the pullback's extreme must come to
    the 50 EMA to count as a test — a low at or below 50 EMA x (1 + 0.15%)
    for a long. `max_age`: the pullback's swing must be at most this many
    bars old (a level from yesterday morning is not this pullback's).
    `tick`: an optional buffer beyond the swing for the stop (0 = at it).

    Returns {direction, entry, stop, target, trigger_level, pullback_level,
    trigger_index, pullback_index, ema_fast, ema_slow, note}.
    """
    n = len(closes)
    if n < slow + 2 * lookback + 2 or not (len(highs) == len(lows) == n):
        return None
    ef, es = ema(closes, fast), ema(closes, slow)
    i = n - 1
    # Only the recent stretch can hold this pullback and the swing before it.
    start = max(0, i - 2 * max_age - 2 * lookback)
    sh, sl = swing_highs(highs, lookback, start), swing_lows(lows, lookback, start)
    near = touch_pct / 100.0

    # ---- LONG: above the 200, pulled back to the 50, breaks the swing high
    if closes[i] > es[i]:
        lows_ = [j for j in sl if i - max_age <= j <= i - 1]
        for j in reversed(lows_):                       # the newest pullback low
            if lows[j] > ef[j] * (1 + near):
                continue                                # never reached the 50 EMA
            highs_before = [k for k in sh if j - max_age <= k < j]
            if not highs_before:
                break
            k = highs_before[-1]                        # the swing high before it
            level = highs[k]
            span = range(k, i + 1)
            if any(closes[t] < es[t] for t in span):
                break                                   # closed below the 200 EMA
            if min(lows[t] for t in range(j, i + 1)) < lows[j]:
                break                                   # the pullback low gave way
            before = [closes[t] for t in range(k + 1, i)]
            if not (closes[i] > level and all(c <= level for c in before)):
                break                                   # not the FIRST close above
            entry, stop = closes[i], lows[j] - tick
            risk = entry - stop
            if risk <= 0:
                break
            return {"direction": "LONG", "entry": entry, "stop": round(stop, 4),
                    "target": round(entry + rr * risk, 4), "trigger_level": level,
                    "pullback_level": lows[j], "trigger_index": k, "pullback_index": j,
                    "ema_fast": ef[i], "ema_slow": es[i],
                    "note": (f"above the {slow} EMA ({es[i]:,.2f}); pulled back to the "
                             f"{fast} EMA (low {lows[j]:,.2f} vs {ef[j]:,.2f}) without a "
                             f"close below the {slow}; first close {entry:,.2f} above "
                             f"the swing high {level:,.2f}")}

    # ---- SHORT: below the 200, pulled back up to the 50, breaks the swing low
    if closes[i] < es[i]:
        highs_ = [j for j in sh if i - max_age <= j <= i - 1]
        for j in reversed(highs_):
            if highs[j] < ef[j] * (1 - near):
                continue
            lows_before = [k for k in sl if j - max_age <= k < j]
            if not lows_before:
                break
            k = lows_before[-1]
            level = lows[k]
            span = range(k, i + 1)
            if any(closes[t] > es[t] for t in span):
                break                                   # closed above the 200 EMA
            if max(highs[t] for t in range(j, i + 1)) > highs[j]:
                break                                   # the pullback high gave way
            before = [closes[t] for t in range(k + 1, i)]
            if not (closes[i] < level and all(c >= level for c in before)):
                break
            entry, stop = closes[i], highs[j] + tick
            risk = stop - entry
            if risk <= 0:
                break
            return {"direction": "SHORT", "entry": entry, "stop": round(stop, 4),
                    "target": round(entry - rr * risk, 4), "trigger_level": level,
                    "pullback_level": highs[j], "trigger_index": k, "pullback_index": j,
                    "ema_fast": ef[i], "ema_slow": es[i],
                    "note": (f"below the {slow} EMA ({es[i]:,.2f}); pulled back up to "
                             f"the {fast} EMA (high {highs[j]:,.2f} vs {ef[j]:,.2f}) "
                             f"without a close above the {slow}; first close "
                             f"{entry:,.2f} below the swing low {level:,.2f}")}
    return None


def params(cfg: Any) -> dict[str, Any]:
    """detect()'s settings from the sjk1: section."""
    g = cfg.get
    return {"fast": int(g("sjk1.fast", 50)), "slow": int(g("sjk1.slow", 200)),
            "lookback": int(g("sjk1.swing_lookback", 3)),
            "touch_pct": float(g("sjk1.touch_pct", 0.15)),
            "max_age": int(g("sjk1.max_age_bars", 36)), "rr": float(g("sjk1.rr", 2.5)),
            "tick": float(g("sjk1.stop_buffer", 0.0) or 0.0)}


def _minutes(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h) * 60 + int(m or 0)


def detect_candles(bars: list[Any], cfg: Any, tz: str) -> dict[str, Any] | None:
    """SJK 1 on the latest CLOSED 5m candle, inside sjk1.from - sjk1.to on the
    market's clock, or None. Adds `ts` (that candle) and `setup`."""
    if not bars:
        return None
    from zoneinfo import ZoneInfo
    last = bars[-1].ts
    try:
        local = last.astimezone(ZoneInfo(tz)) if last.tzinfo else last
    except Exception:                                    # noqa: BLE001
        local = last
    now_m = local.hour * 60 + local.minute
    if not (_minutes(cfg.get("sjk1.from", "09:45")) <= now_m
            < _minutes(cfg.get("sjk1.to", "15:00"))):
        return None
    found = detect([float(b.high) for b in bars], [float(b.low) for b in bars],
                   [float(b.close) for b in bars], **params(cfg))
    if found:
        found = {**found, "ts": last.isoformat(), "setup": SETUP_NAME}
    return found
