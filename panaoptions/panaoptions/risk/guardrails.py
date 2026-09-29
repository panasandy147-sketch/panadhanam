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
from panaoptions.risk.gatekeeper import cap_pct, index_symbols

log = get_logger("risk")


def planned_loss_per_contract(cfg, contract: OptionContract, spot: float = 0.0,
                              stop_underlying: float = 0.0) -> float:
    """What one contract loses if the trade is wrong — the first exit to fire.

    In underlying mode the structural stop normally fires first: the option
    loses about |delta| x the distance from the underlying to its stop. The
    premium backstop (45%) caps it, and a debit spread can never lose more
    than its debit. In premium mode it is the premium stop.
    """
    delta = (abs(contract.long_leg.delta) - abs(contract.short_leg.delta)
             if contract.is_spread else abs(contract.delta))
    return planned_loss(cfg, contract.mid, delta, contract.multiplier or cfg.multiplier,
                        spot, stop_underlying, contract.is_spread)


def planned_loss(cfg, mid: float, delta: float, mult: int, spot: float = 0.0,
                 stop_underlying: float = 0.0, is_spread: bool = False) -> float:
    """planned_loss_per_contract from the numbers alone (the backtest's form)."""
    underlying_mode = str(cfg.get("risk.stop_mode", "premium")) == "underlying"
    stop_pct = float(cfg.get("risk.disaster_stop_pct" if underlying_mode
                             else "risk.stop_loss_pct", 45.0 if underlying_mode else 20.0))
    loss = mid * stop_pct / 100.0 * mult
    if underlying_mode and spot and stop_underlying and delta > 0:
        loss = min(loss, delta * abs(spot - stop_underlying) * mult)
    if is_spread:
        loss = min(loss, mid * mult)
    return round(max(loss, 0.0), 2)


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
    def roll_day(self, today: str) -> bool:
        """A new session clears yesterday's counters, including the halt.
        True when the day changed (the caller restores today's saved state)."""
        if self.state.date == today:
            return False
        if self.state.date:
            log.info("new session %s — resetting daily counters (yesterday: "
                     "%+.2f over %d trades)", today, self.state.realised_pnl,
                     self.state.trades_taken)
        self.state = DayState(date=today)
        return True

    @property
    def risk_per_trade(self) -> float:
        """The most one trade may lose at its stop (0 = no such cap)."""
        pct = float(self.cfg.get("risk.max_risk_per_trade_pct", 0) or 0)
        return round(self.capital * pct / 100.0, 2) if pct > 0 else 0.0

    def halt(self, reason: str) -> None:
        """Latch the breaker for the rest of the calendar day."""
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            log.warning("CIRCUIT BREAKER — %s", reason)

    def record_pnl(self, amount: float) -> None:
        """Book a realised amount and trip the breaker if the day is done."""
        self.state.realised_pnl += amount
        if amount > 0:
            self.state.wins += 1
        elif amount < 0:
            self.state.losses += 1

        limit = self.daily_limit
        if not self.state.halted and self.state.realised_pnl <= -limit:
            self.halt(f"daily loss limit hit: {self.state.realised_pnl:+.2f} against a "
                      f"-{limit:.2f} limit. Locked out for the rest of the day.")

    @property
    def remaining_loss_budget(self) -> float:
        return round(max(self.daily_limit + min(self.state.realised_pnl, 0.0),
                         0.0), 2)

    def _reject(self, reason: str) -> tuple[None, str]:
        key = reason.split(":")[0].split("—")[0].strip()[:60]
        self.state.rejections[key] = self.state.rejections.get(key, 0) + 1
        return None, reason

    # ------------------------------------------------------------------ #
    def budget_room(self, symbol: str | None = None) -> float:
        """What one new trade may spend on premium right now.

        The tighter of the per-trade cap and the room left under the total
        ceiling — exactly what `size` will enforce, so the contract picker can
        choose something `size` will then accept. With a symbol, the index
        ETFs get their higher cap (see risk/gatekeeper.py).
        """
        standard = float(self.cfg.get("risk.max_capital_deployed_pct", 20.0))
        deployed_pct = cap_pct(self.cfg, symbol) if symbol else standard
        total_pct = float(self.cfg.get("risk.max_total_deployed_pct", standard))
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

        max_daily = int(self.cfg.get("risk.max_daily_trades", 0) or 0)
        if max_daily and self.state.trades_taken >= max_daily:
            return self._reject(
                f"Daily trade limit: {self.state.trades_taken} of {max_daily} "
                f"taken today. No more entries until tomorrow — over-trading is "
                f"how a good morning is given back.")

        entry = contract.mid
        if entry <= 0:
            return self._reject(f"{contract.label} has no two-sided market.")

        multiplier = contract.multiplier or self.cfg.multiplier
        cost_per_contract = entry * multiplier

        deployed_pct = cap_pct(self.cfg, setup.symbol)
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
        total_pct = float(self.cfg.get(
            "risk.max_total_deployed_pct",
            self.cfg.get("risk.max_capital_deployed_pct", 20.0)))
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

        # Risk per trade: what the position loses at its first stop must fit
        # risk.max_risk_per_trade_pct (2% = $80 on $4,000). Deployment above
        # caps the premium; this caps the LOSS, and the tighter one decides.
        cap = self.risk_per_trade
        if cap > 0:
            per = planned_loss_per_contract(self.cfg, contract, setup.indicators.close,
                                            setup.underlying_support)
            cur = str(self.cfg.get("account.currency", "$") or "$")
            if per > cap:
                return self._reject(
                    f"Risk per trade: one {contract.label} loses {cur}{per:,.2f} at its "
                    f"stop, over the {self.cfg.get('risk.max_risk_per_trade_pct')}% "
                    f"({cur}{cap:,.2f}) a trade may risk.")
            if per > 0:
                quantity = min(quantity, int(cap // per))

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
        if contract.is_spread:
            # A debit spread is worth at most its width: a target above that
            # can never fill. Cap the first at half the remaining room and the
            # second at 90% of it.
            room_up = contract.width - entry
            target_1 = min(target_1, round(entry + room_up * 0.5, 2))
            target_2 = min(target_2, round(entry + room_up * 0.9, 2))

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
        cur = str(self.cfg.get("account.currency", "$") or "$")
        log.info("%s | %s | deploying %s%.2f (%.1f%% of account); backstop at "
                 "%s%.2f (%.1f%%) — real exit: %s",
                 signal.alert_line(), setup.strategy.value, cur, deployed,
                 deployed / self.capital * 100, cur, at_risk,
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
            "index_symbols": sorted(index_symbols(self.cfg)),
            "index_max_deployed_pct": float(self.cfg.get(
                "risk.index_max_capital_deployed_pct", deployed_pct)),
            "index_max_deployed_per_trade": round(capital * float(self.cfg.get(
                "risk.index_max_capital_deployed_pct", deployed_pct)) / 100, 2),
            # The number that actually matters, spelled out: the risk cap when
            # one is set, else what the deployment cap and the stop imply.
            "risk_per_trade_pct": (float(self.cfg.get("risk.max_risk_per_trade_pct"))
                                   if self.risk_per_trade
                                   else round(deployed_pct * stop_pct / 100, 2)),
            "risk_per_trade": (self.risk_per_trade or
                               round(capital * deployed_pct * stop_pct / 10000, 2)),
            "max_open_trades": int(self.cfg.get("risk.max_open_trades", 1)),
            "max_daily_trades": int(self.cfg.get("risk.max_daily_trades", 0) or 0),
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
