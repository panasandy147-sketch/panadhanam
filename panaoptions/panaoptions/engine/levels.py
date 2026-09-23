"""Session reference levels: the opening range and the pre-market extremes.

Most of the strategies measure against a level rather than an indicator,
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

from dataclasses import dataclass
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


# --------------------------------------------------------------------------- #
# Key levels — where a candlestick pattern is allowed to mean something
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class KeyLevel:
    price: float
    kind: str           # "support" | "resistance"
    source: str         # what makes it a level


def swing_levels(df: pd.DataFrame, lookback: int = 2,
                 limit: int = 6) -> list[KeyLevel]:
    """Recent swing highs and lows: prices the market has already turned at.

    A swing high is a bar whose high beats `lookback` bars either side. The
    two-sided test is why it lags — a level is not a level until price has
    left it — and that lag is correct: a high that has not been rejected yet
    is just the current price.
    """
    if len(df) < lookback * 2 + 1:
        return []

    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    out: list[KeyLevel] = []
    for i in range(lookback, len(df) - lookback):
        window = slice(i - lookback, i + lookback + 1)
        if highs[i] == highs[window].max():
            out.append(KeyLevel(float(highs[i]), "resistance", "swing high"))
        if lows[i] == lows[window].min():
            out.append(KeyLevel(float(lows[i]), "support", "swing low"))
    return out[-limit:]


def key_levels(df: pd.DataFrame, session: SessionLevels,
               vwap: float = 0.0, moving_averages: dict[str, float] | None = None
               ) -> list[KeyLevel]:
    """Everything worth calling a level on this chart."""
    levels = swing_levels(df)

    if session.premarket_high:
        levels.append(KeyLevel(session.premarket_high, "resistance",
                               "pre-market high"))
    if session.premarket_low:
        levels.append(KeyLevel(session.premarket_low, "support",
                               "pre-market low"))
    if session.opening_range_high:
        levels.append(KeyLevel(session.opening_range_high, "resistance",
                               "opening range high"))
    if session.opening_range_low:
        levels.append(KeyLevel(session.opening_range_low, "support",
                               "opening range low"))
    if session.previous_close:
        levels.append(KeyLevel(session.previous_close, "support",
                               "previous close"))
    if vwap:
        levels.append(KeyLevel(vwap, "support", "VWAP"))
    for name, value in (moving_averages or {}).items():
        if value:
            levels.append(KeyLevel(float(value), "support", name))
    return levels


def nearest_level(price: float, levels: list[KeyLevel], atr: float,
                  tolerance_atr: float = 0.5) -> KeyLevel | None:
    """The level this price is sitting at, if any.

    Distance is measured in ATR rather than percent, so "at the level" means
    the same thing on a quiet stock and a volatile one. Without this gate a
    pattern in the middle of a range reads exactly like one at support, and
    the middle of a range is where they fail.
    """
    if not levels:
        return None
    reach = max(atr * tolerance_atr, price * 0.0008)
    within = [(abs(price - lv.price), lv) for lv in levels
              if abs(price - lv.price) <= reach]
    if not within:
        return None
    return min(within, key=lambda pair: pair[0])[1]
