"""Which market the desk trades: the US, India, or whichever is open.

Each market keeps its own books. A rupee P&L added to a dollar one means
nothing, and a weekly review mixing NIFTY with SPY answers no question, so
India's ledger, journal, reviews, audit log, watchlist and learned weights
live in their own folders:

    US    data/            journal/          (unchanged from before)
    IN    data/in/         journal/in/

`activate()` points every module that keeps a path at the market's folders.
Switching is refused while a position is open: the new market's feed could
not price it, and a switch must never abandon a trade.

Auto follows the clock. NSE trades 03:45-10:00 UTC and the US 13:30-20:00
UTC, so the sessions never overlap and one desk can take both, one at a time.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

MARKETS = ("US", "IN")
NAMES = {"US": "United States", "IN": "India (NSE)"}
# Each market's working day, in its own clock: screen from the first time,
# square off by the second. Used by Auto to decide which one is "on".
HOURS = {"US": ("America/New_York", "09:00", "16:00"),
         "IN": ("Asia/Kolkata", "09:05", "15:30")}

_home: dict[str, Any] = {}
_active = "US"


def _hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute or 0))


def in_hours(code: str, now: datetime | None = None) -> bool:
    """Is `code` inside its working day right now (weekdays only)?"""
    tz, start, end = HOURS[code]
    local = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(tz))
    if local.weekday() >= 5:
        return False
    return _hhmm(start) <= local.time().replace(tzinfo=None) < _hhmm(end)


def which_now(now: datetime | None = None) -> str | None:
    """The market whose working day it is, or None between sessions."""
    return next((m for m in MARKETS if in_hours(m, now)), None)


def active() -> str:
    return _active


def activate(code: str) -> str:
    """Point the stores at `code`'s folders. Returns the code now active."""
    global _active
    from panaoptions import watchlist
    from panaoptions.journal import store as journal_store
    from panaoptions.journal import weekly
    from panaoptions.ledger import store

    code = code.upper()
    if code not in MARKETS:
        raise ValueError(f"unknown market {code!r}; one of {', '.join(MARKETS)}")
    if _active == "US":
        # Remember the US folders as they are now, whatever set them.
        _home.update(data=store.DATA_DIR, journal=journal_store.JOURNAL_DIR,
                     watch=watchlist.STORE, daily=weekly.DAILY_DIR,
                     weekly=weekly.WEEKLY_DIR)
    if code == _active:
        return code
    if code == "US":
        data, journal = _home["data"], _home["journal"]
        watch, daily, week = _home["watch"], _home["daily"], _home["weekly"]
    else:
        sub = code.lower()
        data, journal = _home["data"] / sub, _home["journal"] / sub
        watch = data / "watchlist.json"
        daily, week = journal / "daily", journal / "weekly"
    store.DATA_DIR = data
    store._conn = None
    journal_store.JOURNAL_DIR = journal
    watchlist.STORE = watch
    weekly.DAILY_DIR, weekly.WEEKLY_DIR = daily, week
    _active = code
    return code
