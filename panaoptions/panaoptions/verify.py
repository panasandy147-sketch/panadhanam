"""Pre-flight verification: lot multipliers and the previous-day cache.

    python run.py --dry-fire-lots HDFCBANK,INFY          (India; --premium 20)
    python run.py --check-pdh [--market IN|US]

The lot dry-fire pushes a sample contract per symbol through the SAME code
the desk uses — OptionContract.cost, the Risk Gatekeeper's cap and the
RiskManager's sizing — so a lot table that is wrong, or a code path that
forgets the lot, shows up as a wrong total before a real trade does.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from panaoptions.models import Direction, Indicators, OptionContract, OptionRight, Setup, SetupType


def lot_dry_fire(cfg: Any, symbols: list[str], premium: float | dict[str, float] = 20.0
                 ) -> list[dict[str, Any]]:
    """For each symbol: lot, premium x lot, the cap, and what sizing buys."""
    from panaoptions.risk.gatekeeper import RiskGatekeeper
    from panaoptions.risk.guardrails import RiskManager

    risk = RiskManager(cfg)
    gate = RiskGatekeeper(cfg, risk)
    rows = []
    for sym in symbols:
        sym = sym.upper()
        lot = int(cfg.lot_size(sym))
        mid = float(premium.get(sym, 20.0) if isinstance(premium, dict) else premium)
        contract = OptionContract(symbol=sym, right=OptionRight.CALL, strike=1000.0,
                                  expiry="2026-10-27", dte=5, bid=round(mid * 0.99, 2),
                                  ask=round(mid * 1.01, 2), delta=0.45, multiplier=lot)
        setup = Setup(symbol=sym, ts=datetime.now(), direction=Direction.LONG,
                      strategy=SetupType.PD_LIQUIDITY_SWEEP, pattern="dry fire",
                      indicators=Indicators(close=1000.0, atr=10.0),
                      underlying_support=990.0, underlying_target=1030.0)
        signal, why = risk.size(setup, contract, "DRY-FIRE", datetime.now())
        rows.append({
            "symbol": sym, "lot": lot, "premium": contract.mid,
            "cost_per_lot": contract.cost(cfg.multiplier),
            "expected": round(contract.mid * lot, 2),
            "cap": gate.cap(sym), "cap_pct": gate.cap_pct(sym),
            "fits": contract.cost(cfg.multiplier) <= gate.cap(sym),
            "lots_bought": signal.quantity if signal else 0,
            "deployed": signal.cost(cfg.multiplier) if signal else 0.0,
            "note": "" if signal else why,
        })
    return rows


def lot_table(rows: list[dict[str, Any]], currency: str) -> str:
    lines = [f"  {'Symbol':10s} {'Lot':>5s} {'Premium':>9s} {'Premium x lot':>14s} "
             f"{'Cap':>12s} {'Fits':>5s} {'Lots':>5s} {'Deployed':>12s}  Check"]
    for r in rows:
        ok = abs(r["cost_per_lot"] - r["expected"]) < 0.01
        lines.append(
            f"  {r['symbol']:10s} {r['lot']:>5d} {r['premium']:>9.2f} "
            f"{currency}{r['cost_per_lot']:>13,.2f} {currency}{r['cap']:>11,.2f} "
            f"{'yes' if r['fits'] else 'no':>5s} {r['lots_bought']:>5d} "
            f"{currency}{r['deployed']:>11,.2f}  "
            + ("OK" if ok else f"WRONG — expected {r['expected']:,.2f}")
            + (f" ({r['note'][:60]})" if r["note"] else ""))
    return "\n".join(lines)


def pdh_report(cfg: Any, day: str, open_hhmm: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The cached previous-day map for `day`, and what is wrong with it."""
    from panaoptions import fno

    pics = fno.pictures(day)
    problems = []
    if not pics:
        problems.append(f"nothing cached for {day} yet — the desk caches it from "
                        f"fno.ingest_from ({cfg.get('fno.ingest_from', '09:00')}) once running")
    for p in pics:
        if not (p.get("pdh") and p.get("pdl")):
            problems.append(f"{p['symbol']}: no PDH/PDL — no previous session on the tape")
        at = str(p.get("ingested_at") or "")
        if at and at[11:16] >= open_hhmm:
            problems.append(f"{p['symbol']}: cached at {at[11:16]}, AFTER the "
                            f"{open_hhmm} open")
        if p.get("call_oi_change") is None:
            problems.append(f"{p['symbol']}: open-interest change unknown (no OI in the "
                            f"chain yet, or the first reading)")
    return pics, problems
