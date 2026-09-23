"""A rolling log of what the desk just did, for the dashboard to show.

The desk spends most of a session deciding NOT to trade. Without somewhere to
watch that happening, a working desk and a hung one look identical — you see
an empty position list either way. This is the window into the middle: the
cycle starting, each symbol being judged, and the reason it was passed over.

In memory only, and bounded. It is a window, not a record — the record is the
database and the journal, which survive a restart.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_EVENTS = 300


@dataclass(frozen=True)
class Event:
    ts: datetime
    kind: str
    detail: str = ""
    level: str = "info"          # info | good | warn | bad

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts.isoformat(), "time": self.ts.strftime("%H:%M:%S"),
                "kind": self.kind, "detail": self.detail, "level": self.level}


class ActivityLog:
    def __init__(self, limit: int = MAX_EVENTS) -> None:
        self._events: deque[Event] = deque(maxlen=limit)

    def add(self, kind: str, detail: str = "", level: str = "info",
            ts: datetime | None = None) -> Event:
        event = Event(ts=ts or datetime.now(), kind=kind, detail=detail,
                      level=level)
        self._events.append(event)
        return event

    def recent(self, limit: int = 60) -> list[dict[str, Any]]:
        """Newest first, which is the order a log is read in."""
        return [e.to_dict() for e in list(self._events)[-limit:]][::-1]

    def clear(self) -> None:
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)
