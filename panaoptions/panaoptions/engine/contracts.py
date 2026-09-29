"""Pick the contract, and account for everything that was turned away.

The four filters are DTE, delta, spread and price. Three of them are ordinary.
The fourth is not, and it is worth being blunt about:

    delta 0.45-0.60 means at-the-money. One contract is 100 shares. An
    at-the-money option 7-14 days out on SPY, NVDA or AAPL costs roughly
    $350-$650. A $60-$100 budget buys roughly 0.10-0.20 delta.

So on this universe the delta band and the price cap describe an empty set,
and the honest output is nothing at all. What must NOT happen is nothing at
all arriving with no explanation — that looks identical to a quiet market and
wastes a fortnight before anyone notices. Hence `ContractSearch`, which counts
every rejection by cause and keeps the nearest miss on price.
"""
from __future__ import annotations

from panaoptions.engine.liquidity import liquidity_problem, spread_limit
from panaoptions.models import ContractSearch, Direction, OptionContract, OptionRight

# What the desk tries, in order, when the setup's own contract is over the
# per-trade budget (or the per-contract price ceiling):
#   shorter_expiry   the same delta band with less time to expiry
#   debit_spread     buy the 0.40-0.50 delta leg, sell a further out-of-the-
#                    money strike, same expiry: less premium, same direction
#   secondary_delta  a 0.30-0.39 delta contract, only if it is liquid
# Anything left is a hard risk failure and the setup is skipped.
DEFAULT_FALLBACK_ORDER = ("shorter_expiry", "debit_spread", "secondary_delta")


def choose(symbol: str, chain: list[OptionContract], direction: Direction,
           cfg, setup=None, budget: float | None = None,
           now=None) -> ContractSearch:
    """The cheapest qualifying contract, or a full account of why there is none.

    A setup may ask for its own delta band and expiry window: a hammer off
    support and a morning star are not the same bet, and a structural reversal
    needs more time than an opening-range break. Its request wins over the
    global default.
    """
    want = OptionRight.CALL if direction is Direction.LONG else OptionRight.PUT

    min_dte = int(getattr(setup, "min_dte_override", 0)
                  or cfg.get("contracts.min_dte", 7))
    max_dte = int(getattr(setup, "max_dte_override", 0)
                  or cfg.get("contracts.max_dte", 14))
    band = getattr(setup, "delta_band", None)
    min_delta = float(band[0]) if band else float(cfg.get("contracts.min_delta", 0.45))
    max_delta = float(band[1]) if band else float(cfg.get("contracts.max_delta", 0.60))
    max_spread = float(cfg.get("contracts.max_spread_pct_of_mid", 5.0))

    def wide(c) -> bool:
        """Wider than allowed — 7%, or 12% on a 0-4 DTE contract in the
        opening window (engine/liquidity.spread_limit)."""
        return c.effective_spread_pct > spread_limit(cfg, c, now, max_spread)
    min_price = float(cfg.get("contracts.min_contract_price", 0.60))
    max_price = float(cfg.get("contracts.max_contract_price", 1.00))
    # The index ETFs' near-the-money contracts cost more per share than the
    # single-stock ceiling allows; their own ceiling keeps the 25% cap usable.
    from panaoptions.risk.gatekeeper import index_symbols
    index_ceiling = cfg.get("contracts.index_max_contract_price")
    if index_ceiling and symbol.upper() in index_symbols(cfg):
        max_price = max(max_price, float(index_ceiling))
    multiplier = cfg.multiplier

    search = ContractSearch(symbol=symbol)
    survivors: list[OptionContract] = []
    # Tracked separately so we can show the nearest miss on price among
    # contracts that passed everything else. That is the diagnostic that
    # actually explains an empty result.
    price_only_failures: list[OptionContract] = []

    def reject(cause: str) -> None:
        search.rejected[cause] = search.rejected.get(cause, 0) + 1

    for c in chain:
        if c.right is not want:
            continue
        search.examined += 1

        if not (min_dte <= c.dte <= max_dte):
            reject(f"DTE outside {min_dte}-{max_dte}")
            continue
        if c.mid <= 0:
            reject("no two-sided market")
            continue
        if not (min_delta <= abs(c.delta) <= max_delta):
            reject(f"delta outside {min_delta}-{max_delta}")
            continue
        if wide(c):
            reject(f"spread wider than {spread_limit(cfg, c, now, max_spread):g}% of mid")
            continue

        if c.mid < min_price:
            reject(f"cheaper than ${min_price:.2f}")
            price_only_failures.append(c)
            continue
        if c.mid > max_price:
            reject(f"dearer than ${max_price:.2f}")
            price_only_failures.append(c)
            continue
        if budget is not None and c.cost(multiplier) > budget:
            reject(f"over the ${budget:,.0f} budget")
            price_only_failures.append(c)
            continue
        survivors.append(c)

    if not survivors and budget is not None:
        ideal = min(price_only_failures, key=lambda c: c.mid, default=None)
        lead = (f"The {min_delta:.2f}-{max_delta:.2f} delta, {min_dte}-{max_dte} day "
                f"contract ({ideal.label}) costs ${ideal.cost(multiplier):,.0f}, over "
                f"the ${budget:,.0f} budget" if ideal else
                f"Nothing in the {min_delta:.2f}-{max_delta:.2f} delta band fits the "
                f"${budget:,.0f} budget")
        found, tier, how, misses = _fallback(
            symbol, chain, want, min_dte, max_dte, min_delta, max_delta, max_spread,
            min_price, max_price, budget, multiplier, cfg, wide)
        if found is not None:
            search.chosen = found
            search.budget_fallback = True
            search.tier = tier
            search.note = f"{lead} — {how}"
            return search
        search.skipped_because = misses
        if ideal is not None or any(misses):
            search.note = (f"{lead}. SKIPPED — hard risk failure, no fallback "
                           f"fits: " + "; ".join(m for m in misses if m))
            return search

    if survivors:
        # A same-day desk wants the nearest expiry first: 0DTE when the name
        # lists one, otherwise that week's. Among those, cheapest.
        if bool(cfg.get("contracts.prefer_nearest_expiry", False)):
            nearest = min(c.dte for c in survivors)
            survivors = [c for c in survivors if c.dte == nearest]
        # Cheapest first: on a small account the premium is the binding
        # constraint, and a cheaper contract leaves more room to be wrong.
        search.chosen = min(survivors, key=lambda c: c.mid)
        search.tier = "primary"
        return search

    if price_only_failures:
        nearest = min(price_only_failures,
                      key=lambda c: abs(c.mid - (min_price + max_price) / 2))
        search.closest_by_price = nearest
        cost = nearest.cost(multiplier)
        search.note = (
            f"Nothing fits the ${min_price * multiplier:.0f}-"
            f"${max_price * multiplier:.0f} per-contract price range. The nearest contract that "
            f"passed DTE, delta and spread was {nearest.label} at "
            f"${cost:,.0f} ({abs(nearest.delta):.2f} delta). A {min_delta:.2f}-"
            f"{max_delta:.2f} delta option is at the money, and at the money on "
            f"{symbol} costs what it costs — the budget and the delta band "
            f"cannot both hold. Run `--explain-contracts` for the full picture.")
    elif not search.examined:
        search.note = f"No {want.value.lower()} contracts came back for {symbol}."
    else:
        search.note = (f"{search.examined} {want.value.lower()}s examined, none "
                       f"passed. Breakdown: " +
                       ", ".join(f"{v}x {k}" for k, v in
                                 sorted(search.rejected.items(),
                                        key=lambda kv: -kv[1])))
    return search


def _fallback(symbol, chain, want, min_dte, max_dte, min_delta, max_delta,
              max_spread, min_price, max_price, budget, multiplier, cfg, wide=None):
    """Walk the fallback ladder. Returns (contract, tier, how, misses).

    `misses` says, rung by rung, why each found nothing — the text of a
    hard-risk skip.
    """
    floor = float(cfg.get("contracts.budget_fallback_min_delta", 0.30) or 0)
    shortest = min(int(cfg.get("contracts.min_dte", 7)), min_dte)
    order = [str(x) for x in (cfg.get("contracts.fallback_order")
                              or DEFAULT_FALLBACK_ORDER)]
    misses: list[str] = []
    wide = wide or (lambda c: c.effective_spread_pct > max_spread)

    def ok(c) -> bool:
        return (c.right is want and c.mid > 0 and not wide(c)
                and min_price <= c.mid <= max_price
                and c.cost(multiplier) <= budget)

    for rung in order:
        if rung == "shorter_expiry":
            if floor <= 0 or shortest >= min_dte:
                continue
            pool = [c for c in chain if shortest <= c.dte < min_dte
                    and min_delta <= abs(c.delta) <= max_delta and ok(c)]
            if pool:
                c = max(pool, key=lambda c: (c.dte, abs(c.delta)))
                return c, rung, (f"took the same delta with less time: {c.label} "
                                 f"({abs(c.delta):.2f} delta, {c.dte} days) at "
                                 f"${c.cost(multiplier):,.0f}."), misses
            misses.append(f"same delta with less time: none under "
                          f"${budget:,.0f}")
        elif rung == "debit_spread":
            if not bool(cfg.get("contracts.debit_spread.enabled", True)):
                continue
            spread, why = build_debit_spread(
                chain, want, min_delta, max_delta, shortest, max_dte, max_spread,
                max_price, budget, multiplier, cfg, wide)
            if spread is not None:
                kind = "bull call" if want is OptionRight.CALL else "bear put"
                net = spread.mid
                rr = (spread.width - net) / net if net else 0.0
                naked = spread.long_leg.cost(multiplier)
                return spread, rung, (
                    f"CONVERTED to a {kind} debit spread: buy "
                    f"{spread.long_leg.label} ({abs(spread.long_leg.delta):.2f} delta), "
                    f"sell {spread.short_leg.label} ({abs(spread.short_leg.delta):.2f} "
                    f"delta). Net debit {net:.2f} = ${spread.cost(multiplier):,.0f} a "
                    f"spread (naked ${naked:,.0f}); max profit "
                    f"${(spread.width - net) * (spread.multiplier or multiplier):,.0f}, "
                    f"reward:risk {rr:.2f}."), misses
            misses.append(f"debit spread: {why}")
            # Single-leg grace: the spread failed for want of a liquid, priced
            # SHORT leg — take the long leg outright when its whole lot premium
            # (premium x lot) fits the budget, the per-share ceiling aside.
            grace = cfg.get("contracts.single_leg_grace") or {}
            if grace.get("enabled", True) and "short" in why:
                cap = budget * float(grace.get("max_budget_pct", 100.0)) / 100.0
                wide_ok = wide or (lambda c: c.effective_spread_pct > max_spread)
                longs = [c for c in chain if c.right is want and c.mid > 0
                         and bool(c.bid and c.ask) and not wide_ok(c)
                         and not liquidity_problem(c, cfg)
                         and shortest <= c.dte <= max_dte
                         and min_delta <= abs(c.delta) <= max_delta
                         and c.cost(multiplier) <= cap]
                if longs:
                    centre = (min_delta + max_delta) / 2
                    c = min(longs, key=lambda c: (c.dte, abs(abs(c.delta) - centre)))
                    lot = c.multiplier or multiplier
                    return c, "single_leg_grace", (
                        f"no liquid short leg for a spread — SINGLE-LEG GRACE: bought "
                        f"{c.label} outright ({abs(c.delta):.2f} delta), premium "
                        f"{c.mid:.2f} x lot {lot} = {c.cost(multiplier):,.0f}, within "
                        f"the {cap:,.0f} budget."), misses
                misses.append("single-leg grace: the long leg's lot premium does not "
                              "fit the budget either")
        elif rung == "secondary_delta":
            if floor <= 0:
                continue
            thin = 0
            for where, best in (
                    (lambda c: min_dte <= c.dte <= max_dte and floor <= abs(c.delta) < min_delta,
                     lambda c: (abs(c.delta), c.dte)),
                    (lambda c: shortest <= c.dte < min_dte and floor <= abs(c.delta) < min_delta,
                     lambda c: (abs(c.delta), c.dte))):
                pool = [c for c in chain if where(c) and ok(c)]
                liquid = [c for c in pool if not liquidity_problem(c, cfg)]
                thin += len(pool) - len(liquid)
                if liquid:
                    c = max(liquid, key=best)
                    return c, rung, (
                        f"took the secondary tier ({floor:.2f}-{min_delta - 0.01:.2f} "
                        f"delta, liquid): {c.label} ({abs(c.delta):.2f} delta, "
                        f"{c.dte} days, OI {c.open_interest}) at "
                        f"${c.cost(multiplier):,.0f}."), misses
            misses.append(f"{floor:.2f}-{min_delta - 0.01:.2f} delta tier: "
                          + (f"{thin} fit the budget but failed liquidity"
                             if thin else _diagnose(chain, want, shortest, max_dte,
                                                    floor, min_delta, max_spread,
                                                    budget, multiplier)))
    return None, "", "", misses


def _diagnose(chain, want, shortest, max_dte, floor, below, max_spread, budget,
              multiplier) -> str:
    candidates = [c for c in chain if c.right is want and shortest <= c.dte <= max_dte
                  and floor <= abs(c.delta) < below]
    if not candidates:
        return (f"no {want.value.lower()} at {floor:.2f}-{below - 0.01:.2f} delta "
                f"between {shortest} and {max_dte} days came back")
    causes: dict[str, int] = {}
    for c in candidates:
        cause = ("no two-sided market" if c.mid <= 0 else
                 f"spread over {max_spread:g}%" if c.effective_spread_pct > max_spread else
                 f"over the ${budget:,.0f} budget" if c.cost(multiplier) > budget else
                 "outside the price range")
        causes[cause] = causes.get(cause, 0) + 1
    priced = [c for c in candidates if c.mid > 0]
    cheapest = min(priced, key=lambda c: c.mid, default=None)
    return (f"of {len(candidates)}, "
            + ", ".join(f"{n} {k}" for k, n in sorted(causes.items(), key=lambda kv: -kv[1]))
            + (f" (cheapest {cheapest.label} at ${cheapest.cost(multiplier):,.0f})"
               if cheapest else ""))


def make_spread(long_leg: OptionContract, short_leg: OptionContract) -> OptionContract:
    """One position: buy `long_leg`, sell `short_leg`. Priced at the net."""
    return OptionContract(
        symbol=long_leg.symbol, right=long_leg.right, strike=long_leg.strike,
        expiry=long_leg.expiry, dte=long_leg.dte,
        bid=round(max(long_leg.bid - short_leg.ask, 0.0), 4),
        ask=round(max(long_leg.ask - short_leg.bid, 0.0), 4),
        delta=long_leg.delta, implied_volatility=long_leg.implied_volatility,
        open_interest=min(long_leg.open_interest, short_leg.open_interest),
        volume=min(long_leg.volume, short_leg.volume),
        multiplier=long_leg.multiplier,
        estimated=long_leg.estimated or short_leg.estimated,
        long_leg=long_leg, short_leg=short_leg)


def build_debit_spread(chain, want, min_delta, max_delta, min_dte, max_dte,
                       max_spread, max_price, budget, multiplier, cfg, wide=None):
    """The debit spread that keeps the setup's delta and fits the budget.

    Long leg: the setup's delta band (0.40-0.50), liquid, spread within the
    limit. Short leg: same expiry, further out of the money (a higher strike
    for calls, lower for puts), liquid. Of the spreads whose net debit fits
    the budget and the per-share ceiling, with reward:risk of at least
    `contracts.debit_spread.min_reward_risk`: nearest expiry (when the desk
    prefers it), long delta nearest the middle of the band, then the widest —
    the most upside the budget allows.

    Returns (spread or None, why not).
    """
    min_rr = float(cfg.get("contracts.debit_spread.min_reward_risk", 0.8))
    min_net = float(cfg.get("contracts.debit_spread.min_net_debit", 0.10))
    centre = (min_delta + max_delta) / 2

    wide = wide or (lambda c: c.effective_spread_pct > max_spread)

    def usable(c) -> bool:
        return (c.right is want and c.mid > 0 and bool(c.bid and c.ask)
                and not wide(c) and not liquidity_problem(c, cfg))

    longs = [c for c in chain if usable(c) and min_dte <= c.dte <= max_dte
             and min_delta <= abs(c.delta) <= max_delta]
    if not longs:
        return None, (f"no liquid {want.value.lower()} at {min_delta:.2f}-"
                      f"{max_delta:.2f} delta with a spread under {max_spread:g}%")
    above = want is OptionRight.CALL
    fits: list[OptionContract] = []
    over = poor = 0
    for leg in longs:
        for short in chain:
            if (short.expiry != leg.expiry or not usable(short)
                    or not (short.strike > leg.strike if above else short.strike < leg.strike)):
                continue
            spread = make_spread(leg, short)
            net = spread.mid
            if net < min_net:
                continue
            if spread.cost(multiplier) > budget or net > max_price:
                over += 1
                continue
            if (spread.width - net) / net < min_rr:
                poor += 1
                continue
            fits.append(spread)
    if not fits:
        return None, (f"{len(longs)} long leg(s) in band; "
                      + (f"{over} spread(s) still over ${budget:,.0f}" if over else
                         "no liquid, priced short strike further out")
                      + (f", {poor} under {min_rr:g} reward:risk" if poor else ""))
    if bool(cfg.get("contracts.prefer_nearest_expiry", False)):
        nearest = min(s.dte for s in fits)
        fits = [s for s in fits if s.dte == nearest]
    best = min(fits, key=lambda s: (s.dte, round(abs(abs(s.delta) - centre), 3), -s.width))
    return best, ""


def affordable_delta(chain: list[OptionContract], direction: Direction,
                     cfg) -> tuple[float, float] | None:
    """What delta the budget ACTUALLY buys, for the diagnostic report.

    Answering "so what can I afford?" with a number beats telling someone
    their configuration is wrong and leaving them to guess the fix.
    """
    want = OptionRight.CALL if direction is Direction.LONG else OptionRight.PUT
    min_dte = int(cfg.get("contracts.min_dte", 7))
    max_dte = int(cfg.get("contracts.max_dte", 14))
    min_price = float(cfg.get("contracts.min_contract_price", 0.60))
    max_price = float(cfg.get("contracts.max_contract_price", 1.00))

    deltas = [abs(c.delta) for c in chain
              if c.right is want and min_dte <= c.dte <= max_dte
              and min_price <= c.mid <= max_price and c.delta]
    return (min(deltas), max(deltas)) if deltas else None
