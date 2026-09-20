"""WebSocket feed — the dashboard's live wire."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.bus import bus
from app.core.logging import get_logger

log = get_logger("ws")

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    log.info("dashboard connected (%d total)", bus.subscriber_count + 1)
    try:
        engine = getattr(websocket.app.state, "engine", None)
        if engine:
            await websocket.send_text(json.dumps(
                {"topic": "system.status", "data": engine.status()}, default=str))

        async for event in bus.subscribe(replay=True):
            await websocket.send_text(json.dumps(event, default=str))
    except WebSocketDisconnect:
        log.info("dashboard disconnected")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("websocket closed: %s", exc)
