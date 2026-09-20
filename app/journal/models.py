"""Trade journal contracts.

The journal exists to separate PROCESS from OUTCOME. A winning trade that broke
your rules is a bad trade; a losing trade that honoured its stop is an
acceptable one. Every field here serves that distinction.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.core.models import utcnow


class SetupType(str, Enum):
    """Mandatory on every trade. Untagged trades cannot be learned from."""
    MEAN_REVERSION = "Mean Reversion"
    BREAKOUT = "Breakout"
    SECTOR_LAGGARD = "Sector Laggard"
    NEWS_MOMENTUM = "News Momentum"
    OTHER = "Other"


class MistakeTag(str, Enum):
    """The recurring, expensive errors. Naming them is how they stop."""
    CHASED_PRICE = "Chased Price (FOMO)"
    MOVED_STOP = "Moved Stop Loss Further"
    EXITED_EARLY = "Exited Before Target"
    SIZED_TOO_LARGE = "Sized Too Large"
    REVENGE_TRADED = "Revenge Traded"
    IGNORED_MARKET = "Ignored Broader Market Trend"
    NO_VOLUME_CONFIRM = "No Volume / Absorption Confirmation"
    HELD_PAST_TIME_STOP = "Held Past Time Stop"


class TradeVerdict(str, Enum):
    """The only four outcomes that matter for learning."""
    GOOD_WIN = "GOOD_WIN"        # followed the rules, made money
    GOOD_LOSS = "GOOD_LOSS"      # followed the rules, lost money — acceptable
    BAD_WIN = "BAD_WIN"          # broke the rules, got paid anyway — dangerous
    BAD_LOSS = "BAD_LOSS"        # broke the rules and paid for it


class JournalEntry(BaseModel):
    """One completed trade, with everything needed for a post-mortem."""
    id: str
    ts: datetime = Field(default_factory=utcnow)
    market: str = "IN"
    symbol: str
    instrument: str = ""
    setup: SetupType = SetupType.OTHER
    side: str = "BUY"

    # --- the plan, recorded BEFORE the outcome is known ---
    planned_entry: float
    planned_stop: float
    planned_target: float
    planned_quantity: int = 0

    # --- what actually happened ---
    actual_entry: float | None = None
    actual_exit: float | None = None
    actual_quantity: int = 0
    entry_ts: datetime | None = None
    exit_ts: datetime | None = None

    # --- derived ---
    slippage: float = 0.0            # actual entry vs planned, in price terms
    hold_minutes: float = 0.0
    pnl: float = 0.0
    initial_risk: float = 0.0        # planned stop distance x planned quantity
    r_multiple: float = 0.0          # realised PnL / initial risk

    # --- process, not outcome ---
    stop_honoured: bool = True
    mistakes: list[MistakeTag] = Field(default_factory=list)
    verdict: TradeVerdict | None = None
    execution_score: int | None = None   # 1-10, discipline only
    notes: str = ""

    # --- context captured at entry, so the post-mortem is not guesswork ---
    context: dict[str, Any] = Field(default_factory=dict)
    signal_id: str | None = None

    def compute(self) -> JournalEntry:
        """Fill in every derived field from what was recorded."""
        entry = self.actual_entry if self.actual_entry is not None else self.planned_entry
        qty = self.actual_quantity or self.planned_quantity

        self.slippage = round(entry - self.planned_entry, 4)

        stop_points = abs(self.planned_entry - self.planned_stop)
        self.initial_risk = round(stop_points * (self.planned_quantity or qty), 2)

        if self.actual_exit is not None and qty:
            direction = 1 if self.side.upper() == "BUY" else -1
            self.pnl = round((self.actual_exit - entry) * qty * direction, 2)
            # R-multiple realisation: what the trade actually returned, measured
            # in units of the risk it was sized against.
            self.r_multiple = (round(self.pnl / self.initial_risk, 3)
                               if self.initial_risk else 0.0)

        if self.entry_ts and self.exit_ts:
            self.hold_minutes = round(
                (self.exit_ts - self.entry_ts).total_seconds() / 60.0, 1)

        self.verdict = self._verdict()
        return self

    def _verdict(self) -> TradeVerdict:
        """Process first, outcome second. A rule-breaking winner is still bad."""
        clean = not self.mistakes and self.stop_honoured
        won = self.pnl > 0
        if clean:
            return TradeVerdict.GOOD_WIN if won else TradeVerdict.GOOD_LOSS
        return TradeVerdict.BAD_WIN if won else TradeVerdict.BAD_LOSS

    @property
    def rule_violation(self) -> bool:
        return bool(self.mistakes) or not self.stop_honoured


class MistakeCard(BaseModel):
    """The rendered post-mortem for one trade."""
    trade_id: str
    ts: datetime = Field(default_factory=utcnow)
    ticker_setup: str
    execution_score: int
    core_violation: str
    entry_analysis: str
    exit_discipline: str
    root_cause: str
    corrective_protocol: str
    verdict: TradeVerdict
    r_multiple: float
    generated_by: str = "rules"      # "claude" | "rules"

    def to_markdown(self) -> str:
        badge = {
            TradeVerdict.GOOD_WIN: "✅ GOOD WIN — rules followed, paid",
            TradeVerdict.GOOD_LOSS: "🟦 GOOD LOSS — rules followed, edge failed",
            TradeVerdict.BAD_WIN: "⚠️ BAD WIN — rules broken, got paid anyway",
            TradeVerdict.BAD_LOSS: "❌ BAD LOSS — rules broken, paid for it",
        }[self.verdict]

        return f"""### 🃏 Trade Post-Mortem Card

- **Ticker & Setup:** {self.ticker_setup}
- **Execution Quality Score:** {self.execution_score}/10
- **The Core Violation (if any):** {self.core_violation}
- **Verdict:** {badge}
- **R-multiple realised:** {self.r_multiple:+.2f}R

#### 🔍 Technical Breakdown
- **Entry Analysis:** {self.entry_analysis}
- **Exit & Stop Discipline:** {self.exit_discipline}

#### 🧠 Root Cause & Bias
{self.root_cause}

#### 🛠️ Corrective Protocol
{self.corrective_protocol}

---
*Generated {self.ts:%Y-%m-%d %H:%M} by {self.generated_by} · trade `{self.trade_id}`*
"""
