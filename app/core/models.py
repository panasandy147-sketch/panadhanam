"""Shared data contracts.

Every agent consumes `MarketContext` and returns an `AgentReport`. Keeping that
contract narrow is what makes agents hot-swappable: a new analyst only has to
produce a score, a rationale and some evidence to join the desk.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Bias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class InstrumentType(str, Enum):
    EQUITY = "EQUITY"
    FUTURE = "FUTURE"
    CALL = "CE"
    PUT = "PE"


class SignalStatus(str, Enum):
    PROPOSED = "PROPOSED"      # CMIO put it forward
    REJECTED = "REJECTED"      # Risk Manager vetoed it
    APPROVED = "APPROVED"      # cleared risk, alert dispatched
    OPEN = "OPEN"              # position live
    CLOSED_TARGET = "CLOSED_TARGET"
    CLOSED_STOP = "CLOSED_STOP"
    CLOSED_TIME = "CLOSED_TIME"
    CANCELLED = "CANCELLED"


class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGEBOUND = "rangebound"
    VOLATILE = "volatile"


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
class Candle(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Quote(BaseModel):
    symbol: str
    last_price: float
    change_pct: float = 0.0
    volume: float = 0.0
    oi: float | None = None
    bid: float | None = None
    ask: float | None = None
    ts: datetime = Field(default_factory=utcnow)


class OptionLeg(BaseModel):
    strike: float
    option_type: Literal["CE", "PE"]
    ltp: float = 0.0
    oi: float = 0.0
    oi_change: float = 0.0
    volume: float = 0.0
    iv: float = 0.0
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


class OptionChain(BaseModel):
    underlying: str
    spot: float
    expiry: str
    fetched_at: datetime = Field(default_factory=utcnow)
    legs: list[OptionLeg] = Field(default_factory=list)
    # True when no feed served a chain and the simulator generated one. Its
    # OI, PCR and Max Pain are invented, so nothing may vote on them.
    synthetic: bool = False

    def by_type(self, opt: str) -> list[OptionLeg]:
        return sorted([leg for leg in self.legs if leg.option_type == opt],
                      key=lambda x: x.strike)

    def atm_strike(self) -> float:
        if not self.legs:
            return self.spot
        return min({leg.strike for leg in self.legs}, key=lambda k: abs(k - self.spot))


class NewsItem(BaseModel):
    title: str
    source: str
    url: str = ""
    published: datetime = Field(default_factory=utcnow)
    summary: str = ""
    symbols: list[str] = Field(default_factory=list)
    # Filled by the sentiment agent
    impact: float = 0.0          # -1.0 .. +1.0
    confidence: float = 0.0
    decay_minutes: int = 60


class MacroSnapshot(BaseModel):
    fetched_at: datetime = Field(default_factory=utcnow)
    values: dict[str, float] = Field(default_factory=dict)      # key -> level
    changes_pct: dict[str, float] = Field(default_factory=dict)  # key -> % change
    labels: dict[str, str] = Field(default_factory=dict)         # key -> display name
    fii_cash: float | None = None
    dii_cash: float | None = None
    india_vix: float | None = None
    notes: list[str] = Field(default_factory=list)


class Fundamentals(BaseModel):
    symbol: str
    pe: float | None = None
    industry_pe: float | None = None
    roe: float | None = None
    eps: float | None = None
    profit_growth_pct: float | None = None
    debt_to_equity: float | None = None
    avg_volume: float | None = None


# --------------------------------------------------------------------------- #
# Agent I/O
# --------------------------------------------------------------------------- #
class Evidence(BaseModel):
    """One concrete, checkable fact behind a score. Keeps agents honest."""
    label: str
    value: str
    weight: float = 1.0


class AgentReport(BaseModel):
    """The single contract every analyst returns."""
    agent_id: str
    symbol: str
    bias: Bias = Bias.NEUTRAL
    score: float = 0.0            # -1.0 (max bearish) .. +1.0 (max bullish)
    confidence: float = 0.0       # 0..1 — how sure, independent of direction
    rationale: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    # Where this analyst thinks the idea is proven wrong. The Risk Manager
    # prefers a structural level like this over a generic ATR stop.
    invalidation_level: float | None = None
    suggested_entry: float | None = None
    suggested_target: float | None = None
    data_available: bool = True   # False => treated as abstain, not agreement
    latency_ms: int = 0
    used_llm: bool = False
    ts: datetime = Field(default_factory=utcnow)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("score")
    @classmethod
    def _clamp_score(cls, v: float) -> float:
        return max(-1.0, min(1.0, float(v)))

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))


class Instrument(BaseModel):
    symbol: str                   # underlying, e.g. NIFTY / RELIANCE
    tradingsymbol: str            # what the broker actually takes
    instrument_type: InstrumentType = InstrumentType.EQUITY
    exchange: str = "NSE"
    lot_size: int = 1
    strike: float | None = None
    expiry: str | None = None
    tick_size: float = 0.05


class TradeSignal(BaseModel):
    """A fully-specified, risk-approved (or rejected) trade candidate."""
    id: str
    ts: datetime = Field(default_factory=utcnow)
    instrument: Instrument
    side: Side
    entry: float
    stop_loss: float
    target: float
    quantity: int = 0
    lots: int = 0
    # How many underlying units one lot/contract represents. 1 means the
    # instrument trades in single shares and has no lot concept at all, so the
    # UI must not print "99 lots" for 99 shares of QQQ.
    unit_size: int = 1
    unit_label: str = "unit"
    # Spot of the UNDERLYING at entry. For an option this is what lets the
    # outcome tracker mark the premium to market via delta.
    entry_spot: float | None = None
    entry_delta: float | None = None

    risk_per_unit: float = 0.0
    total_risk: float = 0.0
    reward_per_unit: float = 0.0
    risk_reward: float = 0.0
    capital_at_risk_pct: float = 0.0
    notional: float = 0.0

    status: SignalStatus = SignalStatus.PROPOSED
    confirmations: list[str] = Field(default_factory=list)
    composite_score: float = 0.0
    bias: Bias = Bias.NEUTRAL
    rationale: str = ""
    counter_argument: str = ""
    rejection_reasons: list[str] = Field(default_factory=list)
    reports: list[AgentReport] = Field(default_factory=list)
    regime: Regime | None = None

    # Outcome tracking (filled by the learning loop)
    exit_price: float | None = None
    exit_ts: datetime | None = None
    pnl: float | None = None
    r_multiple: float | None = None

    def alert_line(self) -> str:
        """The exact alert format the desk spec asks for."""
        i = self.instrument
        leg = f"{i.tradingsymbol}"
        if i.strike:
            leg = f"{i.symbol} {int(i.strike)} {i.instrument_type.value}"
        return (
            f"[{i.symbol} | {leg}] [{self.side.value}] "
            f"[ENTRY {self.entry:.2f}] [SL {self.stop_loss:.2f}] "
            f"[TGT {self.target:.2f} ({self.risk_reward:.1f}R)] "
            f"[{', '.join(self.confirmations) or 'no confirmations'}]"
        )


class RiskState(BaseModel):
    """Live desk state the Risk Manager mutates and everyone else reads."""
    capital: float
    realised_pnl: float = 0.0
    unrealised_pnl: float = 0.0
    open_positions: int = 0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    daily_loss_limit: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    exposure: float = 0.0

    @property
    def daily_pnl(self) -> float:
        return self.realised_pnl + self.unrealised_pnl


class MarketContext(BaseModel):
    """Everything the analysts get to see for one symbol, one cycle."""
    symbol: str
    cycle_id: str
    ts: datetime = Field(default_factory=utcnow)
    quote: Quote | None = None
    candles: dict[str, list[Candle]] = Field(default_factory=dict)  # tf -> candles
    option_chain: OptionChain | None = None
    news: list[NewsItem] = Field(default_factory=list)
    macro: MacroSnapshot | None = None
    fundamentals: Fundamentals | None = None
    indicators: dict[str, Any] = Field(default_factory=dict)
    regime: Regime | None = None
    # Graded past signals for this symbol — the "learn from what happened" hook.
    recall: list[dict[str, Any]] = Field(default_factory=list)


class CycleResult(BaseModel):
    cycle_id: str
    ts: datetime = Field(default_factory=utcnow)
    symbol: str
    bias: Bias
    composite_score: float
    reports: list[AgentReport] = Field(default_factory=list)
    signal: TradeSignal | None = None
    rejected: list[str] = Field(default_factory=list)
    duration_ms: int = 0
