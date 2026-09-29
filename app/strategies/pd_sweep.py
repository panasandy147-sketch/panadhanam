"""Previous Day Liquidity Sweep — the failed breakout.

Stops rest just beyond yesterday's high and low. A sweep is price running
those stops and failing: the candle pierces the Previous Day High (PDH) or
Low (PDL) and CLOSES BACK INSIDE yesterday's range. Whoever bought the
breakout above the PDH (or sold the breakdown below the PDL) is now trapped,
and their exit is the fuel for the move back.

    PDH sweep  high > PDH, and PDL < close < PDH   ->  SHORT (a put, or sell)
    PDL sweep  low  < PDL, and PDL < close < PDH   ->  LONG  (a call, or buy)

Checked on the latest 5-minute candle, then the latest 15-minute one.

Confirmation — only the reversal candle approves it:
    PDH sweep: the sweep candle is a Shooting Star or a Bearish Engulfing
    PDL sweep: the sweep candle is a Hammer or a Bullish Engulfing
A sweep without one is reported but not traded.

The trade (app/agents/risk.py): stop exactly `pd_sweep.stop_ticks` (2)
ticks beyond the sweep candle's wick; target the day's VWAP or 3R, whichever
is further; no time stop — it runs to the target, the stop, or the intraday
square-off.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

SETUP_NAME = "PD Liquidity Sweep"
CONFIRMING = {"PDH": ("shooting_star", "bearish_engulfing"),
              "PDL": ("hammer", "bullish_engulfing")}


@dataclass
class Sweep:
    side: str                 # "PDH" (short) or "PDL" (long)
    direction: int            # -1 short, +1 long
    timeframe: str            # "5m" / "15m"
    level: float              # the PDH / PDL swept
    wick: float               # the sweep candle's extreme beyond the level
    close: float
    ts: str
    pattern: str = ""         # the confirming reversal candle, if any
    confirmed: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _local_date(ts: Any, tz: str) -> date:
    from zoneinfo import ZoneInfo
    try:
        return ts.astimezone(ZoneInfo(tz)).date() if ts.tzinfo else ts.date()
    except Exception:                                   # noqa: BLE001
        return ts.date()


def _confirming_pattern(bars: list[Any], side: str) -> str:
    """The reversal candle the sweep candle forms, or ''."""
    import pandas as pd

    from app.indicators import patterns
    df = pd.DataFrame([{"open": b.open, "high": b.high, "low": b.low, "close": b.close,
                        "volume": getattr(b, "volume", 0.0)} for b in bars[-25:]])
    for name in CONFIRMING[side]:
        found, _, _ = getattr(patterns, name)(df)
        if found:
            return name
    return ""


def detect(candles: dict[str, list[Any]], prev: dict[str, Any], tz: str, today: date,
           timeframes: tuple[str, ...] = ("5m", "15m")) -> Sweep | None:
    """The sweep on the latest candle of any timeframe — a confirmed one first."""
    pdh, pdl = float(prev.get("high") or 0.0), float(prev.get("low") or 0.0)
    if not pdh or not pdl or pdh <= pdl:
        return None
    unconfirmed: Sweep | None = None
    for tf in timeframes:
        bars = candles.get(tf) or []
        if not bars or _local_date(bars[-1].ts, tz) != today:
            continue
        bar = bars[-1]
        inside = pdl < bar.close < pdh
        if bar.high > pdh and inside:
            side, direction, level, wick = "PDH", -1, pdh, float(bar.high)
        elif bar.low < pdl and inside:
            side, direction, level, wick = "PDL", 1, pdl, float(bar.low)
        else:
            continue
        pattern = _confirming_pattern(bars, side)
        what = "high" if side == "PDH" else "low"
        sweep = Sweep(
            side=side, direction=direction, timeframe=tf, level=level, wick=wick,
            close=float(bar.close), ts=bar.ts.isoformat(), pattern=pattern,
            confirmed=bool(pattern),
            note=(f"{tf} candle swept the previous-day {what} {level:,.2f} (wick "
                  f"{wick:,.2f}) and closed back inside at {bar.close:,.2f}"
                  + (f" — {pattern.replace('_', ' ')} confirms the trap" if pattern
                     else " — no reversal candle, not approved")))
        if sweep.confirmed:
            return sweep
        unconfirmed = unconfirmed or sweep
    return unconfirmed
