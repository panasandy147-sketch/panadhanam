"""A rolling log of what the desk just did, for the dashboard to show.

The desk spends most of a session deciding NOT to trade. Without somewhere to
watch that happening, a working desk and a hung one look identical — you see
an empty position list either way. This is the window into the middle: the
cycle starting, each symbol being judged, and the reason it was passed over.

TWO TIERS, and the reason matters.

Routine scanning chatter is high-volume: three symbols against four strategies
is thirteen events a minute, so a single 300-event buffer holds twenty-three
minutes of a six-and-a-half-hour session. A trade taken at 13:10 would be
evicted by 13:33 — by its own desk's scanning noise — and anyone opening the
dashboard in the afternoon would see a wall of "no setup" and conclude nothing
had happened all day. That is precisely the confusion this file exists to
prevent, so decisions live in their own buffer that chatter cannot evict.

In memory only, and bounded. It is a window, not a record — the record is the
database and the journal, which survive a restart.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# The live scanning window: what the desk is doing right now.
MAX_EVENTS = 300
# The day's decisions. Smaller volume, far longer memory — a session takes a
# handful of trades and rejects a few dozen setups, so this comfortably holds
# a whole day of them.
MAX_NOTABLE = 400

# Anything that changed the desk's mind, or the money. Scanning chatter
# (cycle.start, setup.pass) is deliberately NOT here: it is the background you
# watch to know the desk is alive, not the story of the session.
NOTABLE_KINDS = frozenset({
    "trade.open", "trade.exit", "setup.fired", "risk.refused",
    "contract.none", "contract.fallback", "graded", "halt", "screen.done", "error",
})


@dataclass(frozen=True)
class Event:
    ts: datetime
    kind: str
    detail: str = ""
    level: str = "info"          # info | good | warn | bad
    seq: int = 0                 # arrival order, so a merge can be re-sorted

    @property
    def notable(self) -> bool:
        return self.kind in NOTABLE_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts.isoformat(), "time": self.ts.strftime("%H:%M:%S"),
                "kind": self.kind, "detail": self.detail, "level": self.level,
                "notable": self.notable}


class ActivityLog:
    def __init__(self, limit: int = MAX_EVENTS,
                 notable_limit: int = MAX_NOTABLE) -> None:
        self._events: deque[Event] = deque(maxlen=limit)
        self._notable: deque[Event] = deque(maxlen=notable_limit)
        self._seq = 0

    def add(self, kind: str, detail: str = "", level: str = "info",
            ts: datetime | None = None) -> Event:
        self._seq += 1
        event = Event(ts=ts or datetime.now(), kind=kind, detail=detail,
                      level=level, seq=self._seq)
        self._events.append(event)
        if event.notable:
            self._notable.append(event)
        return event

    def recent(self, limit: int = 60, notable_only: bool = False
               ) -> list[dict[str, Any]]:
        """Newest first, which is the order a log is read in.

        Merges both tiers, so a trade from four hours ago is still here even
        though the chatter around it has long since rolled off.
        """
        if notable_only:
            chosen = list(self._notable)
        else:
            seen = {e.seq for e in self._events}
            chosen = list(self._events) + [e for e in self._notable
                                           if e.seq not in seen]
            chosen.sort(key=lambda e: e.seq)
        return [e.to_dict() for e in chosen[-limit:]][::-1]

    def highlights(self, limit: int = 60) -> list[dict[str, Any]]:
        """Just the decisions: what was taken, refused, and why."""
        return self.recent(limit, notable_only=True)

    def clear(self) -> None:
        self._events.clear()
        self._notable.clear()

    def __len__(self) -> int:
        return len(self._events)

    @property
    def notable_count(self) -> int:
        return len(self._notable)
