"""What every agent returns, and how an agent asks Ollama for its view.

Each agent works in two layers:

  rules   a deterministic score from the data in front of it, plus any hard
          veto (RVOL below 1.5x, no contract, a high-impact catalyst against
          the trade). Always computed, and always enough on its own.
  model   when Ollama is running, the same evidence goes to the local model,
          which answers with a validated {score, veto, reason}. Its score is
          blended in at `agents.llm_weight`; it can never lift a rules veto.

If Ollama is down, slow or answers nonsense, the rules score stands and the
vote says so. A desk that stops trading because a language model is not
running would be worse than one without the model at all.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from panaoptions.logging import get_logger

log = get_logger("agents")


class Opinion(BaseModel):
    """The only shape an agent accepts back from the model."""
    score: float = Field(ge=0.0, le=1.0)
    veto: bool = False
    reason: str = Field(default="", max_length=400)


@dataclass
class AgentVote:
    """One agent's verdict on one signal."""
    agent: str
    score: float
    veto: bool = False
    reasons: list[str] = field(default_factory=list)
    # The model's one-line view when it answered, "" when the vote is rules only.
    model_view: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        head = f"{self.agent} {self.score:.2f}" + (" VETO" if self.veto else "")
        why = "; ".join(self.reasons[:3])
        return f"{head} — {why}" if why else head

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "score": round(self.score, 3),
                "veto": self.veto, "reasons": list(self.reasons),
                "model_view": self.model_view}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def llm_enabled(cfg: Any) -> bool:
    """Whether agents may consult Ollama. PANAOPTIONS_AGENT_LLM=off wins."""
    if os.getenv("PANAOPTIONS_AGENT_LLM", "").strip().lower() in {"0", "off", "false", "no"}:
        return False
    return bool(cfg.get("agents.use_llm", True))


async def ask(cfg: Any, agent: str, role: str,
              evidence: dict[str, Any]) -> Opinion | None:
    """The model's opinion on `evidence`, or None on any failure or timeout."""
    if not llm_enabled(cfg):
        return None
    from panaoptions.ml.llm import structured_complete

    system = (
        f"You are the {agent} on an options paper-trading committee. {role} "
        "Answer ONLY with JSON: {\"score\": 0.0-1.0, \"veto\": true|false, "
        "\"reason\": \"one sentence citing the evidence\"}. 0.5 is neutral. "
        "Judge only the evidence given; do not invent news or prices.")
    prompt = json.dumps(evidence, default=str, indent=1)
    timeout = float(cfg.get("agents.llm_timeout_seconds", 20))
    try:
        return await asyncio.wait_for(
            structured_complete(system=system, prompt=prompt, schema=Opinion,
                                cfg=cfg, max_tokens=200),
            timeout=timeout)
    except TimeoutError:
        log.info("%s: Ollama took over %.0fs — rules score stands", agent, timeout)
        return None
    except Exception as exc:                          # noqa: BLE001 - never fatal
        log.debug("%s: Ollama opinion failed: %s", agent, exc)
        return None


def blend(vote: AgentVote, opinion: Opinion | None, cfg: Any,
          veto_allowed: bool = False) -> AgentVote:
    """Fold the model's opinion into a rules vote.

    The model moves the score by at most `agents.llm_weight` of the gap; it
    never clears a rules veto, and only agents given `veto_allowed` may add
    one (the Macro agent reading a headline the keywords missed).
    """
    if opinion is None:
        return vote
    weight = clamp(float(cfg.get("agents.llm_weight", 0.4)))
    vote.score = round(clamp((1 - weight) * vote.score + weight * opinion.score), 3)
    vote.model_view = opinion.reason.strip()[:400]
    if veto_allowed and opinion.veto and not vote.veto:
        vote.veto = True
        vote.reasons.insert(0, f"model veto: {vote.model_view or 'no reason given'}")
    return vote
