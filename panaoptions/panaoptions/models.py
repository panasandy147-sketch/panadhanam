"""The vocabulary. Everything crossing a module boundary is one of these."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Direction(str, Enum):
    LONG = "LONG"       # buy calls
    SHORT = "SHORT"     # buy puts
    NONE = "NONE"


class OptionRight(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class Candle(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class PreMarketRead(BaseModel):
    """What the pre-market screener found for one symbol."""
    symbol: str
    previous_close: float = 0.0
    last_price: float = 0.0
    gap_pct: float = 0.0
    rvol: float = 0.0
    rvol_unmeasured: bool = False      # an index with no volume in the feed
    passed: bool = False
    reasons: list[str] = Field(default_factory=list)


class Indicators(BaseModel):
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    vwap: float = 0.0
    atr: float = 0.0
    volume: float = 0.0
    avg_volume: float = 0.0
    rvol: float = 0.0
    close: float = 0.0


class SetupType(str, Enum):
    """The named strategies. Every trade is tagged with the one that fired it,
    so the journal can answer "which of these actually works" rather than
    lumping every entry together."""
    ORB_VWAP = "ORB + VWAP"
    VWAP_EMA_PULLBACK = "VWAP / 9-EMA Pullback"
    LIQUIDITY_SWEEP = "Liquidity Sweep Reversal"
    CANDLESTICK_AT_LEVEL = "Candlestick at a Key Level"
    # The volume-profile family (strategies/volume_profile_strategies.py).
    VA_REJECTION = "Value Area Rejection"
    LVN_ACCELERATION = "LVN Pocket Acceleration"
    POC_BOUNCE = "POC Magnet / Bounce"
    # The previous session's high / low swept and reclaimed (a failed breakout).
    PD_LIQUIDITY_SWEEP = "PD Liquidity Sweep"
    # Larry Williams' volatility breakout: today's open +/- k x yesterday's range.
    VOLATILITY_BREAKOUT = "Volatility Breakout"
    OTHER = "Other"


class SessionLevels(BaseModel):
    """The reference prices a strategy measures against, computed once a day."""
    opening_range_high: float = 0.0
    opening_range_low: float = 0.0
    premarket_high: float = 0.0
    premarket_low: float = 0.0
    previous_close: float = 0.0
    # The previous session's high and low (PDH / PDL), and the close before
    # it — for the day's price change in the F&O build-up read.
    previous_high: float = 0.0
    previous_low: float = 0.0
    close_before: float = 0.0

    @property
    def range_height(self) -> float:
        return max(self.opening_range_high - self.opening_range_low, 0.0)

    @property
    def has_opening_range(self) -> bool:
        return self.opening_range_high > 0 and self.opening_range_low > 0


class Setup(BaseModel):
    """A technical trigger on the underlying, before any option is chosen."""
    symbol: str
    ts: datetime
    direction: Direction = Direction.NONE
    strategy: SetupType = SetupType.OTHER
    pattern: str = ""
    confirmations: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    indicators: Indicators = Field(default_factory=Indicators)
    trend_aligned: bool = False

    # The level on the UNDERLYING that invalidates the thesis. This is the
    # real stop: a fixed percentage on the premium is at the mercy of an IV
    # shift or a wide spread, and says nothing about whether the trade was
    # wrong. See risk.stop_mode.
    underlying_support: float = 0.0
    invalidation_note: str = ""
    # Where the move is expected to reach, when the strategy has a view.
    underlying_target: float = 0.0

    # The price that confirms the pattern. A pattern is not an entry: price
    # has to take out the trigger before the option is bought.
    entry_trigger: float = 0.0
    # The level the pattern formed at, and what makes it a level. A pattern in
    # the middle of a range is noise, so this is never empty on a triggered
    # candlestick setup.
    key_level: float = 0.0
    key_level_source: str = ""
    # Delta band this pattern wants, overriding the global one. A hammer and a
    # morning star are not the same bet and do not want the same contract.
    delta_band: tuple[float, float] | None = None
    min_dte_override: int = 0
    max_dte_override: int = 0
    # The win rate this pattern is published as having, if any. Carried so the
    # journal can hold the claim against what it actually did here; it never
    # influences sizing, and nothing in the risk path reads it.
    claimed_accuracy: float = 0.0
    # Plain-language case for the trade, for the dashboard to show.
    reasoning: list[str] = Field(default_factory=list)

    @property
    def triggered(self) -> bool:
        return self.direction is not Direction.NONE and not self.blockers


class OptionContract(BaseModel):
    symbol: str
    right: OptionRight
    strike: float
    expiry: str
    dte: int = 0
    bid: float = 0.0
    ask: float = 0.0
    delta: float = 0.0
    implied_volatility: float = 0.0
    open_interest: int = 0
    # Change in open interest since the previous close, when the exchange
    # publishes it (NSE's changeinOpenInterest); 0 when unknown.
    oi_change: int = 0
    volume: int = 0
    # Units one contract controls: 100 in the US, the NSE lot in India
    # (NIFTY 75, BANKNIFTY 30 ...). 0 means "the market default".
    multiplier: int = 0
    # True when the price is a model estimate (India, NSE refusing), not a quote.
    estimated: bool = False
    # The 1-minute volume-weighted bid-ask spread (% of mid) when the rolling
    # check has samples for this contract; None = only the snapshot is known.
    rolling_spread_pct: float | None = None
    # A debit spread is held as ONE position priced at its net debit: buy
    # `long_leg`, sell `short_leg` further out of the money, same expiry.
    # strike/delta/right above are the long leg's; bid/ask are the net.
    long_leg: OptionContract | None = None
    short_leg: OptionContract | None = None

    @property
    def is_spread(self) -> bool:
        return self.long_leg is not None and self.short_leg is not None

    @property
    def mid(self) -> float:
        if self.is_spread:
            return round(max(self.long_leg.mid - self.short_leg.mid, 0.0), 4)
        if self.bid and self.ask:
            return round((self.bid + self.ask) / 2, 4)
        return self.ask or self.bid

    @property
    def width(self) -> float:
        """A spread's most it can be worth, per share: the strike distance."""
        if not self.is_spread:
            return 0.0
        return round(abs(self.short_leg.strike - self.long_leg.strike), 4)

    @property
    def effective_spread_pct(self) -> float:
        """The bid-ask spread the risk checks use: the rolling 1-minute
        volume-weighted figure when there is one, else the snapshot. For a
        debit spread, the wider of its two legs'."""
        if self.is_spread:
            return max(self.long_leg.effective_spread_pct,
                       self.short_leg.effective_spread_pct)
        if self.rolling_spread_pct is not None:
            return self.rolling_spread_pct
        return self.spread_pct_of_mid

    @property
    def spread(self) -> float:
        return round(max(self.ask - self.bid, 0.0), 4)

    @property
    def spread_pct_of_mid(self) -> float:
        mid = self.mid
        return round(self.spread / mid * 100, 2) if mid else 999.0

    def cost(self, multiplier: int = 100) -> float:
        """What one contract actually costs: premium x its own lot size."""
        return round(self.mid * (self.multiplier or multiplier), 2)

    @property
    def label(self) -> str:
        if self.is_spread:
            return (f"{self.symbol} {self.expiry} {self.long_leg.strike:g}/"
                    f"{self.short_leg.strike:g}{self.right.value[0]} spread")
        return f"{self.symbol} {self.expiry} {self.strike:g}{self.right.value[0]}"

    @property
    def structure(self) -> str:
        if not self.is_spread:
            return "single"
        return "bull call spread" if self.right is OptionRight.CALL else "bear put spread"


OptionContract.model_rebuild()


def option_side(direction: Direction) -> str:
    """The trade as it is bought: a long setup buys calls, a short one puts.
    Both are LONG option positions — the desk never sells premium naked."""
    if direction is Direction.LONG:
        return "LONG_CALL"
    if direction is Direction.SHORT:
        return "LONG_PUT"
    return ""


def execution_kind(contract: OptionContract, direction: Direction) -> str:
    """How the order is structured: outright, or a converted debit spread."""
    if contract.is_spread:
        return ("BULL_CALL_DEBIT_SPREAD" if contract.right is OptionRight.CALL
                else "BEAR_PUT_DEBIT_SPREAD")
    return f"OUTRIGHT_{option_side(direction)}"


class ContractSearch(BaseModel):
    """The result of filtering a chain — including why nothing qualified.

    A filter that returns nothing is only useful if it says what it rejected
    and by how much. Without the counts, an impossible rule looks exactly like
    a quiet market.
    """
    symbol: str
    chosen: OptionContract | None = None
    examined: int = 0
    rejected: dict[str, int] = Field(default_factory=dict)
    closest_by_price: OptionContract | None = None
    note: str = ""
    # True when the setup's own delta band was over budget and the desk took
    # the highest-delta contract that fits instead of skipping the trade.
    budget_fallback: bool = False
    # Which rung found the contract: primary, shorter_expiry, debit_spread,
    # secondary_delta — or "" when nothing did.
    tier: str = ""
    # Why each fallback rung found nothing, when the setup was skipped.
    skipped_because: list[str] = Field(default_factory=list)


class Signal(BaseModel):
    id: str
    ts: datetime
    symbol: str
    direction: Direction
    contract: OptionContract
    quantity: int = 1
    entry_price: float = 0.0          # per share of premium
    stop_price: float = 0.0
    target_1: float = 0.0
    target_2: float = 0.0
    underlying_at_entry: float = 0.0
    underlying_support: float = 0.0
    underlying_target: float = 0.0
    strategy: SetupType = SetupType.OTHER
    invalidation_note: str = ""
    key_level: float = 0.0
    key_level_source: str = ""
    reasoning: list[str] = Field(default_factory=list)
    pattern: str = ""
    claimed_accuracy: float = 0.0
    confirmations: list[str] = Field(default_factory=list)
    ml_probability: float | None = None

    def cost(self, multiplier: int = 100) -> float:
        m = self.contract.multiplier or multiplier
        return round(self.entry_price * self.quantity * m, 2)

    def risk_at_stop(self, multiplier: int = 100) -> float:
        m = self.contract.multiplier or multiplier
        return round((self.entry_price - self.stop_price) * self.quantity * m, 2)

    @property
    def side_tag(self) -> str:
        return option_side(self.direction)

    @property
    def execution(self) -> str:
        return execution_kind(self.contract, self.direction)

    def alert_line(self) -> str:
        arrow = "CALL" if self.direction is Direction.LONG else "PUT"
        c = self.contract
        if c.is_spread:
            return (f"[{self.symbol} | {c.expiry} {c.long_leg.strike:g}/"
                    f"{c.short_leg.strike:g}{arrow[0]} {c.structure.upper()}] BUY "
                    f"[{self.entry_price:.2f} net] SL [{self.stop_price:.2f}] "
                    f"TP1 [{self.target_1:.2f}] TP2 [{self.target_2:.2f}] "
                    f"max [{c.width:g}]")
        return (f"[{self.symbol} | {self.contract.expiry} "
                f"{self.contract.strike:g}{arrow[0]}] BUY "
                f"[{self.entry_price:.2f}] "
                f"SL [{self.stop_price:.2f}] "
                f"TP1 [{self.target_1:.2f}] TP2 [{self.target_2:.2f}]")


class ExitReason(str, Enum):
    STOP = "STOP"
    EMA_TRAIL = "EMA_TRAIL"
    TARGET_1 = "TARGET_1"
    TARGET_2 = "TARGET_2"
    TRAIL = "TRAIL"
    UNDERLYING_BREAK = "UNDERLYING_BREAK"
    TIME_EXIT = "TIME_EXIT"
    DAY_END = "DAY_END"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"


class Fill(BaseModel):
    ts: datetime
    quantity: int
    price: float
    reason: str = ""


class PaperTrade(BaseModel):
    id: str
    signal_id: str
    symbol: str
    direction: Direction
    contract_label: str
    opened_at: datetime
    quantity: int
    entry_price: float
    stop_price: float
    target_1: float
    target_2: float
    underlying_support: float = 0.0
    strategy: SetupType = SetupType.OTHER
    # Which pattern produced it, carried so the journal can grade patterns
    # rather than only the strategy they all roll up under.
    pattern: str = ""
    claimed_accuracy: float = 0.0
    invalidation_note: str = ""
    # Units per contract (the NSE lot in India); 0 = the market default.
    multiplier: int = 0
    market: str = "US"
    # Bought and marked on model prices, not market quotes.
    estimated: bool = False
    # "single", or "bull call spread" / "bear put spread": then the position
    # is priced at long leg minus short leg, and can be worth at most
    # max_value a share (the strike width).
    structure: str = "single"
    legs: int = 1
    long_label: str = ""
    short_label: str = ""
    max_value: float = 0.0
    tier: str = ""
    # LONG_CALL / LONG_PUT, and OUTRIGHT_LONG_CALL / OUTRIGHT_LONG_PUT /
    # BULL_CALL_DEBIT_SPREAD / BEAR_PUT_DEBIT_SPREAD.
    side_tag: str = ""
    execution: str = ""

    remaining: int = 0
    fills: list[Fill] = Field(default_factory=list)
    realised_pnl: float = 0.0
    closed_at: datetime | None = None
    exit_reason: ExitReason | None = None
    breakeven_armed: bool = False
    max_price_seen: float = 0.0
    last_price: float = 0.0
    # The R-multiple exit plan (risk.exit_style "r_multiple"): the underlying
    # at entry, the original distance to its stop (1R), and the furthest the
    # underlying has run in the trade's favour, in R.
    underlying_entry: float = 0.0
    risk_r: float = 0.0
    best_r: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.remaining > 0
