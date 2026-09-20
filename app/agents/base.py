"""Base agent: Claude-backed reasoning with a deterministic fallback.

Two design decisions worth knowing:

1. **Every agent works without an API key.** Each subclass implements
   `analyse_rules()` (pure Python) and optionally `llm_payload()`. With no key,
   or if the API call fails, the rule engine's verdict is used. The desk never
   goes dark because a model was unreachable.

2. **The LLM returns a validated schema, not prose.** We use
   `client.messages.parse(output_format=...)`, so a malformed response is a
   caught exception, not a mis-parsed trade.
"""
from __future__ import annotations

import abc
import asyncio
import json
import time
from typing import Any

from pydantic import BaseModel, Field

from app.core.bus import Topic, bus
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import AgentReport, Bias, Evidence, MarketContext

log = get_logger("agent")


class LLMVerdict(BaseModel):
    """The schema Claude must fill. Narrow on purpose — no free-form trade calls."""
    bias: str = Field(description="BULLISH, BEARISH or NEUTRAL")
    score: float = Field(description="-1.0 (max bearish) to +1.0 (max bullish)")
    confidence: float = Field(description="0.0 to 1.0, how sure you are")
    rationale: str = Field(description="2-3 sentences, specific and quantitative")
    key_evidence: list[str] = Field(default_factory=list,
                                    description="3-5 concrete facts behind the call")
    invalidation_level: float | None = Field(
        default=None, description="Price at which this view is proven wrong, or null")


_llm_client: Any = None
_llm_lock = asyncio.Lock()


async def get_llm_client(cfg: Config) -> Any | None:
    """Lazily build one shared AsyncAnthropic client."""
    global _llm_client
    if not cfg.llm_enabled:
        return None
    if _llm_client is not None:
        return _llm_client
    async with _llm_lock:
        if _llm_client is None:
            try:
                from anthropic import AsyncAnthropic
                _llm_client = AsyncAnthropic(api_key=cfg.anthropic_key)
                log.info("LLM reasoning enabled (%s)", cfg.llm_model)
            except ImportError:
                log.warning("anthropic package not installed — rule-based mode only")
                return None
    return _llm_client


class BaseAgent(abc.ABC):
    """Contract: consume a MarketContext, return an AgentReport."""

    agent_id: str = "base"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self.spec: dict[str, Any] = self.cfg.agent(self.agent_id)
        self.name: str = self.spec.get("name", self.agent_id)

    # ----------------------- subclass hooks -----------------------
    @abc.abstractmethod
    def analyse_rules(self, ctx: MarketContext) -> AgentReport:
        """Deterministic analysis. MUST always return a report, never raise."""

    def llm_payload(self, ctx: MarketContext, baseline: AgentReport) -> str | None:
        """The user message for Claude. Return None to skip the LLM for this cycle."""
        return None

    def system_prompt(self) -> str:
        """Built from config/agents.yaml so prompts are editable without code."""
        parts = [self.spec.get("role", f"You are {self.name}.")]
        if self.spec.get("goal"):
            parts.append(f"\nYour goal: {self.spec['goal']}")
        if self.spec.get("focus"):
            parts.append("\nWhat you look at:")
            parts += [f"- {f}" for f in self.spec["focus"]]
        if self.spec.get("rules"):
            parts.append("\nHard rules:")
            parts += [f"- {r}" for r in self.spec["rules"]]
        parts.append(
            "\nYou are one analyst on a desk; another agent owns position sizing and "
            "stops. Be quantitative, cite the numbers you were given, and never "
            "invent data you were not shown. If the data is too thin to judge, say "
            "so and return NEUTRAL with low confidence — abstaining is respected here."
        )
        return "\n".join(parts)

    # ----------------------- orchestration -----------------------
    async def run(self, ctx: MarketContext) -> AgentReport:
        started = time.perf_counter()
        await bus.publish(Topic.AGENT_START,
                          {"agent": self.agent_id, "name": self.name, "symbol": ctx.symbol})
        try:
            report = self.analyse_rules(ctx)
        except Exception as exc:
            log.exception("%s rule engine failed: %s", self.agent_id, exc)
            report = AgentReport(agent_id=self.agent_id, symbol=ctx.symbol,
                                 data_available=False, rationale=f"rule engine error: {exc}")

        if self.cfg.llm_enabled and report.data_available:
            try:
                enhanced = await self._run_llm(ctx, report)
                if enhanced:
                    report = enhanced
            except Exception as exc:
                log.warning("%s LLM step failed (%s) — keeping rule-based verdict",
                            self.agent_id, exc)

        report.latency_ms = int((time.perf_counter() - started) * 1000)
        await bus.publish(Topic.AGENT_REPORT, report)
        return report

    async def _run_llm(self, ctx: MarketContext, baseline: AgentReport) -> AgentReport | None:
        payload = self.llm_payload(ctx, baseline)
        if not payload:
            return None
        client = await get_llm_client(self.cfg)
        if client is None:
            return None

        response = await client.messages.parse(
            model=self.cfg.llm_model,
            max_tokens=self.spec.get("max_tokens", 2000),
            system=self.system_prompt(),
            thinking={"type": "adaptive"},
            output_config={"effort": self.cfg.llm_effort},
            messages=[{"role": "user", "content": payload}],
            output_format=LLMVerdict,
        )
        verdict = response.parsed_output
        if verdict is None:
            return None

        bias = verdict.bias.strip().upper()
        report = AgentReport(
            agent_id=self.agent_id,
            symbol=ctx.symbol,
            bias=Bias(bias) if bias in {b.value for b in Bias} else Bias.NEUTRAL,
            score=verdict.score,
            confidence=verdict.confidence,
            rationale=verdict.rationale,
            evidence=[Evidence(label="llm", value=e) for e in verdict.key_evidence],
            invalidation_level=verdict.invalidation_level or baseline.invalidation_level,
            suggested_entry=baseline.suggested_entry,
            suggested_target=baseline.suggested_target,
            data_available=True,
            used_llm=True,
            extra={**baseline.extra, "rule_score": baseline.score,
                   "rule_bias": baseline.bias.value},
        )

        # Sharp disagreement between the deterministic prior and the model is a
        # signal in itself — we keep the model's direction but damp confidence.
        if abs(report.score - baseline.score) > 1.0:
            report.confidence *= 0.6
            report.rationale += (
                f" [Note: rule engine scored {baseline.score:+.2f} vs model "
                f"{report.score:+.2f} — confidence reduced on the disagreement.]")
        return report

    # ----------------------- helpers -----------------------
    @staticmethod
    def _bias_from_score(score: float, threshold: float = 0.15) -> Bias:
        if score >= threshold:
            return Bias.BULLISH
        if score <= -threshold:
            return Bias.BEARISH
        return Bias.NEUTRAL

    @staticmethod
    def _compact(data: Any, limit: int = 3500) -> str:
        """JSON for the prompt, truncated so one fat option chain can't blow the budget."""
        text = json.dumps(data, default=str, separators=(",", ":"))
        return text if len(text) <= limit else text[:limit] + "...[truncated]"

    def _recall_block(self, ctx: MarketContext) -> str:
        """Past graded signals — this is how the agent learns from what happened."""
        if not ctx.recall:
            return ""
        lines = ["\nHOW YOUR RECENT CALLS ON THIS SYMBOL ACTUALLY PLAYED OUT:"]
        for item in ctx.recall[:8]:
            lines.append(
                f"- {item.get('ts', '?')[:16]} {item.get('bias', '?')} "
                f"→ {item.get('outcome', 'pending')} "
                f"({item.get('r_multiple', 0):+.2f}R) — {item.get('note', '')[:110]}")
        lines.append("Weigh this history. If a pattern keeps failing in this regime, "
                     "lower your confidence in it.")
        return "\n".join(lines)
