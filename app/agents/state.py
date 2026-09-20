"""Graph state — the object that flows between nodes."""
from __future__ import annotations

from typing import Any, TypedDict

from app.core.models import AgentReport, MarketContext, TradeSignal


class DeskState(TypedDict, total=False):
    """LangGraph state. Plain dict so it serialises for checkpointing."""
    cycle_id: str
    symbol: str
    context: MarketContext
    reports: list[AgentReport]
    decision: dict[str, Any]
    signal: TradeSignal | None
    dispatch: dict[str, Any]
    errors: list[str]
    started_at: float


def new_state(cycle_id: str, symbol: str, context: MarketContext) -> DeskState:
    import time
    return DeskState(
        cycle_id=cycle_id, symbol=symbol, context=context,
        reports=[], decision={}, signal=None, dispatch={}, errors=[],
        started_at=time.perf_counter(),
    )
