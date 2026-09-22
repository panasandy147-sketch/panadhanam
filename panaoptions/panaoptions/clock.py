"""Session timing, always in the market's timezone.

Every window in this app is New York time. Reading the server's clock instead
would open the entry window at 09:35 wherever the machine happens to think it
is — which on a laptop that travels is a different moment every week.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


def now(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def parse_hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute or 0))


def at_or_after(tz: str, hhmm: str, ts: datetime | None = None) -> bool:
    ts = ts or now(tz)
    return ts.timetz().replace(tzinfo=None) >= parse_hhmm(hhmm)


def within(tz: str, start: str, end: str, ts: datetime | None = None) -> bool:
    """Is the clock inside [start, end)? Weekends are never inside."""
    ts = ts or now(tz)
    if ts.weekday() >= 5:
        return False
    current = ts.timetz().replace(tzinfo=None)
    return parse_hhmm(start) <= current < parse_hhmm(end)


def session_phase(cfg, ts: datetime | None = None) -> str:
    """One of: weekend, premarket, entry_window, managing, closed."""
    tz = cfg.timezone
    ts = ts or now(tz)
    if ts.weekday() >= 5:
        return "weekend"

    current = ts.timetz().replace(tzinfo=None)
    entry_open = parse_hhmm(str(cfg.get("session.entry_open", "09:35")))
    entry_close = parse_hhmm(str(cfg.get("session.entry_close", "10:30")))
    force_exit = parse_hhmm(str(cfg.get("session.force_exit_at", "15:45")))

    if current < entry_open:
        return "premarket"
    if current < entry_close:
        return "entry_window"
    if current < force_exit:
        return "managing"
    return "closed"
