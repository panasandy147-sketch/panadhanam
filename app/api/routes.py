"""REST API. Everything the dashboard needs, plus manual controls."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core import clock
from app.core.bus import Topic, bus
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
# Markets
# --------------------------------------------------------------------------- #
@router.get("/markets")
async def markets(request: Request) -> dict[str, Any]:
    cfg = get_config()
    engine = getattr(request.app.state, "engine", None)
    return {
        "active": cfg.active_market,
        "available": cfg.available_markets(),
        "profile": cfg.market.describe(),
        "clock": clock.describe(str(cfg.get("system.timezone", "Asia/Kolkata"))),
        "phase": engine.session_phase() if engine else None,
    }


@router.post("/markets/{code}")
async def switch_market(request: Request, code: str) -> dict[str, Any]:
    """Switch the whole desk between markets (IN / US)."""
    result = await _engine(request).switch_market(code)
    if not result["switched"] and "reason" in result:
        reason = result["reason"]
        if "open" in reason or "unknown" in reason:
            raise HTTPException(409, reason)
    return result


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
    return {"signals": [_explained(s) for s in
                        db.recent_signals(limit=limit, symbol=symbol, status=status)]}


def _explained(row: dict[str, Any]) -> dict[str, Any]:
    """A signal row plus, in words, why it was bought and how it sold."""
    from app.core.explain import why_bought, why_sold

    if row.get("status") == "REJECTED":
        return row
    return {**row, "why": why_bought(row), "exit_reason": why_sold(row)}


@router.get("/signals/{signal_id}")
async def signal_detail(signal_id: str) -> dict[str, Any]:
    signal = db.get_signal(signal_id)
    if not signal:
        raise HTTPException(404, "signal not found")
    return {"signal": _explained(signal),
            "reports": db.reports_for_signal(signal_id)}


@router.get("/focus")
async def focus(request: Request) -> dict[str, Any]:
    """The names the desk is watching now: the top of each band."""
    return _engine(request).focus.snapshot()


@router.get("/rules")
async def rules(request: Request) -> dict[str, Any]:
    """The rules and strategies in words, with the live config's numbers."""
    from app.core.rules import build

    engine = _engine(request)
    return build(get_config(), capital=engine.risk.state.capital)


@router.get("/positions")
async def positions(request: Request) -> dict[str, Any]:
    engine = _engine(request)
    return {
        "open_signals": [_explained(s) for s in db.open_signals()],
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
                  count: int = 200, session: bool = True,
                  warmup: int = 60) -> dict[str, Any]:
    """Bars for the chart: today's session, plus the bars the lines need.

    Several days of 5m bars on one axis squeeze the current session into the
    right-hand edge — the part that shows the trend the desk is acting on. So
    intraday timeframes return the LATEST session in `candles`, and the bars
    immediately before it in `warmup`. The chart draws only `candles` but
    computes its EMAs over both: an EMA 50 started from nine bars of today is
    not an EMA 50, and drawing one would put a wrong line on a right chart.

    "Latest session" is the last bar's date in the MARKET's own timezone, not
    the wall clock and not the viewer's: before the open, after the close and
    at a weekend the chart still shows a whole session, and watching New York
    from India does not split one session across two dates.

    Daily bars are returned as they are — across days is the point of them.
    """
    from zoneinfo import ZoneInfo

    engine = _engine(request)
    cfg = get_config()
    tz_name = cfg.market.timezone
    data = await engine.broker.get_candles(symbol, timeframe, max(count, 250))

    today: list = list(data)
    before: list = []
    trimmed = session and timeframe != "1d" and bool(data)
    if trimmed:
        zone = ZoneInfo(tz_name)
        day = data[-1].ts.astimezone(zone).date()
        today = [c for c in data if c.ts.astimezone(zone).date() == day]
        before = [c for c in data if c.ts.astimezone(zone).date() < day][-warmup:]
    else:
        today = today[-count:]

    def bar(c) -> dict[str, Any]:
        return {"time": int(c.ts.timestamp()), "open": c.open, "high": c.high,
                "low": c.low, "close": c.close, "volume": c.volume}

    return {"symbol": symbol, "timeframe": timeframe,
            # The chart library renders epochs in UTC; it has to be told which
            # clock the session runs on, or 09:15 IST is labelled 03:45.
            "timezone": tz_name,
            "session_only": trimmed,
            "candles": [bar(c) for c in today],
            "warmup": [bar(c) for c in before]}


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


class CapitalRequest(BaseModel):
    capital: float


@router.post("/risk/capital")
async def set_capital(request: Request, body: CapitalRequest) -> dict[str, Any]:
    """Change the account size the desk sizes positions against."""
    engine = _engine(request)
    result = engine.risk.set_capital(body.capital)
    if not result["ok"]:
        raise HTTPException(409, result["reason"])
    await bus.publish(Topic.RISK_STATE, engine.risk.snapshot())
    return result


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


# --------------------------------------------------------------------------- #
# Opportunity board & historical replay
# --------------------------------------------------------------------------- #
@router.get("/opportunities")
async def opportunities(request: Request, per_tier: int | None = None,
                        refresh: bool = False) -> dict[str, Any]:
    """Top N setups per risk tier (low / medium / high).

    Cached between calls so opening the dashboard doesn't re-scan the whole
    watchlist; pass refresh=true to force a rescan.
    """
    engine = _engine(request)
    if refresh or not engine.scanner.last_scan:
        return await engine.scanner.scan(per_tier=per_tier)
    return engine.scanner.last_scan


@router.post("/opportunities/scan")
async def scan_opportunities(request: Request, per_tier: int | None = None) -> dict[str, Any]:
    return await _engine(request).scanner.scan(per_tier=per_tier)


@router.get("/replay")
async def replay(request: Request, days: int = 5, timeframe: str = "5m",
                 refresh: bool = False) -> dict[str, Any]:
    """What the rules would have caught over the last N sessions."""
    engine = _engine(request)
    if refresh or not engine.replay.last_result:
        return await engine.replay.run(days=days, timeframe=timeframe)
    return engine.replay.last_result


@router.post("/replay/run")
async def run_replay(request: Request, days: int = 5,
                     timeframe: str = "5m") -> dict[str, Any]:
    return await _engine(request).replay.run(days=days, timeframe=timeframe)


@router.get("/agents")
async def agents(request: Request) -> dict[str, Any]:
    return _engine(request).desk.describe()
