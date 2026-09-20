"""REST API. Everything the dashboard needs, plus manual controls."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.bus import bus
from app.core.config import get_config, reload_config
from app.storage import db

router = APIRouter(prefix="/api", tags=["api"])


def _engine(request: Request):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(503, "engine not started yet")
    return engine


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    return _engine(request).status()


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    engine = getattr(request.app.state, "engine", None)
    return {
        "ok": True,
        "engine": bool(engine and engine.running),
        "broker": engine.broker.health() if engine else None,
        "subscribers": bus.subscriber_count,
    }


@router.get("/config")
async def get_config_view() -> dict[str, Any]:
    cfg = get_config()
    return {"settings": cfg.settings, "agents": cfg.agents, "universe": cfg.universe}


@router.post("/config/reload")
async def reload_cfg(request: Request) -> dict[str, Any]:
    """Hot-reload config/*.yaml and rebuild the agent roster — no restart needed."""
    cfg = reload_config()
    engine = getattr(request.app.state, "engine", None)
    if engine:
        engine.desk.reload()
    return {"reloaded": True, "analysts": cfg.enabled_analysts()}


# --------------------------------------------------------------------------- #
# Engine control
# --------------------------------------------------------------------------- #
@router.post("/engine/start")
async def start_engine(request: Request) -> dict[str, Any]:
    await _engine(request).start()
    return {"running": True}


@router.post("/engine/stop")
async def stop_engine(request: Request) -> dict[str, Any]:
    await _engine(request).stop()
    return {"running": False}


@router.post("/engine/pause")
async def pause_engine(request: Request, paused: bool = True) -> dict[str, Any]:
    engine = _engine(request)
    engine.paused = paused
    return {"paused": engine.paused}


class CycleRequest(BaseModel):
    symbols: list[str] | None = None


@router.post("/cycle/run")
async def run_cycle(request: Request, body: CycleRequest | None = None) -> dict[str, Any]:
    """Run one analysis cycle on demand — handy outside market hours."""
    engine = _engine(request)
    results = await engine.run_cycle((body.symbols if body else None) or None)
    return {"results": results}


@router.post("/premarket/scan")
async def premarket(request: Request) -> dict[str, Any]:
    return await _engine(request).run_premarket_scan()


# --------------------------------------------------------------------------- #
# Signals & positions
# --------------------------------------------------------------------------- #
@router.get("/signals")
async def signals(limit: int = 50, symbol: str | None = None,
                  status: str | None = None) -> dict[str, Any]:
    return {"signals": db.recent_signals(limit=limit, symbol=symbol, status=status)}


@router.get("/signals/{signal_id}")
async def signal_detail(signal_id: str) -> dict[str, Any]:
    signal = db.get_signal(signal_id)
    if not signal:
        raise HTTPException(404, "signal not found")
    return {"signal": signal, "reports": db.reports_for_signal(signal_id)}


@router.get("/positions")
async def positions(request: Request) -> dict[str, Any]:
    engine = _engine(request)
    return {
        "open_signals": db.open_signals(),
        "broker_positions": await engine.broker.get_positions(),
    }


@router.get("/stats")
async def stats() -> dict[str, Any]:
    return db.stats_summary()


# --------------------------------------------------------------------------- #
# Market data for the charts
# --------------------------------------------------------------------------- #
@router.get("/market/{symbol}/candles")
async def candles(request: Request, symbol: str, timeframe: str = "5m",
                  count: int = 200) -> dict[str, Any]:
    engine = _engine(request)
    data = await engine.broker.get_candles(symbol, timeframe, count)
    return {"symbol": symbol, "timeframe": timeframe,
            "candles": [{"time": int(c.ts.timestamp()), "open": c.open, "high": c.high,
                         "low": c.low, "close": c.close, "volume": c.volume} for c in data]}


@router.get("/market/{symbol}/quote")
async def quote(request: Request, symbol: str) -> dict[str, Any]:
    q = await _engine(request).broker.get_quote(symbol)
    if not q:
        raise HTTPException(404, "quote unavailable")
    return q.model_dump(mode="json")


@router.get("/market/{symbol}/chain")
async def chain(request: Request, symbol: str, expiry: str | None = None) -> dict[str, Any]:
    engine = _engine(request)
    c = await engine.broker.get_option_chain(symbol, expiry)
    if not c:
        raise HTTPException(404, "option chain unavailable for this symbol")
    from app.indicators.derivatives import analyse
    q = await engine.broker.get_quote(symbol)
    metrics = analyse(c, q.change_pct if q else 0.0, get_config().get("derivatives", {}) or {})
    return {"chain": c.model_dump(mode="json"), "metrics": metrics}


@router.get("/watchlist")
async def watchlist() -> dict[str, Any]:
    return {"watchlist": get_config().watchlist()}


@router.get("/news")
async def news(limit: int = 30) -> dict[str, Any]:
    return {"news": [e["data"] for e in bus.history("news.item", limit)]}


@router.get("/macro")
async def macro(request: Request) -> dict[str, Any]:
    snap = _engine(request).macro.last
    return snap.model_dump(mode="json") if snap else {}


# --------------------------------------------------------------------------- #
# Risk tools
# --------------------------------------------------------------------------- #
class SizingRequest(BaseModel):
    capital: float = 100_000
    risk_pct: float = 1.0
    entry: float
    stop_loss: float
    lot_size: int = 1
    risk_reward: float | None = None


@router.post("/risk/calculate")
async def calculate(request: Request, body: SizingRequest) -> dict[str, Any]:
    """Powers the dashboard's interactive position-sizing panel."""
    return _engine(request).risk.size_calculator(
        capital=body.capital, risk_pct=body.risk_pct, entry=body.entry,
        stop_loss=body.stop_loss, lot_size=body.lot_size, rr=body.risk_reward)


@router.get("/risk/state")
async def risk_state(request: Request) -> dict[str, Any]:
    return _engine(request).risk.snapshot()


@router.post("/risk/resume")
async def resume(request: Request) -> dict[str, Any]:
    """Manually clear a halt. Deliberately explicit — the desk does not
    un-halt itself after breaching the daily loss limit."""
    engine = _engine(request)
    engine.risk.state.halted = False
    engine.risk.state.halt_reason = ""
    return engine.risk.snapshot()


# --------------------------------------------------------------------------- #
# Learning
# --------------------------------------------------------------------------- #
@router.get("/learning/scorecard")
async def scorecard(request: Request) -> dict[str, Any]:
    return {"agents": _engine(request).feedback.scorecard(),
            "weights": get_config().get("weights", {})}


@router.get("/learning/recall/{symbol}")
async def recall(request: Request, symbol: str) -> dict[str, Any]:
    return {"recall": _engine(request).feedback.recall_for(symbol, limit=20)}


@router.get("/agents")
async def agents(request: Request) -> dict[str, Any]:
    return _engine(request).desk.describe()
