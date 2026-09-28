"""In-process async pub/sub with a replay buffer.

The dashboard connects over a WebSocket and immediately gets recent history, so
a browser opened mid-session isn't staring at an empty screen. Swappable for
Redis pub/sub later by implementing the same `publish`/`subscribe` pair.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from app.core.logging import get_logger

log = get_logger("bus")

MAX_REPLAY = 300


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


_CHATTER = frozenset({"agent.start", "agent.report", "quote.update",
                      "cycle.start", "risk.state"})


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._replay: deque[dict[str, Any]] = deque(maxlen=MAX_REPLAY)
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, payload: Any) -> None:
        event = {
            "topic": topic,
            "ts": datetime.now().isoformat(),
            "data": json.loads(json.dumps(payload, default=_default)),
        }
        # What a freshly opened page is shown. Analyst chatter and ticks would
        # push every decision out of it within seconds of a busy cycle.
        if topic not in _CHATTER:
            self._replay.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A slow reader loses the OLDEST event, never its whole feed.
                # Dropping the subscriber here left the dashboard's socket open
                # and silent for good: after one burst (ranking 60 symbols is
                # ~600 analyst events) the Activity Log never updated again,
                # with nothing on screen saying so.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def publish_nowait(self, topic: str, payload: Any) -> None:
        """Fire-and-forget from sync code."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.publish(topic, payload))

    async def subscribe(self, replay: bool = True) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.add(q)
        try:
            if replay:
                for event in list(self._replay)[-60:]:
                    yield event
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

    def history(self, topic: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = list(self._replay)
        if topic:
            items = [e for e in items if e["topic"] == topic]
        return items[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


bus = EventBus()


# Canonical topic names — the dashboard switches on these.
class Topic:
    AGENT_START = "agent.start"
    AGENT_REPORT = "agent.report"
    CYCLE_START = "cycle.start"
    CYCLE_DONE = "cycle.done"
    SIGNAL_PROPOSED = "signal.proposed"
    SIGNAL_APPROVED = "signal.approved"
    SIGNAL_REJECTED = "signal.rejected"
    POSITION_UPDATE = "position.update"
    FOCUS = "focus.updated"
    RISK_STATE = "risk.state"
    NEWS = "news.item"
    MACRO = "macro.update"
    QUOTE = "quote.update"
    LEARNING = "learning.update"
    LOG = "system.log"
    ERROR = "system.error"
