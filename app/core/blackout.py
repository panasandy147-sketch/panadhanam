"""No new entries around high-impact macro releases.

Spreads widen and prices jump in the minutes around an FOMC decision, a CPI
print or payrolls; a stop placed in that window fills wherever the market
lands. Two things start a blackout, each `minutes_before` / `minutes_after`
wide:

  * a scheduled release listed in `news_blackout.events` for the active
    market, written in that market's own time;
  * a fresh headline naming one (the keyword list), for releases nobody
    put in the calendar.

Open positions keep their stops and targets; only NEW entries wait.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core import clock


def _parse_local(text: str, tz: str) -> datetime | None:
    try:
        naive = datetime.strptime(str(text).strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=clock.zone(tz))


def reason(cfg: Any, news: list | None = None,
           now: datetime | None = None) -> str:
    """Why new entries are paused right now, or "" when they are not."""
    if not bool(cfg.get("news_blackout.enabled", True)):
        return ""
    tz = str(cfg.get("system.timezone", "Asia/Kolkata"))
    now = now or datetime.now(UTC)
    before = timedelta(minutes=float(cfg.get("news_blackout.minutes_before", 15)))
    after = timedelta(minutes=float(cfg.get("news_blackout.minutes_after", 15)))

    events = (cfg.get("news_blackout.events", {}) or {}).get(cfg.active_market) or []
    for event in events:
        at = _parse_local(event.get("at", ""), tz)
        if at and at - before <= now <= at + after:
            local = at.astimezone(clock.zone(tz))
            until = (at + after).astimezone(clock.zone(tz))
            return (f"News blackout: {event.get('name', 'macro release')} at "
                    f"{local:%H:%M} — no new entries until {until:%H:%M}")

    keywords = [k.lower() for k in (cfg.get("news_blackout.headline_keywords") or [])]
    for item in news or []:
        published = getattr(item, "published", None)
        title = str(getattr(item, "title", "") or "")
        if not published or not title:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        if not (now - after <= published <= now):
            continue
        low = title.lower()
        if any(k in low for k in keywords):
            until = (published + after).astimezone(clock.zone(tz))
            return (f"News blackout: \"{title[:70]}\" — no new entries until "
                    f"{until:%H:%M}")
    return ""
