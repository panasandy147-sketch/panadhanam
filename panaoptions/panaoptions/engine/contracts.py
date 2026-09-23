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

from panaoptions.models import ContractSearch, Direction, OptionContract, OptionRight


def choose(symbol: str, chain: list[OptionContract], direction: Direction,
           cfg, setup=None) -> ContractSearch:
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
    min_price = float(cfg.get("contracts.min_contract_price", 0.60))
    max_price = float(cfg.get("contracts.max_contract_price", 1.00))
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
        if c.spread_pct_of_mid > max_spread:
            reject(f"spread wider than {max_spread}% of mid")
            continue

        if c.mid < min_price:
            reject(f"cheaper than ${min_price:.2f}")
            price_only_failures.append(c)
            continue
        if c.mid > max_price:
            reject(f"dearer than ${max_price:.2f}")
            price_only_failures.append(c)
            continue
        survivors.append(c)

    if survivors:
        # Cheapest first: on a small account the premium is the binding
        # constraint, and a cheaper contract leaves more room to be wrong.
        search.chosen = min(survivors, key=lambda c: c.mid)
        return search

    if price_only_failures:
        nearest = min(price_only_failures,
                      key=lambda c: abs(c.mid - (min_price + max_price) / 2))
        search.closest_by_price = nearest
        cost = nearest.cost(multiplier)
        search.note = (
            f"Nothing fits the ${min_price * multiplier:.0f}-"
            f"${max_price * multiplier:.0f} budget. The nearest contract that "
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
