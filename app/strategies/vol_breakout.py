"""Volatility Breakout — Larry Williams (1987 Robbins World Cup, +11,376%).

Yesterday's range says how far price can travel today. Once it has moved
`k` x that range away from today's open, the day's expansion is under way:

    LONG   the FIRST closed 5m candle above open + k x (PDH - PDL)
    SHORT  the FIRST closed 5m candle below open - k x (PDH - PDL)

with price on the same side of VWAP and the 9 EMA over the 21 for a long
(under for a short). Only the candle that crosses counts — a level crossed
an hour ago is a chase. One per side per day; entries from
`vol_breakout.minutes_after_open` after the open until `vol_breakout.to`.

The trade (app/agents/risk.py, the setup-defined path it shares with the PD
sweep): stop exactly `vol_breakout.stop_ticks` ticks beyond today's open —
back there and the expansion has failed — target risk.min_risk_reward (3R).
No room check against the previous-day high/low: a breakout is meant to run
through it.

Backtest (20 sessions to 30 Sept 2026, walk-forward, with the desk's account
rules): US at k 0.5 and the screener's ATR floor at 1.0% took the earlier
ten sessions +2.83% -> +5.26% and the judged ten +1.04% -> +4.73% at the
same 1.0% / 2.0% drawdown. India: no setting beat its rules in both halves.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

SETUP_NAME = "Volatility Breakout"


@dataclass
class Breakout:
    direction: int            # +1 long, -1 short
    level: float              # open +/- k x range, the line crossed
    day_open: float           # the invalidation: back here and it failed
    prev_range: float
    k: float
    close: float
    ts: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _local(ts: Any, tz: str):
    from zoneinfo import ZoneInfo
    try:
        return ts.astimezone(ZoneInfo(tz)) if ts.tzinfo else ts
    except Exception:                                   # noqa: BLE001
        return ts


def _minutes(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h) * 60 + int(m or 0)


def _daily_slope(daily: list[Any], tz: str, today: date, days: int) -> float | None:
    """Yesterday's close less the close `days` sessions before it."""
    closes = [float(b.close) for b in daily if _local(b.ts, tz).date() < today]
    if len(closes) <= days:
        return None
    return closes[-1] - closes[-1 - days]


def detect(bars: list[Any], prev: dict[str, Any], primary: dict[str, Any], tz: str,
           today: date, cfg: Any, swing: bool = False,
           daily: list[Any] | None = None) -> Breakout | None:
    """The breakout on the latest CLOSED 5m candle, or None.

    swing=True is the 1-4 day gold desk, as backtested on two years of hourly
    bars: the first bar whose HIGH (LOW) reaches the level today, with the
    20-day trend (swing.trend_days), no VWAP/EMA condition, swing.k."""
    g = cfg.get
    high, low = float(prev.get("high") or 0.0), float(prev.get("low") or 0.0)
    if not bars or high <= low:
        return None
    open_m = _minutes(g("system.market_open", "09:30"))
    session = [b for b in bars if _local(b.ts, tz).date() == today
               and _minutes(_local(b.ts, tz).strftime("%H:%M")) >= open_m]
    if len(session) < 2:
        return None
    bar, before = session[-1], session[-2]
    now_m = _minutes(_local(bar.ts, tz).strftime("%H:%M"))
    key = "swing" if swing else "vol_breakout"
    if now_m < open_m + int(g(f"{key}.minutes_after_open", 30)) \
            or now_m >= _minutes(g(f"{key}.to", "14:30")):
        return None
    k = float(g(f"{key}.k", 0.5))
    rng = high - low
    day_open = float(session[0].open)
    up, down = day_open + k * rng, day_open - k * rng
    close = float(bar.close)
    vwap = float(primary.get("vwap") or 0.0)
    fast, slow = float(primary.get("ema9") or 0.0), float(primary.get("ema21") or 0.0)
    if swing:
        slope = _daily_slope(daily or [], tz, today, int(g("swing.trend_days", 20)))
        if slope is None:
            return None
        earlier = session[:-1]
        crossed_up = any(b.high >= up for b in earlier)
        crossed_down = any(b.low <= down for b in earlier)
        if bar.high >= up and not crossed_up and not crossed_down and slope > 0:
            direction, level = 1, up
        elif bar.low <= down and not crossed_down and not crossed_up and slope < 0:
            direction, level = -1, down
        else:
            return None
    elif close > up >= float(before.close) and close > vwap and fast > slow:
        direction, level = 1, up
    elif close < down <= float(before.close) and close < vwap and fast < slow:
        direction, level = -1, down
    else:
        return None
    side = "above" if direction > 0 else "below"
    return Breakout(
        direction=direction, level=round(level, 4), day_open=day_open,
        prev_range=round(rng, 4), k=k, close=close, ts=bar.ts.isoformat(),
        note=((f"first 5m bar to reach today's open {day_open:,.2f} "
               f"{'+' if direction > 0 else '-'} {k:g} x yesterday's range {rng:,.2f} "
               f"= {level:,.2f}, with the 20-day trend — a 1-4 day swing")
              if swing else
              (f"first 5m close {side} today's open {day_open:,.2f} "
               f"{'+' if direction > 0 else '-'} {k:g} x yesterday's range {rng:,.2f} "
               f"= {level:,.2f} (close {close:,.2f}), {side} VWAP {vwap:,.2f}, "
               f"9 EMA {'over' if direction > 0 else 'under'} the 21")))
