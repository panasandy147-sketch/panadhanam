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
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from panaoptions.data.greeks import atm_premium_estimate, premium_estimate
from panaoptions.logging import get_logger

log = get_logger("preflight")

# Rough spot and implied-vol levels for the default universe, used only to
# estimate what a contract costs when no live chain is in hand. Wrong by 20%
# is still right about the conclusion, which is all this is for — a stale
# figure here changes a warning's dollar amount, never whether it fires.
#
# These drift. `python run.py --explain-contracts` prices the real chain and
# is the answer whenever the exact number matters.
_TYPICAL = {
    # The brief's universe — at the money costs $320-$1,800 a contract.
    "SPY": (570, 0.14), "QQQ": (740, 0.18), "AAPL": (230, 0.25),
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
    # A literal command that resolves this, when one exists. Describing a fix
    # in prose still leaves somebody working out what to type, and "set
    # PANAOPTIONS_CAPITAL in the environment" is not a command you can paste.
    command: str = ""

    def render(self) -> str:
        mark = "BLOCKER" if self.level == "blocker" else "warning"
        out = (f"  [{mark}] {self.setting}\n"
               f"      {self.problem}\n"
               f"      Fix: {self.fix}")
        if self.command:
            out += f"\n\n      Run this:\n          {self.command}"
        return out


def _python() -> str:
    """How to invoke this project's Python, written the way it must be typed.

    A bare `python` on Windows is the Microsoft Store build and has none of
    the packages, so a suggested command that starts with `python` sends
    people straight back to a ModuleNotFoundError.
    """
    here = Path(__file__).resolve().parent.parent
    windows = sys.platform.startswith("win")
    folder, exe = ("Scripts", "python.exe") if windows else ("bin", "python")
    sep = "\\" if windows else "/"
    for base, label in ((here, "."), (here.parent, "..")):
        if (base / ".venv" / folder / exe).exists():
            return f"{label}{sep}.venv{sep}{folder}{sep}{exe}"
    return "python"


def _shipped_capital(cfg) -> float:
    """What settings.yaml asks for, ignoring the .env override."""
    import yaml

    try:
        with open(cfg.path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return float((raw.get("account") or {}).get("starting_capital") or 0.0)
    except (OSError, ValueError, yaml.YAMLError):
        return 0.0


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
            # Round up to a tidy figure — nobody sets capital to $1,593.
            needed = cheapest / (deployed_pct / 100.0)
            suggested = int(math.ceil(needed / 500.0) * 500)

            fix = (f"Either raise capital to about ${needed:,.0f}, or keep "
                   f"${capital:,.0f} and change universe.symbols")
            if fits:
                fix += (f" to names this account can actually buy at the money: "
                        f"{', '.join(fits)}.")
            else:
                fix += " to cheaper underlyings."
            findings.append(Finding(
                "blocker", "account.starting_capital",
                f"The cheapest at-the-money contract in this universe costs "
                f"about ${cheapest:,.0f}, and {deployed_pct:.0f}% of "
                f"${capital:,.0f} is ${budget:,.0f}. No symbol on the "
                f"watchlist is tradeable at this account size.",
                fix,
                command=f"{_python()} run.py --set PANAOPTIONS_CAPITAL={suggested}"))

    # 3b. Is a per-machine override holding capital below the shipped value?
    #
    # PANAOPTIONS_CAPITAL in .env wins over settings.yaml, by design — but it
    # is written once and then forgotten, so raising the figure in the repo
    # changes nothing for anyone who has one. Silently keeping the old number
    # after an upgrade that was meant to raise it is the kind of stale state
    # nobody goes looking for.
    pinned = os.getenv("PANAOPTIONS_CAPITAL")
    shipped = _shipped_capital(cfg)
    if pinned and shipped and capital < shipped:
        findings.append(Finding(
            "warning", "PANAOPTIONS_CAPITAL",
            f"Your .env pins capital at ${capital:,.0f}, below the "
            f"${shipped:,.0f} this version ships with. That is "
            f"${capital * deployed_pct / 100:,.0f} a trade instead of "
            f"${shipped * deployed_pct / 100:,.0f}, which is what the pattern "
            f"delta bands were sized against.",
            "Raise it to match, or keep yours if the smaller figure is "
            "deliberate — it is your machine's setting and nothing will "
            "overwrite it.",
            command=f"{_python()} run.py --set PANAOPTIONS_CAPITAL="
                    f"{int(shipped)}"))

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

    # 6. Can the budget buy the contract each pattern asks for?
    #
    # This is the trap in per-pattern contract selection. A pattern that asks
    # for 0.70 delta at 45 DTE is asking for a much more expensive instrument
    # than the 0.50-delta default, and the setups with the best published
    # numbers are exactly the ones that ask for the most. Without this check
    # the highest-conviction pattern on the list would fire, find no contract,
    # and log a line nobody reads — which looks identical to it never firing.
    patterns_cfg = cfg.get("strategies.candlestick_at_level.patterns", {}) or {}
    if bool(cfg.get("strategies.candlestick_at_level.enabled", True)):
        out_of_reach: list[tuple[str, float, float, int]] = []
        allowed = cfg.get("strategies.candlestick_at_level.allowed_patterns")
        allowed_keys = ({a.lower().replace(" ", "_").replace("-", "_") for a in allowed}
                        | ({"liquidity_sweep_rejection"} if allowed and
                           {"Hammer", "Shooting Star"} & set(allowed) else set())
                        if allowed else None)
        for key, block in patterns_cfg.items():
            if not isinstance(block, dict):
                continue
            if allowed_keys is not None and key not in allowed_keys:
                continue    # a pattern this profile does not trade
            band = block.get("delta") or []
            window = block.get("dte") or []
            if len(band) != 2 or len(window) != 2:
                continue
            wanted_delta, wanted_dte = float(band[0]), int(window[0])
            # A 0-DTE contract expires today, not in zero time: priced at zero
            # days every same-day band would come back free and pass this
            # check trivially, which is the opposite of useful. Roughly a
            # third of a session is left when these actually get bought.
            priced_dte = wanted_dte if wanted_dte > 0 else 0.3
            costs = [premium_estimate(spot, iv, priced_dte, wanted_delta)
                     * multiplier
                     for spot, iv in (_TYPICAL.get(sym.upper()) or (0, 0)
                                      for sym in cfg.symbols)
                     if spot]
            if costs and min(costs) > budget:
                out_of_reach.append((key, min(costs), wanted_delta, wanted_dte))

        if out_of_reach:
            out_of_reach.sort(key=lambda row: row[1])
            listed = "; ".join(
                f"{key.replace('_', ' ')} ({d:.2f}d/{dte}DTE, ~${cost:,.0f})"
                for key, cost, d, dte in out_of_reach)
            cheapest = out_of_reach[0][1]
            alternative = cfg.get("universe.small_account_alternative", []) or []
            fix = (f"Fund it — about ${cheapest / (deployed_pct / 100.0):,.0f} "
                   f"of capital at {deployed_pct:.0f}% deployment reaches the "
                   f"cheapest of them")
            if alternative:
                fix += (f" — or trade the smaller names already in the config "
                        f"({', '.join(alternative[:4])}, ...) by moving "
                        f"universe.small_account_alternative into "
                        f"universe.symbols")
            fix += (". Otherwise these are patterns this account will watch "
                    "rather than trade, which is a legitimate choice as long "
                    "as it is a choice.")
            findings.append(Finding(
                "warning", "strategies.candlestick_at_level.patterns",
                f"{len(out_of_reach)} of {len(patterns_cfg)} patterns ask for "
                f"a contract no name in the universe offers inside the "
                f"${budget:,.0f} per-trade budget, so they can fire and never "
                f"be filled: {listed}.",
                fix))

    # 7. What does holding the maximum actually cost?
    #
    # "Three positions" is an abstraction until it is a dollar figure, and the
    # per-trade risk number everybody reads is for ONE trade. Three at once is
    # three times that, correlated — index ETFs and megacaps go down together
    # — so it is worth saying out loud before a bad morning says it instead.
    max_open = int(cfg.get("risk.max_open_trades", 1))
    if max_open > 1:
        total_pct = float(cfg.get("risk.max_total_deployed_pct", deployed_pct))
        at_work = capital * total_pct / 100.0
        # The BACKSTOP, not the percentage stop. In underlying mode the
        # strategy's level normally fires first, but what a position can lose
        # before anything mechanical stops it is the disaster stop — and that
        # is the number this warning exists to state.
        underlying_mode = str(cfg.get("risk.stop_mode", "premium")) == "underlying"
        backstop_pct = float(cfg.get("risk.disaster_stop_pct", 45.0)
                             if underlying_mode else stop_pct)
        worst = at_work * backstop_pct / 100.0
        loss_limit_pct = float(cfg.get("risk.daily_loss_limit_pct", 0) or 0)
        loss_limit = capital * loss_limit_pct / 100.0
        note = ""
        if loss_limit and loss_limit < worst:
            note = (f" In practice the daily loss limit halts the desk at "
                    f"-${loss_limit:,.0f} ({loss_limit_pct:.0f}%) before it "
                    f"gets there.")
        findings.append(Finding(
            "warning", "risk.max_open_trades",
            f"Up to {max_open} positions at once, ${at_work:,.0f} deployed "
            f"({total_pct:.0f}% of the account). If all {max_open} hit the "
            f"{backstop_pct:.0f}% backstop together — and correlated names "
            f"do — that "
            f"is -${worst:,.0f}, {worst / capital * 100:.0f}% of the "
            f"account.{note}",
            "Lower risk.max_total_deployed_pct to cap the total, or "
            "risk.max_open_trades to hold fewer at once. Neither is wrong; "
            "this is here so the number is a choice rather than a surprise."))

    # 8. Can every enabled strategy actually be reached?
    #
    # The desk hunts until the last enabled strategy shuts, but never past the
    # square-off — a trade opened at 15:44 is forced out a minute later. A
    # window that runs past it is quietly clipped, and a strategy that is
    # enabled, in window by its own reckoning and never once asked is the
    # hardest kind of dead config to notice.
    force_exit = str(cfg.get("session.force_exit_at", "15:45"))
    for key, block in (cfg.get("strategies", {}) or {}).items():
        if not isinstance(block, dict) or not block.get("enabled", True):
            continue
        closes = str(block.get("to", "") or "")
        if closes and closes > force_exit:
            findings.append(Finding(
                "warning", f"strategies.{key}.to",
                f"This strategy runs to {closes}, past the {force_exit} "
                f"square-off. The desk stops hunting at {force_exit}, so the "
                f"last {closes[:5]}-{force_exit} of the window never trades.",
                f"Set strategies.{key}.to to {force_exit} or earlier, or move "
                f"session.force_exit_at later if the window is what you meant."))

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
