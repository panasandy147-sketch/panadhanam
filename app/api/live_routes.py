"""Trading Day API — arm the desk for a session, and read the day's report."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/trading-day", tags=["trading-day"])


def _engine(request: Request):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(503, "engine not started yet")
    return engine


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    return _engine(request).trading_day.status()


@router.post("/start")
async def start(request: Request) -> dict[str, Any]:
    result = await _engine(request).trading_day.start()
    if not result["armed"]:
        raise HTTPException(409, result["reason"])
    return result


@router.post("/stop")
async def stop(request: Request) -> dict[str, Any]:
    return await _engine(request).trading_day.stop()


@router.get("/report")
async def report(request: Request) -> dict[str, Any]:
    """Everything that happened this session."""
    return _engine(request).trading_day.report()
