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
    # Capital currently at work across every open position. Held here rather
    # than recomputed, because the sizing rule has to see the total before it
    # agrees to add to it.
    deployed: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    rejections: dict[str, int] = field(default_factory=dict)


class RiskManager:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.capital = cfg.capital
        self.state = DayState()

    @property
    def daily_limit(self) -> float:
        """The day's loss budget, as a positive number.

        A percentage of capital by default so it scales with the account; an
        absolute `daily_loss_limit` overrides it when one is set.
        """
        absolute = self.cfg.get("risk.daily_loss_limit")
        if absolute not in (None, "", 0, 0.0):
            return abs(float(absolute))
        pct = float(self.cfg.get("risk.daily_loss_limit_pct", 10.0))
        return round(self.capital * pct / 100.0, 2)

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

        limit = self.daily_limit
        if not self.state.halted and self.state.realised_pnl <= -limit:
            self.state.halted = True
            self.state.halt_reason = (
                f"daily loss limit hit: {self.state.realised_pnl:+.2f} against a "
                f"-{limit:.2f} limit. No more entries today.")
            log.warning("CIRCUIT BREAKER — %s", self.state.halt_reason)

    @property
    def remaining_loss_budget(self) -> float:
        return round(max(self.daily_limit + min(self.state.realised_pnl, 0.0),
                         0.0), 2)

    def _reject(self, reason: str) -> tuple[None, str]:
        key = reason.split(":")[0].split("—")[0].strip()[:60]
        self.state.rejections[key] = self.state.rejections.get(key, 0) + 1
        return None, reason

    # ------------------------------------------------------------------ #
    def budget_room(self) -> float:
        """What one new trade may spend on premium right now.

        The tighter of the per-trade cap and the room left under the total
        ceiling — exactly what `size` will enforce, so the contract picker can
        choose something `size` will then accept.
        """
        deployed_pct = float(self.cfg.get("risk.max_capital_deployed_pct", 20.0))
        total_pct = float(self.cfg.get("risk.max_total_deployed_pct", deployed_pct))
        per_trade = self.capital * deployed_pct / 100.0
        room = self.capital * total_pct / 100.0 - self.state.deployed
        return max(0.0, min(per_trade, room))

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

        # Per-trade first. When nothing is open this is the binding rule, and
        # "20% of $500 is $100, the contract is $460" is the answer somebody
        # needs — the total ceiling would refuse the same trade with a number
        # that explains less.
        if budget < cost_per_contract:
            return self._reject(
                f"One {contract.label} costs ${cost_per_contract:,.2f}, and "
                f"{deployed_pct:.0f}% of ${self.capital:,.2f} is ${budget:,.2f}. "
                f"Not even one contract fits.")

        # Then the total. The per-trade cap is not the whole rule once more
        # than one position can be open: three trades at 20% each satisfy it
        # every single time and deploy 60% of the account.
        total_pct = float(self.cfg.get("risk.max_total_deployed_pct",
                                       deployed_pct))
        room = self.capital * total_pct / 100.0 - self.state.deployed
        if room < cost_per_contract:
            return self._reject(
                f"${self.state.deployed:,.2f} is already at work across "
                f"{self.state.open_trades} position(s), and the ceiling is "
                f"{total_pct:.0f}% of ${self.capital:,.2f} "
                f"(${self.capital * total_pct / 100:,.2f}). One "
                f"{contract.label} costs ${cost_per_contract:,.2f} and there "
                f"is ${max(room, 0):,.2f} of room.")

        # Whichever is tighter decides the size.
        budget = min(budget, room)
        quantity = int(budget // cost_per_contract)
        if quantity < 1:
            return self._reject(
                f"One {contract.label} costs ${cost_per_contract:,.2f}, and "
                f"{deployed_pct:.0f}% of ${self.capital:,.2f} is ${budget:,.2f}. "
                f"Not even one contract fits.")

        # Never let rounding push the position past the budget.
        while quantity > 1 and quantity * cost_per_contract > budget:
            quantity -= 1

        # In underlying mode the percentage stop is a DISASTER backstop: the
        # strategy's own invalidation level is what normally fires, and it is
        # checked against the underlying rather than the premium. A tight
        # percentage stop here would take the trade out on an IV wobble that
        # says nothing about whether the thesis was wrong.
        underlying_mode = str(self.cfg.get("risk.stop_mode", "premium")) == "underlying"
        stop_pct = float(self.cfg.get(
            "risk.disaster_stop_pct" if underlying_mode else "risk.stop_loss_pct",
            45.0 if underlying_mode else 20.0))
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
            underlying_target=setup.underlying_target,
            strategy=setup.strategy,
            invalidation_note=setup.invalidation_note,
            key_level=setup.key_level,
            key_level_source=setup.key_level_source,
            reasoning=list(setup.reasoning),
            pattern=setup.pattern, claimed_accuracy=setup.claimed_accuracy,
            confirmations=list(setup.confirmations),
            ml_probability=ml_probability,
        )

        deployed = signal.cost(multiplier)
        at_risk = signal.risk_at_stop(multiplier)
        log.info("%s | %s | deploying $%.2f (%.1f%% of account); backstop at "
                 "$%.2f (%.1f%%) — real exit: %s",
                 signal.alert_line(), setup.strategy.value, deployed,
                 deployed / self.capital * 100, at_risk,
                 at_risk / self.capital * 100,
                 setup.invalidation_note or "the underlying level")
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
            "daily_loss_limit": self.daily_limit,
            "stop_mode": str(self.cfg.get("risk.stop_mode", "premium")),
            "exit_style": str(self.cfg.get("risk.exit_style", "scale")),
            "remaining_loss_budget": self.remaining_loss_budget,
            "realised_pnl": round(self.state.realised_pnl, 2),
            "trades_taken": self.state.trades_taken,
            "wins": self.state.wins,
            "losses": self.state.losses,
            "open_trades": self.state.open_trades,
            "deployed": round(self.state.deployed, 2),
            "max_total_deployed_pct": float(self.cfg.get(
                "risk.max_total_deployed_pct",
                self.cfg.get("risk.max_capital_deployed_pct", 20.0))),
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
        }
