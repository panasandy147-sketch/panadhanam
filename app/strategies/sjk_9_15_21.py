"""SJK 9-15-21 — the 9 / 15 / 21 EMA Master Strategy (the user's, 5 Oct 2026).

    LONG   the EMAs fanned out upward: 9 > 15 > 21, separated (not
           intertwined, not flat). Enter on the close of the candle where that
           alignment CONFIRMS, or on a pullback whose low touches the 9 EMA
           (the 9/15 band) and closes back above it, higher than the bar
           before. Stop: below the recent swing low, or just below the 21 EMA
           when there is no swing low beneath the entry. Target: 1:rr (2).
    SHORT  the mirror: 21 > 15 > 9 fanned out downward; the confirming close,
           or a rejection after a pullback up to the 9 EMA.
    NONE   "Sideways / Choppy": the EMAs out of order, crossing repeatedly,
           squeezed together or flat. No new order.

The parts, each a function below:

  1. ema()                     the 9, 15 and 21 EMAs of the close
  2. alignment()               +1 bullish fan, -1 bearish fan, 0 no order
  3. chop_reason()             why the market is Sideways / Choppy, or ""
  4. market_state()            the state on the latest bar, for the panels
  5. entry_trigger()           the confirming close / the pullback at bar t
  6. stop_and_target()         the swing (or 21 EMA) stop and the 1:rr target
  7. detect()                  the setup on the LATEST closed bar, or None
  8. backtest() / summary()    the strategy alone over historical candles

One trade per continuous alignment: detect() fires only on the FIRST trigger
of the current run of same-order EMAs (at or after `start`, the session's
window), so the same fan-out never fires twice. A cross of the EMAs ends the
run; a new run may trade again.

detect() works on plain lists of floats, so the same function serves the
live desk, backtest() below and the tests (panaoptions carries the same code
in panaoptions/strategies/sjk_9_15_21.py; the two apps share no code).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.strategies.sjk50_200 import ema, swing_highs, swing_lows

KEY = "sjk_9_15_21"                     # the settings section
SETUP_NAME = "SJK 9-15-21 · EMA Fan"
SIDEWAYS = "Sideways / Choppy"
# Chart colours (the dashboards draw the same three lines).
COLOURS = {"fast": "#a855f7", "mid": "#3b82f6", "slow": "#6b7280"}   # purple, blue, dark grey


# --------------------------------------------------------------------------- #
# 2. Alignment
# --------------------------------------------------------------------------- #
def alignment(fast: float, mid: float, slow: float) -> int:
    """+1 when 9 > 15 > 21 (bullish fan), -1 when 21 > 15 > 9, else 0."""
    if fast > mid > slow:
        return 1
    if fast < mid < slow:
        return -1
    return 0


# --------------------------------------------------------------------------- #
# 3. The chop filter
# --------------------------------------------------------------------------- #
def chop_reason(ef: Sequence[float], em: Sequence[float], es: Sequence[float],
                closes: Sequence[float], i: int, flips: Sequence[int],
                min_sep_pct: float, chop_bars: int, max_flips: int,
                min_slope_pct: float) -> str:
    """'' when bar i trends cleanly, else why it is Sideways / Choppy.

    `flips[t]` counts the changes of EMA order up to bar t (a prefix sum),
    so the crossings in the last `chop_bars` bars cost one subtraction."""
    d = alignment(ef[i], em[i], es[i])
    if d == 0:
        return "the 9 / 15 / 21 EMAs are intertwined (no clear order)"
    lo = max(0, i - chop_bars)
    crossed = flips[i] - flips[lo]
    if crossed > max_flips:
        return f"the EMAs changed order {crossed} times in the last {chop_bars} bars"
    price = float(closes[i]) or 1.0
    gap = min_sep_pct / 100.0 * price
    if (ef[i] - em[i]) * d < gap or (em[i] - es[i]) * d < gap:
        return (f"the EMAs are squeezed together (each gap under {min_sep_pct:g}% "
                f"of the price)")
    slope = (es[i] - es[lo]) * d / price * 100.0
    if slope < min_slope_pct:
        return (f"the 21 EMA is flat ({slope:+.2f}% over {i - lo} bars, needs "
                f"{min_slope_pct:g}% its way)")
    return ""


def _flip_counts(ef, em, es) -> list[int]:
    """Prefix sum of EMA-order changes: out[t] = changes in bars 1..t."""
    out, total, prev = [0], 0, alignment(ef[0], em[0], es[0])
    for t in range(1, len(ef)):
        cur = alignment(ef[t], em[t], es[t])
        total += cur != prev
        prev = cur
        out.append(total)
    return out


# --------------------------------------------------------------------------- #
# 4. The market state (for the panels and the skip log)
# --------------------------------------------------------------------------- #
def market_state(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
                 fast: int = 9, mid: int = 15, slow: int = 21, min_sep_pct: float = 0.02,
                 chop_bars: int = 12, max_flips: int = 2, min_slope_pct: float = 0.05,
                 **_: Any) -> tuple[str, str]:
    """("Bullish fan" | "Bearish fan" | "Sideways / Choppy", why)."""
    if len(closes) < 2:
        return SIDEWAYS, "not enough bars"
    ef, em, es = ema(closes, fast), ema(closes, mid), ema(closes, slow)
    i = len(closes) - 1
    why = chop_reason(ef, em, es, closes, i, _flip_counts(ef, em, es),
                      min_sep_pct, chop_bars, max_flips, min_slope_pct)
    if why:
        return SIDEWAYS, why
    return ("Bullish fan" if alignment(ef[i], em[i], es[i]) > 0 else "Bearish fan"), ""


# --------------------------------------------------------------------------- #
# 5. The entry trigger
# --------------------------------------------------------------------------- #
def entry_trigger(t: int, d: int, trend: Sequence[int], highs, lows, closes,
                  ef, em, es, mode: str = "both") -> str:
    """'' or the kind of entry at bar t in direction d (trend[t] must be d):

    'alignment confirmed'  the first bar of a clean fan-out (trend[t-1] != d)
    'pullback to the 9/15 band'  low touched the 9 EMA, held above the 21 and
                           closed back above the 9, higher than the bar before
                           (the mirror for a short)"""
    if t < 1 or trend[t] != d:
        return ""
    if mode in ("both", "confirm") and trend[t - 1] != d:
        return "alignment confirmed"
    if mode in ("both", "pullback"):
        if d > 0 and lows[t] <= max(ef[t], em[t]) and lows[t] > es[t] \
                and closes[t] > ef[t] and closes[t] > closes[t - 1]:
            return "pullback to the 9/15 band"
        if d < 0 and highs[t] >= min(ef[t], em[t]) and highs[t] < es[t] \
                and closes[t] < ef[t] and closes[t] < closes[t - 1]:
            return "rejection at the 9/15 band"
    return ""


# --------------------------------------------------------------------------- #
# 6. Stop and target
# --------------------------------------------------------------------------- #
def stop_and_target(i: int, d: int, highs, lows, closes, es, lookback: int,
                    max_age: int, rr: float, stop_mode: str, tick: float
                    ) -> tuple[float, float, str] | None:
    """(stop, target, where the stop came from), or None when there is no
    risk to define. stop_mode 'swing' (default): the most recent confirmed
    swing low (high) of the last max_age bars beyond the entry, else the 21
    EMA; 'ema21': always just beyond the 21 EMA."""
    entry = float(closes[i])
    stop, source = None, ""
    if stop_mode != "ema21":
        start = max(0, i - max_age - lookback)
        if d > 0:
            cands = [k for k in swing_lows(lows, lookback, start) if i - max_age <= k < i
                     and lows[k] < entry]
            if cands:
                stop, source = float(lows[cands[-1]]) - tick, f"swing low {lows[cands[-1]]:,.2f}"
        else:
            cands = [k for k in swing_highs(highs, lookback, start) if i - max_age <= k < i
                     and highs[k] > entry]
            if cands:
                stop, source = float(highs[cands[-1]]) + tick, f"swing high {highs[cands[-1]]:,.2f}"
    if stop is None:
        stop = float(es[i]) - tick if d > 0 else float(es[i]) + tick
        source = f"the 21 EMA {es[i]:,.2f}"
    risk = (entry - stop) * d
    if risk <= 0:
        return None
    return round(stop, 4), round(entry + d * rr * risk, 4), source


# --------------------------------------------------------------------------- #
# 7. The setup
# --------------------------------------------------------------------------- #
def detect(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
           fast: int = 9, mid: int = 15, slow: int = 21, lookback: int = 3,
           min_sep_pct: float = 0.02, chop_bars: int = 12, max_flips: int = 2,
           min_slope_pct: float = 0.05, rr: float = 2.0, stop_mode: str = "swing",
           tick: float = 0.0, max_age: int = 24, entry_mode: str = "both",
           start: int = 0) -> dict[str, Any] | None:
    """The SJK 9-15-21 setup on the LATEST bar (index -1), or None.

    `start`: the first bar a trigger may count from (the session's window);
    an earlier trigger in the same run does not use up this run's one trade.
    Returns {direction, entry, stop, target, kind, stop_source, ema_fast,
    ema_mid, ema_slow, run_start, note}.
    """
    n = len(closes)
    if n < max(3 * slow, chop_bars + 2 * lookback + 2) or not (len(highs) == len(lows) == n):
        return None
    ef, em, es = ema(closes, fast), ema(closes, mid), ema(closes, slow)
    i = n - 1
    d = alignment(ef[i], em[i], es[i])
    if d == 0:
        return None
    # The current run of same-order EMAs (bounded: a day and a half of 5m bars).
    s = i
    while s - 1 >= max(0, i - 120) and alignment(ef[s - 1], em[s - 1], es[s - 1]) == d:
        s -= 1
    flips = _flip_counts(ef, em, es)
    trend = [0] * n
    for t in range(max(1, s - 1), i + 1):
        if not chop_reason(ef, em, es, closes, t, flips, min_sep_pct, chop_bars,
                           max_flips, min_slope_pct):
            trend[t] = alignment(ef[t], em[t], es[t])
    if trend[i] != d:
        return None                                    # Sideways / Choppy now
    first, kind = None, ""
    for t in range(max(s, start, 1), i + 1):
        kind = entry_trigger(t, d, trend, highs, lows, closes, ef, em, es, entry_mode)
        if kind:
            first = t
            break
    if first != i:
        return None                                    # nothing yet, or already fired
    levels = stop_and_target(i, d, highs, lows, closes, es, lookback, max_age, rr,
                             stop_mode, tick)
    if levels is None:
        return None
    stop, target, source = levels
    side = "LONG" if d > 0 else "SHORT"
    order = "9 > 15 > 21" if d > 0 else "21 > 15 > 9"
    return {"direction": side, "entry": float(closes[i]), "stop": stop, "target": target,
            "kind": kind, "stop_source": source, "ema_fast": ef[i], "ema_mid": em[i],
            "ema_slow": es[i], "run_start": s,
            "note": (f"EMAs fanned {order} ({ef[i]:,.2f} / {em[i]:,.2f} / {es[i]:,.2f}), "
                     f"separated and trending; {kind} at {closes[i]:,.2f}; stop at "
                     f"{source}")}


def params(cfg: Any) -> dict[str, Any]:
    """detect()'s settings from the sjk_9_15_21: section."""
    g = cfg.get
    p = f"{KEY}."
    return {"fast": int(g(p + "fast", 9)), "mid": int(g(p + "mid", 15)),
            "slow": int(g(p + "slow", 21)), "lookback": int(g(p + "swing_lookback", 3)),
            "min_sep_pct": float(g(p + "min_sep_pct", 0.02)),
            "chop_bars": int(g(p + "chop_bars", 12)), "max_flips": int(g(p + "max_flips", 2)),
            "min_slope_pct": float(g(p + "min_slope_pct", 0.05)),
            "rr": float(g(p + "rr", 2.0)), "stop_mode": str(g(p + "stop_mode", "swing")),
            "tick": float(g(p + "stop_buffer", 0.0) or 0.0),
            "max_age": int(g(p + "max_age_bars", 24)),
            "entry_mode": str(g(p + "entry_mode", "both"))}


def _minutes(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h) * 60 + int(m or 0)


def _local(ts: Any, tz: str) -> Any:
    from zoneinfo import ZoneInfo
    try:
        return ts.astimezone(ZoneInfo(tz)) if ts.tzinfo else ts
    except Exception:                                    # noqa: BLE001
        return ts


def session_start(bars: list[Any], tz: str, opens: str) -> int:
    """The index of the first bar of the latest session at or after `opens`
    (market time) — where this session's one trade per run counts from."""
    if not bars:
        return 0
    day = _local(bars[-1].ts, tz).date()
    start = _minutes(opens)
    for pos in range(len(bars) - 1, -1, -1):
        at = _local(bars[pos].ts, tz)
        if at.date() != day or at.hour * 60 + at.minute < start:
            return pos + 1
    return 0


def detect_candles(bars: list[Any], cfg: Any, tz: str) -> dict[str, Any] | None:
    """SJK 9-15-21 on the latest CLOSED 5m candle, inside sjk_9_15_21.from -
    .to on the market's clock, or None. Adds `ts` (that candle), `setup` and
    `state` (the market state)."""
    if not bars:
        return None
    at = _local(bars[-1].ts, tz)
    now_m = at.hour * 60 + at.minute
    opens = str(cfg.get(f"{KEY}.from", "09:45"))
    if not (_minutes(opens) <= now_m < _minutes(cfg.get(f"{KEY}.to", "15:45"))):
        return None
    h, lo, c = ([float(b.high) for b in bars], [float(b.low) for b in bars],
                [float(b.close) for b in bars])
    found = detect(h, lo, c, start=session_start(bars, tz, opens), **params(cfg))
    if found:
        found = {**found, "ts": bars[-1].ts.isoformat(), "setup": SETUP_NAME}
    return found


# --------------------------------------------------------------------------- #
# 8. The backtest
# --------------------------------------------------------------------------- #
def backtest(bars: list[Any], cfg: Any, tz: str, square_off: str | None = None,
             warmup: int = 63, detector: Any = None) -> list[dict[str, Any]]:
    """Walk 5m candles bar by bar (no lookahead): one position at a time,
    entered on the signal bar's close, out at the stop, the target or the
    square-off (system.square_off_time), whichever first; a bar touching both
    is a stop (pessimistic). Returns one dict per trade with its R.

    `detector`: any detect_candles(bars, cfg, tz) — SJK 9-15-21's by default;
    SJK 50-200's (warmup 210) runs through the same walk."""
    detector = detector or detect_candles
    sq = _minutes(square_off or str(cfg.get("system.square_off_time", "15:15")))
    trades: list[dict[str, Any]] = []
    pos: dict[str, Any] | None = None
    for i in range(warmup, len(bars)):
        bar = bars[i]
        at = _local(bar.ts, tz)
        if pos is not None:
            long = pos["direction"] == "LONG"
            hit_stop = bar.low <= pos["stop"] if long else bar.high >= pos["stop"]
            hit_tgt = bar.high >= pos["target"] if long else bar.low <= pos["target"]
            late = at.date() != pos["day"] or at.hour * 60 + at.minute >= sq
            exit_px, why = None, ""
            if hit_stop:
                exit_px, why = pos["stop"], "STOP"
            elif hit_tgt:
                exit_px, why = pos["target"], "TARGET"
            elif late:
                exit_px, why = float(bars[i - 1].close), "SQUARE_OFF"
            if exit_px is not None:
                sign = 1 if long else -1
                r = (exit_px - pos["entry"]) * sign / pos["risk"]
                trades.append({**{k: v for k, v in pos.items() if k != "day"},
                               "exit": exit_px, "exit_ts": bar.ts.isoformat(),
                               "outcome": why, "r": round(r, 3)})
                pos = None
            else:
                continue
        found = detector(bars[max(0, i - 599):i + 1], cfg, tz)
        if found:
            pos = {"symbol": getattr(bar, "symbol", ""), "ts": bar.ts.isoformat(),
                   "day": at.date(), "direction": found["direction"],
                   "entry": found["entry"], "stop": found["stop"], "target": found["target"],
                   "risk": abs(found["entry"] - found["stop"]),
                   "kind": found.get("kind", found.get("setup", ""))}
    return trades


def summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Trades, win rate, average and total R, and the worst run in R."""
    rs = [float(t["r"]) for t in trades]
    if not rs:
        return {"trades": 0}
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    wins = [r for r in rs if r > 0]
    return {"trades": len(rs), "win_rate": round(len(wins) / len(rs) * 100, 1),
            "avg_r": round(sum(rs) / len(rs), 3), "total_r": round(sum(rs), 2),
            "max_drawdown_r": round(dd, 2)}
