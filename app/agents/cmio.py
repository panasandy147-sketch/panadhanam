"""Chief Market Intelligence Officer — synthesis and conflict resolution.

The CMIO does NOT get to size trades or set stops; it nominates a direction and
names the confirmations. The Risk Manager has the final word. That separation is
the whole point of the hierarchy: the optimistic agent and the agent holding the
purse strings are different agents.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import BaseAgent, get_llm_client
from app.core.logging import get_logger
from app.core.models import AgentReport, Bias, MarketContext
from app.core.registry import register_agent

log = get_logger("agent.cmio")


class CMIODecision(BaseModel):
    bias: str = Field(description="BULLISH, BEARISH or NEUTRAL")
    composite_score: float = Field(description="-1.0 to +1.0 overall conviction")
    confirmations: list[str] = Field(
        default_factory=list,
        description="Independent confirming factors, one short phrase each")
    rationale: str = Field(description="3-4 sentences explaining the synthesis")
    counter_argument: str = Field(
        description="The single strongest argument AGAINST this call")
    conflicts_found: list[str] = Field(default_factory=list)
    proceed: bool = Field(description="True only if this is worth sending to the Risk Desk")


@register_agent("cmio")
class CMIOAgent(BaseAgent):
    agent_id = "cmio"

    def analyse_rules(self, ctx: MarketContext) -> AgentReport:  # pragma: no cover
        raise NotImplementedError("CMIO uses synthesise(), not the analyst path")

    # ------------------------------------------------------------------ #
    async def synthesise(self, ctx: MarketContext,
                         reports: list[AgentReport]) -> dict[str, Any]:
        baseline = self._weighted_vote(ctx, reports)

        if self.cfg.llm_enabled:
            try:
                enhanced = await self._llm_synthesise(ctx, reports, baseline)
                if enhanced:
                    return enhanced
            except Exception as exc:
                log.warning("CMIO LLM synthesis failed (%s) — using weighted vote", exc)
        return baseline

    # ------------------------------------------------------------------ #
    # Deterministic synthesis: a confidence-weighted vote with explicit vetoes
    # ------------------------------------------------------------------ #
    def _weighted_vote(self, ctx: MarketContext,
                       reports: list[AgentReport]) -> dict[str, Any]:
        weights = self.cfg.get("weights", {}) or {}
        consensus = self.cfg.get("consensus", {}) or {}

        active = [r for r in reports if r.data_available]
        abstained = [r.agent_id for r in reports if not r.data_available]

        if not active:
            return self._decision(Bias.NEUTRAL, 0.0, [], [],
                                  "No analyst had usable data this cycle.", "", False,
                                  abstained)

        numerator = 0.0
        denominator = 0.0
        confirmations: list[str] = []
        conflicts: list[str] = []

        for r in active:
            if r.agent_id == "fundamental":
                continue    # a veto gate, not a vote
            w = float(weights.get(r.agent_id, 1.0)) * max(r.confidence, 0.05)
            numerator += r.score * w
            denominator += w
            if abs(r.score) >= 0.25:
                confirmations.append(f"{r.agent_id}: {r.bias.value.lower()} ({r.score:+.2f})")

        composite = numerator / denominator if denominator else 0.0
        composite = max(-1.0, min(1.0, composite))

        # --- conflict detection ---
        directional = [r for r in active if abs(r.score) >= 0.25 and r.agent_id != "fundamental"]
        bulls = [r for r in directional if r.score > 0]
        bears = [r for r in directional if r.score < 0]
        if bulls and bears:
            conflicts.append(
                f"{len(bulls)} bullish ({', '.join(r.agent_id for r in bulls)}) vs "
                f"{len(bears)} bearish ({', '.join(r.agent_id for r in bears)})")
            composite = self._resolve_conflict(composite, bulls, bears, consensus, conflicts)

        # --- news veto ---
        if consensus.get("veto_on_high_impact_news", True):
            news = next((r for r in active if r.agent_id == "news_sentiment"), None)
            if news and news.extra.get("has_high_impact"):
                threshold = float(self.cfg.get("news.high_impact_threshold", 0.7))
                if composite > 0 and news.score <= -threshold:
                    conflicts.append("High-impact bearish news vetoes a long")
                    composite = 0.0
                elif composite < 0 and news.score >= threshold:
                    conflicts.append("High-impact bullish news vetoes a short")
                    composite = 0.0

        # --- fundamental veto ---
        fundamental = next((r for r in reports if r.agent_id == "fundamental"), None)
        if fundamental and fundamental.data_available and not fundamental.extra.get("eligible", True):
            conflicts.append(f"Fundamental filter blocks {ctx.symbol}: "
                             f"{', '.join(fundamental.extra.get('fails', []))}")
            composite = 0.0

        # --- macro alignment (optional, off by default) ---
        if consensus.get("require_macro_alignment", False):
            macro = next((r for r in active if r.agent_id == "macro_flow"), None)
            if macro and composite * macro.score < 0 and abs(macro.score) > 0.3:
                conflicts.append("Macro backdrop opposes the technical call")
                composite *= 0.4

        # --- gates ---
        min_conf = int(consensus.get("min_confirmations", 2))
        min_score = float(consensus.get("min_composite_score", 0.35))
        bias = self._bias_from_score(composite, min_score)

        proceed = (bias != Bias.NEUTRAL
                   and len(confirmations) >= min_conf
                   and abs(composite) >= min_score)

        reasons = []
        if bias == Bias.NEUTRAL:
            reasons.append(f"composite {composite:+.2f} inside the neutral band (±{min_score})")
        if len(confirmations) < min_conf:
            reasons.append(f"only {len(confirmations)} confirmations, need {min_conf}")

        rationale = (
            f"Weighted vote of {len(active)} analysts → {composite:+.3f}. "
            + (f"Confirmations: {'; '.join(confirmations)}. " if confirmations else "")
            + (f"Conflicts: {'; '.join(conflicts)}. " if conflicts else "")
            + (f"Not proceeding: {', '.join(reasons)}." if not proceed else
               f"Proceeding to the Risk Desk as {bias.value}.")
        )
        counter = self._counter_argument(bias, active, conflicts)

        return self._decision(bias, composite, confirmations, conflicts,
                              rationale, counter, proceed, abstained)

    def _resolve_conflict(self, composite: float, bulls: list[AgentReport],
                          bears: list[AgentReport], consensus: dict,
                          conflicts: list[str]) -> float:
        policy = consensus.get("conflict_policy", "flat")
        tech_ids = {"candlestick", "derivatives"}

        if policy == "flat":
            conflicts.append("Conflict policy 'flat' — conviction damped toward neutral")
            return composite * 0.45
        if policy == "side_with_technicals":
            tech = [r for r in bulls + bears if r.agent_id in tech_ids]
            if tech:
                avg = sum(r.score * r.confidence for r in tech) / max(
                    sum(r.confidence for r in tech), 1e-9)
                conflicts.append("Sided with technical analysts per conflict policy")
                return max(-1.0, min(1.0, avg))
        if policy == "side_with_macro":
            macro = [r for r in bulls + bears if r.agent_id in {"macro_flow", "news_sentiment"}]
            if macro:
                avg = sum(r.score * r.confidence for r in macro) / max(
                    sum(r.confidence for r in macro), 1e-9)
                conflicts.append("Sided with macro/news per conflict policy")
                return max(-1.0, min(1.0, avg))
        return composite * 0.45

    @staticmethod
    def _counter_argument(bias: Bias, reports: list[AgentReport],
                          conflicts: list[str]) -> str:
        if conflicts:
            return conflicts[0]
        opposing = [r for r in reports
                    if (bias == Bias.BULLISH and r.score < -0.1)
                    or (bias == Bias.BEARISH and r.score > 0.1)]
        if opposing:
            worst = min(opposing, key=lambda r: r.score * (1 if bias == Bias.BULLISH else -1))
            return f"{worst.agent_id} disagrees ({worst.score:+.2f}): {worst.rationale[:160]}"
        weakest = min(reports, key=lambda r: r.confidence, default=None)
        if weakest:
            return (f"No direct opposition, but {weakest.agent_id} has only "
                    f"{weakest.confidence:.0%} confidence — the consensus may be thin.")
        return "No explicit counter-argument identified — treat that as a warning, not comfort."

    @staticmethod
    def _decision(bias: Bias, score: float, confirmations: list[str],
                  conflicts: list[str], rationale: str, counter: str,
                  proceed: bool, abstained: list[str] | None = None) -> dict[str, Any]:
        return {
            "bias": bias,
            "composite_score": round(score, 3),
            "confirmations": confirmations,
            "conflicts": conflicts,
            "rationale": rationale,
            "counter_argument": counter,
            "proceed": proceed,
            "abstained": abstained or [],
        }

    # ------------------------------------------------------------------ #
    async def _llm_synthesise(self, ctx: MarketContext, reports: list[AgentReport],
                              baseline: dict[str, Any]) -> dict[str, Any] | None:
        client = await get_llm_client(self.cfg)
        if client is None:
            return None

        digest = []
        for r in reports:
            digest.append({
                "analyst": r.agent_id,
                "available": r.data_available,
                "bias": r.bias.value,
                "score": r.score,
                "confidence": r.confidence,
                "rationale": r.rationale[:400],
                "evidence": [f"{e.label}: {e.value}" for e in r.evidence[:5]],
                "invalidation": r.invalidation_level,
            })

        consensus = self.cfg.get("consensus", {}) or {}
        prompt = (
            f"SYMBOL: {ctx.symbol}\n"
            f"SPOT: {ctx.quote.last_price if ctx.quote else 'n/a'} "
            f"({ctx.quote.change_pct if ctx.quote else 0:+.2f}% today)\n"
            f"REGIME: {ctx.regime.value if ctx.regime else 'unknown'}\n\n"
            f"ANALYST REPORTS:\n{self._compact(digest, 6000)}\n\n"
            f"DESK POLICY:\n"
            f"- minimum {consensus.get('min_confirmations', 2)} INDEPENDENT confirmations\n"
            f"- minimum composite score {consensus.get('min_composite_score', 0.35)}\n"
            f"- conflict policy: {consensus.get('conflict_policy', 'flat')}\n"
            f"- an analyst with available=false ABSTAINED. Abstention is not agreement.\n\n"
            f"MY WEIGHTED VOTE: {baseline['composite_score']:+.3f} "
            f"({baseline['bias'].value}), proceed={baseline['proceed']}\n"
            f"Confirmations counted: {baseline['confirmations']}\n"
            f"Conflicts: {baseline['conflicts']}\n\n"
            f"Synthesise the desk's view. Two confirmations must come from genuinely "
            f"different evidence — 'EMA stack bullish' and 'price above VWAP' are the "
            f"same observation twice, not two confirmations. Set proceed=false if this "
            f"is not a clean setup; a flat day is a perfectly good outcome."
            f"{self._recall_block(ctx)}"
        )

        response = await client.messages.parse(
            model=self.cfg.llm_model,
            max_tokens=self.spec.get("max_tokens", 4000),
            system=self.system_prompt(),
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.llm_effort},
            messages=[{"role": "user", "content": prompt}],
            output_format=CMIODecision,
        )
        d = response.parsed_output
        if d is None:
            return None

        bias_str = d.bias.strip().upper()
        bias = Bias(bias_str) if bias_str in {b.value for b in Bias} else Bias.NEUTRAL

        # The model may not lower the bar the desk set — policy gates are re-applied.
        min_conf = int(consensus.get("min_confirmations", 2))
        min_score = float(consensus.get("min_composite_score", 0.35))
        proceed = (d.proceed and bias != Bias.NEUTRAL
                   and len(d.confirmations) >= min_conf
                   and abs(d.composite_score) >= min_score)

        return {
            "bias": bias,
            "composite_score": round(max(-1.0, min(1.0, d.composite_score)), 3),
            "confirmations": d.confirmations,
            "conflicts": d.conflicts_found,
            "rationale": d.rationale,
            "counter_argument": d.counter_argument,
            "proceed": proceed,
            "abstained": baseline["abstained"],
            "used_llm": True,
        }
