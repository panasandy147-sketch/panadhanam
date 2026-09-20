"""Market-aware time.

The desk asks "is the market open?" and "am I past the entry cutoff?". Those
questions are only meaningful in the MARKET's timezone, not the server's. A PC
in Mumbai trading US hours would otherwise compute the session from IST and
conclude the market is shut all afternoon.

Every session check in the system goes through here.
"""
from __future__ import annotations

from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.logging import get_logger

log = get_logger("clock")

_UTC = ZoneInfo("UTC")
_cache: dict[str, ZoneInfo] = {}


def zone(name: str) -> ZoneInfo:
    if name not in _cache:
        try:
            _cache[name] = ZoneInfo(name)
        except (ZoneInfoNotFoundError, KeyError):
            log.warning("unknown timezone '%s' — falling back to UTC", name)
            _cache[name] = _UTC
    return _cache[name]


def market_now(timezone: str) -> datetime:
    """Current wall-clock time in the market's own timezone."""
    return datetime.now(zone(timezone))


def parse_time(value: str, default: dtime) -> dtime:
    try:
        parts = [int(x) for x in str(value).split(":")]
        return dtime(parts[0], parts[1] if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return default


def is_trading_day(timezone: str, trading_days: list[int] | None = None) -> bool:
    days = set(trading_days if trading_days is not None else [0, 1, 2, 3, 4])
    return market_now(timezone).weekday() in days


def is_open(timezone: str, open_t: str, close_t: str,
            trading_days: list[int] | None = None) -> bool:
    if not is_trading_day(timezone, trading_days):
        return False
    now = market_now(timezone).time()
    return parse_time(open_t, dtime(9, 15)) <= now <= parse_time(close_t, dtime(15, 30))


def past(timezone: str, cutoff: str) -> bool:
    """Is the market's local clock at or past this HH:MM?"""
    now = market_now(timezone)
    target = parse_time(cutoff, dtime(23, 59))
    return (now.hour, now.minute) >= (target.hour, target.minute)


def session_phase(timezone: str, premarket: str, open_t: str, close_t: str,
                  trading_days: list[int] | None = None) -> str:
    """closed | premarket | open | postmarket | weekend, in market time."""
    if not is_trading_day(timezone, trading_days):
        return "weekend"
    now = market_now(timezone).time()
    if now < parse_time(premarket, dtime(8, 45)):
        return "closed"
    if now < parse_time(open_t, dtime(9, 15)):
        return "premarket"
    if now <= parse_time(close_t, dtime(15, 30)):
        return "open"
    return "postmarket"


def describe(timezone: str) -> dict[str, str]:
    now = market_now(timezone)
    return {
        "timezone": timezone,
        "market_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "utc_offset": now.strftime("%z"),
        "local_time": datetime.now().strftime("%H:%M:%S"),
    }
