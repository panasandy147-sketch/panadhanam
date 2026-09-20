"""Macro, Geopolitical & FII/DII Flow Analyst — the session backdrop."""
from __future__ import annotations

from app.agents.base import BaseAgent
from app.core.models import AgentReport, Evidence, MarketContext
from app.core.registry import register_agent
from app.data.macro import macro_bias_score


@register_agent("macro_flow")
class MacroFlowAgent(BaseAgent):
    agent_id = "macro_flow"

    def analyse_rules(self, ctx: MarketContext) -> AgentReport:
        snap = ctx.macro
        if not snap or not snap.values:
            return AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                               data_available=False,
                               rationale="Macro feed unavailable this cycle.")

        score, reasons = macro_bias_score(snap, self.cfg)
        evidence = [Evidence(label="Macro", value=note, weight=0.15) for note in snap.notes[:6]]

        if snap.fii_cash is not None:
            direction = "buying" if snap.fii_cash > 0 else "selling"
            evidence.append(Evidence(
                label="FII flow", value=f"FII cash {snap.fii_cash:+.0f} Cr — {direction}", weight=0.2))
            score += 0.15 if snap.fii_cash > 0 else -0.15
        if snap.dii_cash is not None:
            evidence.append(Evidence(
                label="DII flow", value=f"DII cash {snap.dii_cash:+.0f} Cr", weight=0.1))

        vix = snap.india_vix
        vix_capped = False
        if vix and vix >= float(self.cfg.get("macro.vix_panic_level", 20.0)):
            score = min(score, 0.25)   # cap long conviction when fear is elevated
            vix_capped = True

        score = max(-1.0, min(1.0, score))
        confidence = min(1.0, 0.25 + 0.08 * len(snap.values) + abs(score) * 0.3)

        rationale = f"Macro backdrop {score:+.2f}. " + " ".join(snap.notes[:3])
        if vix_capped:
            rationale += " Long conviction capped by elevated VIX."

        return AgentReport(
            agent_id=self.agent_id, symbol=ctx.symbol,
            bias=self._bias_from_score(score, 0.2), score=round(score, 3),
            confidence=round(confidence, 2), rationale=rationale, evidence=evidence,
            extra={"values": snap.values, "changes_pct": snap.changes_pct,
                   "india_vix": vix, "reasons": reasons, "vix_capped": vix_capped},
        )

    def llm_payload(self, ctx: MarketContext, baseline: AgentReport) -> str | None:
        snap = ctx.macro
        if not snap or not snap.values:
            return None
        return (
            f"Symbol under review: {ctx.symbol}\n\n"
            f"GLOBAL LEVELS:\n{self._compact(snap.values)}\n"
            f"CHANGES %:\n{self._compact(snap.changes_pct)}\n"
            f"India VIX: {snap.india_vix}\n"
            f"FII cash: {snap.fii_cash}  DII cash: {snap.dii_cash}\n\n"
            f"AUTOMATED READ:\n" + "\n".join(f"- {n}" for n in snap.notes) + "\n\n"
            f"Set the session's risk appetite and say whether the macro backdrop "
            f"supports, opposes or is irrelevant to a trade in {ctx.symbol}.\n"
            f"Remember: flow data is usually a day stale — it is context, not a trigger. "
            f"My rule engine scored {baseline.score:+.3f}."
        )
