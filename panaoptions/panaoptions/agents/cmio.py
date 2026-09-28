"""Agent 4 — CMIO / Risk Manager: combine the votes, enforce the limits, act.

    committee score = Σ weightᵢ · scoreᵢ / Σ weightᵢ      (agents.weights)
    final score     = committee score × strategy weight   (strategy_weights,
                                                            tuned each Friday)

Approved when no agent vetoed AND the final score reaches
`agents.approve_threshold`. Then — and only then — the Risk Gatekeeper
reviews the contract, and the RiskManager sizes it. The CMIO cannot approve
past the gatekeeper: a perfect committee score on a contract with a 9% spread
is still refused.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from panaoptions.agents import derivatives, macro, technical
from panaoptions.agents.base import AgentVote, clamp
from panaoptions.alpha import AlphaSignal
from panaoptions.logging import get_logger
from panaoptions.models import Candle, ContractSearch, OptionContract, Setup
from panaoptions.risk.gatekeeper import GateDecision, RiskGatekeeper

log = get_logger("agents.cmio")

NAME = "CMIO"
DEFAULT_WEIGHTS = {"technical": 0.35, "derivatives": 0.35, "macro": 0.30}


def strategy_weight(cfg: Any, source: str) -> float:
    """The learned multiplier for a strategy, clamped to 0.25-1.5."""
    try:
        value = float(cfg.get(f"strategy_weights.{source}", 1.0))
    except (TypeError, ValueError):
        value = 1.0
    return clamp(value, 0.25, 1.5)


@dataclass
class Verdict:
    """The committee's decision on one signal."""
    approved: bool
    score: float
    committee: float
    weight: float
    threshold: float
    votes: list[AgentVote] = field(default_factory=list)
    gate: GateDecision | None = None
    reason: str = ""
    # The committee's own call, before the gatekeeper — kept so a cached vote
    # can be re-gated against the account as it is now.
    committee_approved: bool = False

    def summary(self) -> str:
        parts = ", ".join(f"{v.agent.lower()} {v.score:.2f}"
                          + ("✗" if v.veto else "") for v in self.votes)
        verdict = "approved" if self.approved else f"refused — {self.reason}"
        return (f"CMIO {self.score:.2f} (committee {self.committee:.2f} × "
                f"weight {self.weight:.2f}, needs {self.threshold:.2f}): "
                f"{parts} — {verdict}")

    def to_dict(self) -> dict[str, Any]:
        return {"approved": self.approved, "score": self.score,
                "committee": self.committee, "weight": self.weight,
                "threshold": self.threshold, "reason": self.reason,
                "votes": [v.to_dict() for v in self.votes],
                "gate": ({"approved": self.gate.approved,
                          "passed": self.gate.passed,
                          "rejected": self.gate.rejected,
                          "cap": self.gate.cap, "cap_pct": self.gate.cap_pct}
                         if self.gate else None)}


def combine(votes: list[AgentVote], signal: AlphaSignal, cfg: Any) -> Verdict:
    """The weighted committee score, the strategy weight, and the call."""
    weights = {**DEFAULT_WEIGHTS, **(cfg.get("agents.weights") or {})}
    total = sum(float(weights.get(v.agent.lower(), 0.0)) for v in votes) or 1.0
    committee = sum(float(weights.get(v.agent.lower(), 0.0)) * v.score
                    for v in votes) / total
    weight = strategy_weight(cfg, signal.source)
    score = round(clamp(committee * weight), 3)
    threshold = float(cfg.get("agents.approve_threshold", 0.55))
    verdict = Verdict(approved=False, score=score, committee=round(committee, 3),
                      weight=weight, threshold=threshold, votes=votes)
    vetoes = [v for v in votes if v.veto]
    if vetoes:
        verdict.reason = "; ".join(f"{v.agent} veto: {v.reasons[0] if v.reasons else ''}"
                                   for v in vetoes)
    elif score < threshold:
        verdict.reason = f"score {score:.2f} below {threshold:.2f}"
    else:
        verdict.approved = verdict.committee_approved = True
    return verdict


class CMIO:
    """Runs the committee for one signal and hands approved trades to risk."""

    def __init__(self, cfg: Any, gatekeeper: RiskGatekeeper) -> None:
        self.cfg = cfg
        self.gatekeeper = gatekeeper

    async def convene(self, *, signal: AlphaSignal, setup: Setup,
                      candles: list[Candle], chain: list[OptionContract],
                      search: ContractSearch, feed: Any, now: datetime,
                      screen_rvol: float = 0.0,
                      unrealised: float = 0.0) -> Verdict:
        """Ask the three specialists at once, combine, then gate the contract."""
        votes = list(await asyncio.gather(
            technical.vote(signal, setup, candles, self.cfg, screen_rvol),
            derivatives.vote(signal, chain, search, self.cfg,
                             spot=setup.indicators.close,
                             today=now.date().isoformat()),
            macro.vote(signal, feed, self.cfg, now)))
        verdict = combine(votes, signal, self.cfg)
        self.gate(verdict, signal, setup, search, unrealised)
        log.info("%s %s — %s", signal.symbol, signal.direction, verdict.summary())
        return verdict

    def gate(self, verdict: Verdict, signal: AlphaSignal, setup: Setup,
             search: ContractSearch, unrealised: float = 0.0) -> Verdict:
        """Put a committee-approved verdict through the Risk Gatekeeper.

        Always against the account as it is NOW, so a vote cached from an
        earlier cycle cannot carry yesterday's room into today's order.
        """
        if not verdict.committee_approved or search.chosen is None:
            return verdict
        gate = self.gatekeeper.review(signal.to_dict(), search.chosen,
                                      setup.delta_band, unrealised)
        verdict.gate = gate
        verdict.approved = gate.approved
        verdict.reason = "" if gate.approved else f"Risk Gatekeeper: {gate.reason}"
        return verdict
