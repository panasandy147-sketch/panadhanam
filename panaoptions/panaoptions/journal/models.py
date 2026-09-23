"""What a graded trade looks like."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from panaoptions.models import SetupType


class Mistake(str, Enum):
    """The taxonomy. Every tag is detectable from the ledger alone — nothing
    here depends on the trader admitting to anything."""
    NO_STRATEGY_TAG = "Entered without a named strategy"
    IGNORED_INVALIDATION = "Held past the invalidation level"
    STOP_NOT_HONOURED = "Stop was not honoured"
    EXITED_EARLY = "Exited before the target or the trail"
    OVERSIZED = "Position exceeded the deployment limit"
    OUTSIDE_WINDOW = "Entered outside the strategy's window"
    CHASED = "Chased the entry away from the signal bar"
    TRADED_WHILE_HALTED = "Traded after the circuit breaker"
    HELD_TO_THE_BELL = "Held to the forced close rather than exiting on a rule"


class Verdict(str, Enum):
    """Four outcomes, because process and result are independent.

    BAD_WIN is the dangerous one: the money came in, so the habit gets
    reinforced, and it is the habit that eventually costs you.
    """
    GOOD_WIN = "GOOD_WIN"
    GOOD_LOSS = "GOOD_LOSS"
    BAD_WIN = "BAD_WIN"
    BAD_LOSS = "BAD_LOSS"


class JournalEntry(BaseModel):
    id: str
    trade_id: str
    ts: datetime
    symbol: str
    contract: str
    strategy: SetupType = SetupType.OTHER
    # Which pattern produced it. Seventeen patterns roll up under one strategy
    # name, and "Candlestick at a Key Level lost money" is not a finding you
    # can act on — the question is always WHICH of them.
    pattern: str = ""
    # What that pattern is published as scoring, for the review to hold the
    # claim against the measurement. Never read by the risk path.
    claimed_accuracy: float = 0.0
    direction: str = "LONG"

    entry_price: float = 0.0
    exit_price: float = 0.0
    stop_price: float = 0.0
    quantity: int = 0
    pnl: float = 0.0
    return_pct: float = 0.0
    hold_minutes: float = 0.0
    exit_reason: str = ""

    underlying_support: float = 0.0
    invalidation_note: str = ""

    mistakes: list[Mistake] = Field(default_factory=list)
    verdict: Verdict | None = None
    execution_score: int = 0        # 1-10, discipline only
    notes: str = ""

    @property
    def clean(self) -> bool:
        return not self.mistakes


class Card(BaseModel):
    """The written post-mortem for one trade."""
    trade_id: str
    ts: datetime
    headline: str
    strategy: SetupType = SetupType.OTHER
    verdict: Verdict
    execution_score: int
    pnl: float
    what_happened: str = ""
    core_violation: str = ""
    what_to_repeat: str = ""
    generated_by: str = "rules"

    def to_markdown(self) -> str:
        lines = [
            f"# {self.headline}",
            "",
            f"*{self.ts:%Y-%m-%d %H:%M} · {self.strategy.value} · "
            f"**{self.verdict.value}** · discipline {self.execution_score}/10 · "
            f"P&L {self.pnl:+,.2f}*",
            "",
        ]
        if self.verdict is Verdict.BAD_WIN:
            lines += ["> This made money **while breaking a rule**. The P&L is "
                      "rewarding a habit that will eventually cost you — which "
                      "is why it is graded as a failure.", ""]
        if self.what_happened:
            lines += ["## What happened", "", self.what_happened, ""]
        if self.core_violation and self.core_violation.lower() != "none":
            lines += ["## The error", "", self.core_violation, ""]
        if self.what_to_repeat:
            lines += ["## Do this again", "", self.what_to_repeat, ""]
        lines += [f"*Graded by {self.generated_by}.*", ""]
        return "\n".join(lines)
