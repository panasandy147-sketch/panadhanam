"""The Risk Gatekeeper: every signal passes here before an order exists.

The alpha engine says what the tape is doing and where the idea is wrong. It
knows nothing about money. This module is the only place that decides whether
a particular contract may be bought for that signal, and it checks, in order:

    1. the daily circuit breaker   realised + open loss at 10% of capital
                                   ($400 on $4,000) stops all trading today
    2. the signal itself           five keys, stop on the right side
    3. the contract's side         a call for a long, a put for a short
    4. the bid-ask spread          no more than 7% of the mid, on the rolling
                                   1-minute volume-weighted figure; for a
                                   debit spread, on each leg
    5. the delta band              the setup's band, or down to the 0.30
                                   fallback floor when the band was over budget
    6. the per-trade capital cap   20% of capital ($800), or 25% ($1,000) on the
                                   high-notional index ETFs (SPY, QQQ, DIA)
    7. the total ceiling           everything open together

The index exception exists because a near-the-money SPY or QQQ contract costs
$900-$1,000 on its own: at a flat 20% every valid index signal was refused on
price alone, while the same idea on a $150 stock went through.

Deterministic by design. No model and no prompt reaches this code.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from panaoptions.alpha import validate as validate_signal
from panaoptions.logging import get_logger
from panaoptions.models import Direction, OptionContract, OptionRight

log = get_logger("gatekeeper")

DEFAULT_INDEX_SYMBOLS = ("SPY", "QQQ", "DIA")


@dataclass
class GateDecision:
    """The verdict on one signal-and-contract pair, with the reasons."""
    approved: bool
    cap: float = 0.0
    cap_pct: float = 0.0
    passed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.rejected)


def index_symbols(cfg: Any) -> set[str]:
    """The tickers that get the higher capital cap. "DJI" means DIA."""
    listed = cfg.get("risk.index_symbols") or DEFAULT_INDEX_SYMBOLS
    names = {str(s).upper() for s in listed}
    if "DJI" in names or "^DJI" in names:
        names.add("DIA")
    return names


def cap_pct(cfg: Any, symbol: str) -> float:
    """The per-trade deployment cap for this symbol, in percent of capital."""
    standard = float(cfg.get("risk.max_capital_deployed_pct", 20.0))
    if symbol.upper() in index_symbols(cfg):
        return max(standard, float(cfg.get("risk.index_max_capital_deployed_pct",
                                           25.0)))
    return standard


class RiskGatekeeper:
    """Intercepts signals after generation and approves or refuses a contract.

    Holds no state of its own: the day's P&L, the breaker and the capital at
    work live on the RiskManager, so the gate and the sizing rule can never
    disagree about how much room is left.
    """

    def __init__(self, cfg: Any, risk: Any | None = None) -> None:
        self.cfg = cfg
        if risk is None:
            from panaoptions.risk.guardrails import RiskManager
            risk = RiskManager(cfg)
        self.risk = risk

    # -- the numbers --------------------------------------------------- #
    @property
    def capital(self) -> float:
        return float(self.risk.capital)

    def is_index(self, symbol: str) -> bool:
        return symbol.upper() in index_symbols(self.cfg)

    def cap_pct(self, symbol: str) -> float:
        return cap_pct(self.cfg, symbol)

    def cap(self, symbol: str) -> float:
        """The most one trade on this symbol may spend on premium."""
        return round(self.capital * self.cap_pct(symbol) / 100.0, 2)

    def room(self) -> float:
        """What is left under the ceiling on everything open at once."""
        standard = float(self.cfg.get("risk.max_capital_deployed_pct", 20.0))
        total_pct = float(self.cfg.get("risk.max_total_deployed_pct", standard))
        return round(self.capital * total_pct / 100.0 - self.risk.state.deployed, 2)

    def budget_for(self, symbol: str) -> float:
        """The tighter of the symbol's cap and the room left in total."""
        return max(0.0, min(self.cap(symbol), self.room()))

    @property
    def max_spread_pct(self) -> float:
        return float(self.cfg.get("risk.max_spread_pct_of_mid",
                                  self.cfg.get("contracts.max_spread_pct_of_mid", 7.0)))

    def delta_bounds(self, band: tuple[float, float] | None) -> tuple[float, float]:
        """The delta a contract may have: the band, extended down to the
        fallback floor when one is configured (the budget fallback's reach)."""
        low = float(band[0]) if band else float(self.cfg.get("contracts.min_delta", 0.35))
        high = float(band[1]) if band else float(self.cfg.get("contracts.max_delta", 0.50))
        floor = float(self.cfg.get("contracts.budget_fallback_min_delta", 0.30) or 0)
        return (min(low, floor) if floor > 0 else low), high

    # -- the breaker --------------------------------------------------- #
    def drawdown(self, unrealised: float = 0.0) -> float:
        """Today's loss so far, realised plus open, as a positive number."""
        return round(max(0.0, -(self.risk.state.realised_pnl + unrealised)), 2)

    def breaker_tripped(self, unrealised: float = 0.0) -> bool:
        """True once today's drawdown reaches the daily limit.

        Latches: the first time it is true the RiskManager is halted for the
        rest of the session, whatever the open positions do next.
        """
        if self.risk.state.halted:
            return True
        limit = self.risk.daily_limit
        loss = self.drawdown(unrealised)
        if limit > 0 and loss >= limit:
            self.risk.state.halted = True
            self.risk.state.halt_reason = (
                f"daily drawdown ${loss:,.2f} reached the ${limit:,.2f} limit "
                f"({limit / self.capital:.0%} of capital) — trading stopped "
                f"for the day")
            log.warning("CIRCUIT BREAKER — %s", self.risk.state.halt_reason)
            return True
        return False

    # -- the gate ------------------------------------------------------ #
    def review(self, signal: Mapping[str, Any], contract: OptionContract,
               delta_band: tuple[float, float] | None = None,
               unrealised: float = 0.0) -> GateDecision:
        """Approve or refuse buying `contract` for `signal`.

        Every check runs, so a refusal lists everything wrong at once rather
        than one reason per cycle.
        """
        symbol = str(signal.get("symbol", contract.symbol))
        decision = GateDecision(approved=False, cap=self.cap(symbol),
                                cap_pct=self.cap_pct(symbol))
        ok, no = decision.passed.append, decision.rejected.append

        if self.breaker_tripped(unrealised):
            no(f"circuit breaker: {self.risk.state.halt_reason}")

        problems = validate_signal(signal)
        if problems:
            no("malformed signal: " + "; ".join(problems))
        else:
            ok("signal well formed")

        want = (OptionRight.CALL if signal.get("direction") == Direction.LONG.value
                else OptionRight.PUT)
        if contract.right is not want:
            no(f"{contract.label} is a {contract.right.value.lower()}, the "
               f"signal needs a {want.value.lower()}")

        # The spread is the rolling 1-minute volume-weighted figure when the
        # desk has one (see engine/liquidity.py), not one snapshot; for a
        # debit spread, each leg must pass on its own.
        mid = contract.mid
        legs = ([contract.long_leg, contract.short_leg] if contract.is_spread
                else [contract])
        one_sided = [leg.label for leg in legs if leg.mid <= 0 or not (leg.bid and leg.ask)]
        spread = contract.effective_spread_pct
        rolling = any(leg.rolling_spread_pct is not None for leg in legs)
        how = " (1-min volume-weighted)" if rolling else ""
        if mid <= 0 or one_sided:
            no(f"{', '.join(one_sided) or contract.label} has no two-sided market")
        elif spread > self.max_spread_pct:
            no(f"spread {spread:.1f}% of mid{how} is wider than "
               f"{self.max_spread_pct:g}%" + (" on a leg" if contract.is_spread else ""))
        else:
            ok(f"spread {spread:.1f}%{how} ≤ {self.max_spread_pct:g}%"
               + (" on both legs" if contract.is_spread else ""))
        if contract.is_spread:
            if contract.width <= mid:
                no(f"{contract.label} costs {mid:.2f} for {contract.width:g} of "
                   f"width — no profit is possible")
            else:
                ok(f"{contract.structure}: net debit {mid:.2f}, width "
                   f"{contract.width:g}, max loss is the debit")

        low, high = self.delta_bounds(delta_band)
        delta = abs(contract.delta)
        if not low <= delta <= high:
            no(f"delta {delta:.2f} outside {low:.2f}-{high:.2f}")
        else:
            ok(f"delta {delta:.2f} within {low:.2f}-{high:.2f}")

        cost = contract.cost(self.cfg.multiplier)
        if cost > decision.cap:
            no(f"one contract costs ${cost:,.2f}, over the {decision.cap_pct:g}% "
               f"cap of ${decision.cap:,.2f}")
        else:
            ok(f"${cost:,.2f} within the {decision.cap_pct:g}% cap "
               f"(${decision.cap:,.2f})"
               + (" — index exception" if self.is_index(symbol)
                  and decision.cap_pct > float(self.cfg.get(
                      "risk.max_capital_deployed_pct", 20.0)) else ""))
        room = self.room()
        if cost > room:
            no(f"${room:,.2f} of room left under the total ceiling, the "
               f"contract costs ${cost:,.2f}")

        decision.approved = not decision.rejected
        return decision
