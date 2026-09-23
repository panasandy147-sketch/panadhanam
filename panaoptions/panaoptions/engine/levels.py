"""Session reference levels: the opening range and the pre-market extremes.

Two of the three strategies measure against a level rather than an indicator,
and a level is only meaningful if it is computed from the right bars:

  * The **15-minute opening range** is the first three 5m candles of the
    regular session — 09:30, 09:35 and 09:40 ET. It is not final until 09:45,
    and asking for it before then gets nothing rather than a partial range,
    because a breakout of half a range is not a breakout.

  * The **pre-market high and low** come from bars BEFORE the opening bell.
    They need a feed request with pre/post data included; the regular-session
    tape does not contain them.
"""
from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

import pandas as pd

from panaoptions.logging import get_logger
from panaoptions.models import Candle, SessionLevels

log = get_logger("levels")

# The regular US session. The opening range is the first `_OR_BARS` 5m candles.
_SESSION_OPEN = time(9, 30)
_SESSION_CLOSE = time(16, 0)
_OR_BARS = 3


def _local(candles: list[Candle], tz: str) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame([c.model_dump() for c in candles])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(ZoneInfo(tz))
    return df.set_index("ts").sort_index()


def compute(candles: list[Candle], tz: str = "America/New_York",
            session_date=None) -> SessionLevels:
    """The day's reference levels from a pre/post-inclusive tape."""
    levels = SessionLevels()
    df = _local(candles, tz)
    if df.empty:
        return levels

    day = session_date or df.index[-1].date()
    today = df[df.index.date == day]
    if today.empty:
        return levels

    times = today.index.time
    regular = today[(times >= _SESSION_OPEN) & (times < _SESSION_CLOSE)]
    premarket = today[times < _SESSION_OPEN]

    if not premarket.empty:
        levels.premarket_high = float(premarket["high"].max())
        levels.premarket_low = float(premarket["low"].min())

    # Only once the range has actually finished forming.
    if len(regular) >= _OR_BARS:
        window = regular.iloc[:_OR_BARS]
        levels.opening_range_high = float(window["high"].max())
        levels.opening_range_low = float(window["low"].min())

    earlier = df[df.index.date < day]
    if not earlier.empty:
        levels.previous_close = float(earlier["close"].iloc[-1])

    return levels
