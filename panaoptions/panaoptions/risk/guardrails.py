"""The $500 account rules. Deterministic, and never negotiable.

No model, no prompt and no configuration reload can talk this module into a
bigger position. It takes a setup and a contract and returns either a fully
specified signal or a refusal with a reason.

One number deserves stating plainly, because the specification mixes two
different things that both get called "risk":

    capital DEPLOYED  = the premium paid          = up to 20% of the account
    capital AT RISK   = premium x the stop        = 20% of that = 4%

A 20%-of-account position with a 20% stop risks 4% of the account, not 20%.
Both numbers appear on every signal so the distinction never blurs. 4% per
trade is still roughly four times what a conventional desk risks, which is a
choice the account owner has made deliberately — it is not hidden here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from panaoptions.logging import get_logger
from panaoptions.models import OptionContract, Setup, Signal

log = get_logger("risk")


@dataclass
class DayState:
    """Resets each session. Losses accumulate; the breaker latches."""
    date: str = ""
    realised_pnl: float = 0.0
    trades_taken: int = 0
    wins: int = 0
    losses: int = 0
    open_trades: int = 0
    halted: bool = False
    halt_reason: str = ""
    rejections: dict[str, int] = field(default_factory=dict)


class RiskManager:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.capital = cfg.capital
        self.state = DayState()

    # ------------------------------------------------------------------ #
    def roll_day(self, today: str) -> None:
        """A new session clears yesterday's counters, including the halt."""
        if self.state.date == today:
            return
        if self.state.date:
            log.info("new session %s — resetting daily counters (yesterday: "
                     "%+.2f over %d trades)", today, self.state.realised_pnl,
                     self.state.trades_taken)
        self.state = DayState(date=today)

    def record_pnl(self, amount: float) -> None:
        """Book a realised amount and trip the breaker if the day is done."""
        self.state.realised_pnl += amount
        if amount > 0:
            self.state.wins += 1
        elif amount < 0:
            self.state.losses += 1

        limit = float(self.cfg.get("risk.daily_loss_limit", 50.0))
        if not self.state.halted and self.state.realised_pnl <= -abs(limit):
            self.state.halted = True
            self.state.halt_reason = (
                f"daily loss limit hit: {self.state.realised_pnl:+.2f} against a "
                f"-{abs(limit):.2f} limit. No more entries today.")
            log.warning("CIRCUIT BREAKER — %s", self.state.halt_reason)

    @property
    def remaining_loss_budget(self) -> float:
        limit = abs(float(self.cfg.get("risk.daily_loss_limit", 50.0)))
        return round(max(limit + min(self.state.realised_pnl, 0.0), 0.0), 2)

    def _reject(self, reason: str) -> tuple[None, str]:
        key = reason.split(":")[0].split("—")[0].strip()[:60]
        self.state.rejections[key] = self.state.rejections.get(key, 0) + 1
        return None, reason

    # ------------------------------------------------------------------ #
    def size(self, setup: Setup, contract: OptionContract,
             signal_id: str, ts: datetime,
             ml_probability: float | None = None) -> tuple[Signal | None, str]:
        """Turn a setup plus a contract into a sized signal, or refuse."""
        if self.state.halted:
            return self._reject(f"Desk halted: {self.state.halt_reason}")

        max_open = int(self.cfg.get("risk.max_open_trades", 1))
        if self.state.open_trades >= max_open:
            return self._reject(
                f"Already holding {self.state.open_trades} position(s) and the "
                f"limit is {max_open}. One trade at a time is the rule that "
                f"stops a bad morning compounding.")

        entry = contract.mid
        if entry <= 0:
            return self._reject(f"{contract.label} has no two-sided market.")

        multiplier = self.cfg.multiplier
        cost_per_contract = entry * multiplier

        deployed_pct = float(self.cfg.get("risk.max_capital_deployed_pct", 20.0))
        budget = self.capital * deployed_pct / 100.0
        quantity = int(budget // cost_per_contract)
        if quantity < 1:
            return self._reject(
                f"One {contract.label} costs ${cost_per_contract:,.2f}, and "
                f"{deployed_pct:.0f}% of ${self.capital:,.2f} is ${budget:,.2f}. "
                f"Not even one contract fits.")

        # Never let rounding push the position past the budget.
        while quantity > 1 and quantity * cost_per_contract > budget:
            quantity -= 1

        stop_pct = float(self.cfg.get("risk.stop_loss_pct", 20.0))
        tp1_pct = float(self.cfg.get("risk.take_profit_1_pct", 40.0))
        tp2_pct = float(self.cfg.get("risk.take_profit_2_pct", 70.0))

        stop = round(entry * (1 - stop_pct / 100.0), 2)
        target_1 = round(entry * (1 + tp1_pct / 100.0), 2)
        target_2 = round(entry * (1 + tp2_pct / 100.0), 2)

        if stop <= 0 or stop >= entry:
            return self._reject(
                f"A {stop_pct:.0f}% stop on a ${entry:.2f} contract is not a "
                f"usable level.")

        signal = Signal(
            id=signal_id, ts=ts, symbol=setup.symbol, direction=setup.direction,
            contract=contract, quantity=quantity, entry_price=entry,
            stop_price=stop, target_1=target_1, target_2=target_2,
            underlying_at_entry=setup.indicators.close,
            underlying_support=setup.underlying_support,
            pattern=setup.pattern, confirmations=list(setup.confirmations),
            ml_probability=ml_probability,
        )

        deployed = signal.cost(multiplier)
        at_risk = signal.risk_at_stop(multiplier)
        log.info("%s | deploying $%.2f (%.1f%% of account), risking $%.2f "
                 "(%.1f%%) if the stop fills",
                 signal.alert_line(), deployed, deployed / self.capital * 100,
                 at_risk, at_risk / self.capital * 100)
        return signal, ""

    # ------------------------------------------------------------------ #
    def describe(self) -> dict:
        capital = self.capital
        deployed_pct = float(self.cfg.get("risk.max_capital_deployed_pct", 20.0))
        stop_pct = float(self.cfg.get("risk.stop_loss_pct", 20.0))
        return {
            "capital": capital,
            "max_deployed_per_trade": round(capital * deployed_pct / 100, 2),
            "max_deployed_pct": deployed_pct,
            # The number that actually matters, spelled out.
            "risk_per_trade_pct": round(deployed_pct * stop_pct / 100, 2),
            "risk_per_trade": round(capital * deployed_pct * stop_pct / 10000, 2),
            "daily_loss_limit": float(self.cfg.get("risk.daily_loss_limit", 50.0)),
            "remaining_loss_budget": self.remaining_loss_budget,
            "realised_pnl": round(self.state.realised_pnl, 2),
            "trades_taken": self.state.trades_taken,
            "wins": self.state.wins,
            "losses": self.state.losses,
            "open_trades": self.state.open_trades,
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
        }
