"""Practice day API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/practice", tags=["practice"])


def _engine(request: Request):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(503, "engine not started yet")
    return engine


class StartRequest(BaseModel):
    trading_day: str | None = None     # YYYY-MM-DD; blank = most recent session
    symbols: list[str] | None = None
    timeframe: str = "5m"
    speed: int = 60                    # bars per minute of real time


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    return _engine(request).practice.status()


@router.post("/start")
async def start(request: Request, body: StartRequest | None = None) -> dict[str, Any]:
    body = body or StartRequest()
    engine = _engine(request)

    # The live cycle loop and a replay would fight over the same risk desk.
    was_paused = engine.paused
    engine.paused = True

    result = await engine.practice.start(
        trading_day=body.trading_day, symbols=body.symbols,
        timeframe=body.timeframe, speed=body.speed)

    if not result["started"]:
        engine.paused = was_paused
        raise HTTPException(409, result["reason"])
    return result


@router.post("/pause")
async def pause(request: Request) -> dict[str, Any]:
    return await _engine(request).practice.pause()


@router.post("/stop")
async def stop(request: Request) -> dict[str, Any]:
    engine = _engine(request)
    out = await engine.practice.stop()
    engine.paused = False          # hand the desk back to the live loop
    return out


@router.post("/speed")
async def speed(request: Request, value: int) -> dict[str, Any]:
    return {"speed": _engine(request).practice.set_speed(value)}


@router.post("/log")
async def log_to_journal(request: Request) -> dict[str, Any]:
    """Send every closed practice trade through the post-mortem engine."""
    return await _engine(request).practice.log_to_journal()
