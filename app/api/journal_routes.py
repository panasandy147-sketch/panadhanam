"""Journal and post-mortem API."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.journal import store, weekly
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


# --------------------------------------------------------------------------- #
# The weekend review
# --------------------------------------------------------------------------- #
def _week(week: str | None) -> tuple[date, date]:
    """Resolve ?week=YYYY-MM-DD (any day in it) to that Monday..Friday."""
    if not week:
        return weekly.current_week()
    try:
        return weekly.week_bounds(date.fromisoformat(week))
    except ValueError as exc:
        raise HTTPException(400, f"'{week}' is not a YYYY-MM-DD date") from exc


@router.get("/weekly")
async def weekly_review(week: str | None = None,
                        coach: bool = True) -> dict[str, Any]:
    """The week's trades, the reasoning behind each, and the coach's read.

    Readable mid-week: `complete` says whether the week has actually finished,
    so a Wednesday snapshot is never mistaken for the week's verdict.

    `coach=false` skips the LLM pass, which on a local model is the slow part.
    """
    start, end = _week(week)
    review = await weekly.build(start, end, with_coach=coach)
    return review.model_dump(mode="json")


@router.post("/weekly/save")
async def save_weekly(week: str | None = None) -> dict[str, Any]:
    """Write the review to journal/weekly/ so it can be committed to git."""
    start, end = _week(week)
    review = await weekly.build(start, end)
    return weekly.save(review)


@router.get("/weekly/download")
async def download_weekly(week: str | None = None,
                          format: str = "md") -> Response:
    """The review as a file. `format=md` to read, `format=json` for the data."""
    if format not in {"md", "json"}:
        raise HTTPException(400, "format must be 'md' or 'json'")

    start, end = _week(week)
    review = await weekly.build(start, end)

    if format == "json":
        body, media = review.model_dump_json(indent=2), "application/json"
    else:
        body, media = weekly.to_markdown(review), "text/markdown; charset=utf-8"

    name = f"weekly-review-{review.label}.{format}"
    return Response(
        content=body, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'})
