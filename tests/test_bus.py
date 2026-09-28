"""The dashboard's live feed must survive a burst."""
from __future__ import annotations

import asyncio

import pytest

from app.core.bus import EventBus


@pytest.mark.asyncio
async def test_a_burst_drops_old_events_not_the_subscriber():
    """Ranking 60 symbols published ~600 analyst events at once; the queue
    filled, the subscriber was dropped, and the Activity Log went silent for
    the rest of the day while the socket stayed open."""
    bus = EventBus()
    feed = bus.subscribe(replay=False)
    first = asyncio.ensure_future(feed.__anext__())
    await asyncio.sleep(0)                                  # subscribed
    for i in range(3000):
        await bus.publish("agent.report", {"i": i})
    await first
    await bus.publish("position.update", {"event": "opened", "symbol": "ADBE"})
    assert bus.subscriber_count == 1
    seen = []
    while True:
        event = await asyncio.wait_for(feed.__anext__(), 1)
        seen.append(event)
        if event["topic"] == "position.update":
            break
    assert seen[-1]["data"]["symbol"] == "ADBE"


@pytest.mark.asyncio
async def test_a_new_page_gets_decisions_not_chatter():
    bus = EventBus()
    await bus.publish("trading_day.state", {"armed": True})
    for i in range(500):
        await bus.publish("agent.report", {"i": i})
    topics = {e["topic"] for e in bus.history()}
    assert "trading_day.state" in topics and "agent.report" not in topics
