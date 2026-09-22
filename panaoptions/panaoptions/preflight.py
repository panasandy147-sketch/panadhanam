"""Check the rules can all hold at once, BEFORE the market opens.

Every setting in this app is individually reasonable. The failures come from
combinations: a delta band that implies a price the budget forbids, a price cap
the deployment rule will never fund, a stop so tight the daily limit cannot be
reached. Each of those produces exactly the same symptom — a desk that scans
all morning and takes nothing — and that symptom is indistinguishable from a
genuinely quiet market until somebody does the arithmetic.

So the arithmetic runs at startup, and again on demand. A finding names the
setting, the number that makes it impossible, and what would fix it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from panaoptions.data.greeks import atm_premium_estimate
from panaoptions.logging import get_logger

log = get_logger("preflight")

# Rough spot and implied-vol levels for the default universe, used only to
# estimate what an at-the-money contract costs when no live chain is in hand.
# Wrong by 20% is still right about the conclusion.
_TYPICAL = {
    # The brief's universe — at the money costs $320-$1,800 a contract.
    "SPY": (570, 0.14), "QQQ": (495, 0.18), "AAPL": (230, 0.25),
    "NVDA": (180, 0.50), "TSLA": (420, 0.55), "AMD": (160, 0.45),
    "MSFT": (425, 0.22), "AMZN": (225, 0.30),
    # Liquid names a small account can actually buy at the money.
    "IWM": (225, 0.20), "PLTR": (40, 0.55), "INTC": (25, 0.45),
    "SOFI": (14, 0.60), "HOOD": (30, 0.60), "F": (11, 0.35),
    "XLF": (48, 0.15), "GDX": (40, 0.35),
}


@dataclass
class Finding:
    level: str          # "blocker" | "warning"
    setting: str
    problem: str
    fix: str

    def render(self) -> str:
        mark = "BLOCKER" if self.level == "blocker" else "warning"
        return (f"  [{mark}] {self.setting}\n"
                f"      {self.problem}\n"
                f"      Fix: {self.fix}")


def _atm_cost(symbol: str, dte: int, multiplier: int) -> float | None:
    spot_iv = _TYPICAL.get(symbol.upper())
    if not spot_iv:
        return None
    spot, iv = spot_iv
    return atm_premium_estimate(spot, iv, dte) * multiplier


def check(cfg) -> list[Finding]:
    """Everything that cannot work, in the order it will bite."""
    findings: list[Finding] = []
    multiplier = cfg.multiplier
    capital = cfg.capital

    max_price = float(cfg.get("contracts.max_contract_price", 1.0)) * multiplier
    min_price = float(cfg.get("contracts.min_contract_price", 0.0)) * multiplier
    min_delta = float(cfg.get("contracts.min_delta", 0.45))
    max_delta = float(cfg.get("contracts.max_delta", 0.60))
    deployed_pct = float(cfg.get("risk.max_capital_deployed_pct", 20.0))
    budget = capital * deployed_pct / 100.0
    max_dte = int(cfg.get("contracts.max_dte", 14))

    # 1. Can the deployment rule fund a contract the filter would accept?
    #    This is the one that bites SECOND, after the price cap is raised, and
    #    it is the reason raising the cap alone changes nothing.
    if min_price > budget:
        needed = min_price / (deployed_pct / 100.0)
        findings.append(Finding(
            "blocker", "risk.max_capital_deployed_pct vs contracts.min_contract_price",
            f"{deployed_pct:.0f}% of ${capital:,.0f} is ${budget:,.0f}, but the "
            f"filter will not accept a contract cheaper than ${min_price:,.0f}. "
            f"The risk manager refuses every contract the filter accepts.",
            f"Raise account.starting_capital to about ${needed:,.0f}, or lower "
            f"contracts.min_contract_price below ${budget / multiplier:.2f}."))

    # 2. Is the delta band reachable inside the price cap, symbol by symbol?
    unaffordable: list[tuple[str, float]] = []
    for symbol in cfg.symbols:
        cost = _atm_cost(symbol, max_dte, multiplier)
        if cost is None:
            continue
        if min_delta >= 0.40 and cost > max_price:
            unaffordable.append((symbol, cost))

    if unaffordable:
        worst = max(cost for _, cost in unaffordable)
        names = ", ".join(s for s, _ in unaffordable)
        findings.append(Finding(
            "blocker", "contracts.max_contract_price vs contracts.min_delta",
            f"{min_delta:.2f}-{max_delta:.2f} delta is at the money, and an "
            f"at-the-money {max_dte}-day contract costs more than the "
            f"${max_price:,.0f} cap on: {names} (up to ${worst:,.0f}).",
            f"Raise contracts.max_contract_price to about "
            f"{worst / multiplier:.2f} per share (${worst:,.0f} a contract), "
            f"trade cheaper underlyings, or lower contracts.min_delta."))

    # 3. Can the account afford ANY at-the-money contract in this universe?
    affordable_now = []
    for symbol in cfg.symbols:
        cost = _atm_cost(symbol, int(cfg.get("contracts.min_dte", 7)), multiplier)
        if cost is not None and cost <= budget:
            affordable_now.append(symbol)

    if min_delta >= 0.40 and not affordable_now:
        cheapest = min((c for c in (_atm_cost(s, int(cfg.get("contracts.min_dte", 7)),
                                              multiplier) for s in cfg.symbols)
                        if c is not None), default=0.0)
        if cheapest:
            alternative = cfg.get("universe.small_account_alternative") or []
            fits = [s for s in alternative
                    if (_atm_cost(s, int(cfg.get("contracts.min_dte", 7)),
                                  multiplier) or 1e9) <= budget]
            fix = (f"Either raise account.starting_capital to about "
                   f"${cheapest / (deployed_pct / 100.0):,.0f} (or set "
                   f"PANAOPTIONS_CAPITAL in the environment)")
            if fits:
                fix += (f", or keep ${capital:,.0f} and change universe.symbols "
                        f"to names this account can actually buy at the money: "
                        f"{', '.join(fits)}.")
            else:
                fix += "."
            findings.append(Finding(
                "blocker", "account.starting_capital",
                f"The cheapest at-the-money contract in this universe costs "
                f"about ${cheapest:,.0f}, and {deployed_pct:.0f}% of "
                f"${capital:,.0f} is ${budget:,.0f}. No symbol on the "
                f"watchlist is tradeable at this account size.",
                fix))

    # 4. Would a single stop-out blow more than the whole day's budget?
    stop_pct = float(cfg.get("risk.stop_loss_pct", 20.0))
    absolute = cfg.get("risk.daily_loss_limit")
    daily_limit = (abs(float(absolute)) if absolute not in (None, "", 0, 0.0)
                   else capital * float(cfg.get("risk.daily_loss_limit_pct", 10.0)) / 100.0)
    risk_per_trade = budget * stop_pct / 100.0
    if risk_per_trade > daily_limit:
        findings.append(Finding(
            "warning", "risk.daily_loss_limit vs risk.stop_loss_pct",
            f"One stop-out risks ${risk_per_trade:,.2f}, which already exceeds "
            f"the ${daily_limit:,.2f} daily limit. The breaker trips on the "
            f"first loser, so the limit is really a one-trade-a-day rule.",
            f"Raise risk.daily_loss_limit above ${risk_per_trade:,.2f} to allow "
            f"more than one attempt, or cut risk.max_capital_deployed_pct."))

    # 4b. Can the scale-out rule ever run? Half of one contract is not a
    #     thing, so a budget that only ever funds a single contract makes the
    #     "exit 50% at +40%, trail the rest" rule quietly inert — the position
    #     closes whole at the first target and the runner never exists.
    if min_delta >= 0.40:
        costs = [c for c in (_atm_cost(sym, int(cfg.get("contracts.min_dte", 7)),
                                       multiplier) for sym in cfg.symbols)
                 if c is not None and c <= budget]
        if costs and budget / min(costs) < 2:
            cheapest = min(costs)
            findings.append(Finding(
                "warning", "risk.take_profit_1_size_pct",
                f"A ${budget:,.0f} budget buys one contract at "
                f"${cheapest:,.0f}, and half a contract does not exist. The "
                f"scale-out closes the whole position at the first target, so "
                f"the +{cfg.get('risk.take_profit_2_pct', 70)}% target and the "
                f"EMA trail never come into play.",
                f"About ${cheapest * 2 / (deployed_pct / 100.0):,.0f} of "
                f"capital funds two contracts on the cheapest name, which is "
                f"what the scale-out rule needs. Until then the trade is "
                f"all-or-nothing at +{cfg.get('risk.take_profit_1_pct', 40)}%."))

    # 5. Is the risk per trade far outside what the stated rules imply?
    real_risk_pct = deployed_pct * stop_pct / 100.0
    # A conventional desk risks 1-2% per trade. Past 3% is a different game,
    # and worth saying out loud even though it is a legitimate choice.
    if real_risk_pct > 3.0:
        # Losses compound on a shrinking account, so this is a log, not 50
        # divided by the percentage — the linear version overstates the run of
        # losers it takes by about half.
        to_halve = int(math.log(0.5) / math.log(1 - real_risk_pct / 100.0))
        findings.append(Finding(
            "warning", "risk.max_capital_deployed_pct",
            f"Deploying {deployed_pct:.0f}% behind a {stop_pct:.0f}% stop risks "
            f"{real_risk_pct:.1f}% of the account per trade. "
            f"{to_halve} consecutive losers would halve it.",
            "This is a deliberate choice on a small paper account, not an "
            "error. Lower risk.max_capital_deployed_pct if you did not mean it."))

    if min_price > max_price:
        findings.append(Finding(
            "blocker", "contracts.min_contract_price",
            f"The minimum (${min_price:,.0f}) is above the maximum "
            f"(${max_price:,.0f}), so no contract can ever qualify.",
            "Set min below max."))

    return findings


def report(cfg, log_it: bool = True) -> list[Finding]:
    """Run the checks and say what was found. Returns the findings."""
    findings = check(cfg)
    blockers = [f for f in findings if f.level == "blocker"]

    if log_it:
        for finding in blockers:
            log.error("%s — %s", finding.setting, finding.problem)
        for finding in (f for f in findings if f.level == "warning"):
            log.warning("%s — %s", finding.setting, finding.problem)
        if blockers:
            log.error("The desk will scan and take nothing until these are "
                      "fixed. Run `python run.py --check-config` for the fixes.")
    return findings
