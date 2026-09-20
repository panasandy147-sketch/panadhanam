"""Journal and post-mortem API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.journal import store
from app.journal.analytics import compute_analytics
from app.journal.models import JournalEntry, MistakeTag, SetupType, TradeVerdict
from app.journal.postmortem import PostMortemEngine

log = get_logger("api.journal")

router = APIRouter(prefix="/api/journal", tags=["journal"])


class LogTradeRequest(BaseModel):
    symbol: str
    setup: SetupType = Field(description="Mandatory: untagged trades teach nothing")
    side: str = "BUY"
    planned_entry: float
    planned_stop: float
    planned_target: float
    planned_quantity: int = 0
    actual_entry: float | None = None
    actual_exit: float | None = None
    actual_quantity: int = 0
    entry_ts: str | None = None
    exit_ts: str | None = None
    instrument: str = ""
    mistakes: list[MistakeTag] = Field(default_factory=list)
    notes: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    signal_id: str | None = None


@router.get("/taxonomy")
async def taxonomy() -> dict[str, Any]:
    """The vocabulary the journal uses — drives the UI dropdowns."""
    return {
        "setups": [s.value for s in SetupType],
        "mistakes": [m.value for m in MistakeTag],
        "verdicts": [v.value for v in TradeVerdict],
    }


@router.post("/log")
async def log_trade(request: Request, body: LogTradeRequest) -> dict[str, Any]:
    """Log a completed trade and generate its Mistake Card."""
    import uuid
    from datetime import datetime

    cfg = getattr(request.app.state, "engine", None)
    market = cfg.cfg.active_market if cfg else "IN"
    capital = cfg.risk.state.capital if cfg else 0.0

    def _ts(value: str | None):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    entry = JournalEntry(
        id=f"TRD-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4].upper()}",
        market=market,
        symbol=body.symbol.upper(),
        instrument=body.instrument or body.symbol.upper(),
        setup=body.setup,
        side=body.side.upper(),
        planned_entry=body.planned_entry,
        planned_stop=body.planned_stop,
        planned_target=body.planned_target,
        planned_quantity=body.planned_quantity,
        actual_entry=body.actual_entry,
        actual_exit=body.actual_exit,
        actual_quantity=body.actual_quantity or body.planned_quantity,
        entry_ts=_ts(body.entry_ts),
        exit_ts=_ts(body.exit_ts),
        mistakes=list(body.mistakes),
        notes=body.notes,
        # Capital is recorded so the sizing check has something to measure against.
        context={"capital": capital, **body.context},
        signal_id=body.signal_id,
    )

    engine = PostMortemEngine()
    card = await engine.build(entry)
    entry.execution_score = card.execution_score

    store.init_journal()
    store.save_entry(entry)
    path = store.save_card(card)
    store.export_summary()

    return {
        "trade": entry.model_dump(mode="json"),
        "card": card.model_dump(mode="json"),
        "markdown": card.to_markdown(),
        "saved_to": str(path.name),
    }


@router.get("/entries")
async def list_entries(limit: int = 100, setup: str | None = None,
                       verdict: str | None = None) -> dict[str, Any]:
    return {"entries": store.entries(limit=limit, setup=setup, verdict=verdict)}


@router.get("/cards")
async def list_cards(limit: int = 50) -> dict[str, Any]:
    return {"cards": store.cards(limit=limit)}


@router.get("/cards/{trade_id}")
async def card_detail(trade_id: str) -> dict[str, Any]:
    for row in store.cards(limit=500):
        if row["trade_id"] == trade_id:
            return row
    raise HTTPException(404, "card not found")


@router.get("/analytics")
async def analytics() -> dict[str, Any]:
    return compute_analytics()


@router.post("/export")
async def export() -> dict[str, Any]:
    """Regenerate journal/README.md so the git-tracked summary is current."""
    store.init_journal()
    path = store.export_summary()
    return {"exported": str(path)}
