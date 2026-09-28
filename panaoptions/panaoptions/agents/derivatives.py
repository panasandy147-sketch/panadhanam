"""Agent 2 — Derivatives & Flow: is there a good contract, and does the
options market lean the same way?

Reads the chain the contract picker already fetched, so it costs no extra
request:

  contract     the picker's choice — nearest expiry first (0DTE/1DTE where
               the name lists them), 0.35-0.50 delta on the same-day profile.
               No contract is a hard veto.
  put/call     put volume over call volume across the chain. Heavy puts under
               a call (or heavy calls under a put) cost points.
  IV           today's at-the-money IV against this symbol's own history. A
               buyer paying the top fifth of its range is paying for a move
               the market already expects.
  flow         unusual volume against open interest (engine/flow.py) agreeing
               or opposing.
"""
from __future__ import annotations

from typing import Any

from panaoptions.agents import consensus
from panaoptions.agents.base import AgentVote, ask, blend, clamp
from panaoptions.alpha import AlphaSignal
from panaoptions.engine import flow as flow_mod
from panaoptions.ledger import store
from panaoptions.models import ContractSearch, OptionContract, OptionRight

NAME = "Derivatives"


def put_call_ratio(chain: list[OptionContract]) -> float | None:
    calls = sum(int(c.volume or 0) for c in chain if c.right is OptionRight.CALL)
    puts = sum(int(c.volume or 0) for c in chain if c.right is OptionRight.PUT)
    if calls <= 0:
        return None if puts <= 0 else 99.0
    return round(puts / calls, 2)


def atm_iv(chain: list[OptionContract], spot: float) -> float:
    """The implied volatility nearest the money on the nearest expiry."""
    priced = [c for c in chain if c.implied_volatility > 0]
    if not priced or spot <= 0:
        return 0.0
    nearest = min(c.dte for c in priced)
    front = [c for c in priced if c.dte == nearest]
    return float(min(front, key=lambda c: abs(c.strike - spot)).implied_volatility)


async def vote(signal: AlphaSignal, chain: list[OptionContract],
               search: ContractSearch, cfg: Any, spot: float,
               today: str, flow: consensus.FlowRead | None = None) -> AgentVote:
    """Score the options side of the trade.

    Extreme flow (>= 10x OI) against the trade is a strict veto — the
    Derivatives Flow Conflict Veto — whatever else the chain says.
    """
    flow = flow if flow is not None else consensus.read(chain, cfg)
    result = AgentVote(agent=NAME, score=0.60)
    conflict = consensus.conflict_veto(signal, flow)
    if conflict:
        result.veto = True
        result.score = 0.0
        result.reasons.append(conflict)
        result.data = {"extreme_flow": flow.line()}
        return result
    chosen = search.chosen
    if chosen is None:
        result.veto = True
        result.score = 0.0
        result.reasons.append(search.note or "no contract qualified")
        return result

    result.reasons.append(
        f"{chosen.label}: {abs(chosen.delta):.2f} delta, {chosen.dte} DTE, "
        f"spread {chosen.spread_pct_of_mid:.1f}%")
    if chosen.dte <= int(cfg.get("agents.derivatives.preferred_max_dte", 1)):
        result.score += 0.05
        result.reasons.append(f"{chosen.dte}DTE — same-day/next-day contract")

    want = 1 if signal.long else -1
    pcr = put_call_ratio(chain)
    against = float(cfg.get("agents.derivatives.max_pcr_against", 1.5))
    if pcr is not None:
        leaning = -1 if pcr >= against else 1 if pcr <= 1 / against else 0
        if leaning == -want:
            result.score -= 0.10
            result.reasons.append(f"put/call {pcr:.2f} leans against the trade")
        elif leaning == want:
            result.score += 0.05
            result.reasons.append(f"put/call {pcr:.2f} agrees")
        else:
            result.reasons.append(f"put/call {pcr:.2f} neutral")

    iv = atm_iv(chain, spot)
    iv_pct = None
    if iv > 0:
        iv_pct = store.iv_percentile(signal.symbol, iv, before=today)
        store.record_iv(signal.symbol, today, iv)
    ceiling = float(cfg.get("agents.derivatives.iv_percentile_ceiling", 80))
    if iv_pct is not None:
        if iv_pct > ceiling:
            result.score -= 0.15
            result.reasons.append(f"IV at the {iv_pct:.0f}th percentile — premium is dear")
        elif iv_pct < 30:
            result.score += 0.05
            result.reasons.append(f"IV at the {iv_pct:.0f}th percentile — premium is cheap")
        else:
            result.reasons.append(f"IV at the {iv_pct:.0f}th percentile")

    if flow.extreme and flow.bias == (1 if signal.long else -1):
        result.score += 0.10
        result.reasons.append(f"extreme flow behind the trade: {flow.line()}")
    seen = flow_mod.scan(chain, cfg)
    if seen.found:
        if seen.bias == want:
            result.score += 0.15
            result.reasons.append(f"flow agrees: {seen.headline()}")
        elif seen.bias == -want:
            result.score -= 0.20
            result.reasons.append(f"flow OPPOSES: {seen.headline()}")

    result.score = round(clamp(result.score), 3)
    result.data = {"pcr": pcr, "atm_iv": round(iv, 4), "iv_percentile": iv_pct,
                   "flow": seen.headline() if seen.found else "",
                   "contract": chosen.label, "delta": abs(chosen.delta),
                   "dte": chosen.dte, "extreme_flow": flow.line()}
    opinion = await ask(cfg, NAME,
                        "Judge whether the option chain supports buying this "
                        "contract: positioning, implied volatility, flow.",
                        {"signal": signal.to_dict(), **result.data,
                         "fallback": search.budget_fallback})
    return blend(result, opinion, cfg)
