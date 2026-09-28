"""The four-agent consensus pipeline, and the news veto that overrides it.

    Alpha / Technical     candlestick         the chart: patterns, trend, VWAP
    Derivative / Flow     derivatives         the chain: OI walls, PCR, IV, leg
    Macro / Sentiment     news_sentiment      headlines for this symbol
                          macro_flow          indices, futures, VIX, flows
    CMIO / Risk Manager   cmio + risk         combines the votes (agents/cmio.py),
                                              then sizes and gates (agents/risk.py)

The CMIO's weighted vote is unchanged; this module adds the two things that
sit on top of it:

  news_veto()          an ABSOLUTE veto. When the News & Sentiment agent's
                       polarity is stronger than `consensus.news_veto_polarity`
                       (0.60) and points against the proposed trade — -0.65
                       under a BUY, +0.65 under a SELL — the trade is dead,
                       whatever the chart says. No conflict policy, lead
                       analyst or model opinion can override it.
  effective_weights()  each analyst's vote weight: the configured weight (as
                       the EWMA learner has adjusted it) times the multiplier
                       the Friday Ollama review wrote to
                       config/strategy_weights.json.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger("agent.consensus")

ROOT = Path(__file__).resolve().parents[2]
STRATEGY_WEIGHTS_PATH = ROOT / "config" / "strategy_weights.json"

PIPELINE: dict[str, tuple[str, ...]] = {
    "Alpha / Technical": ("candlestick",),
    "Derivative / Flow": ("derivatives",),
    "Macro / Sentiment": ("news_sentiment", "macro_flow"),
    "CMIO / Risk Manager": ("cmio", "risk"),
}
# The analysts whose weight the Friday review may tune.
VOTING_AGENTS: tuple[str, ...] = ("candlestick", "derivatives", "news_sentiment",
                                  "macro_flow")
MULTIPLIER_MIN, MULTIPLIER_MAX = 0.25, 1.5
NEWS_AGENTS = ("news_sentiment",)


# --------------------------------------------------------------------------- #
def news_veto(reports: list[Any], direction: float, cfg: Any) -> str:
    """The veto reason when news polarity strongly contradicts the trade, else "".

    `direction` is the proposed trade's sign: > 0 BUY / long, < 0 SELL / short.
    Strictly greater than the threshold: exactly 0.60 does not veto.
    """
    if direction == 0 or not bool(cfg.get("consensus.veto_on_high_impact_news", True)):
        return ""
    threshold = float(cfg.get("consensus.news_veto_polarity", 0.60))
    for report in reports or []:
        if getattr(report, "agent_id", "") not in NEWS_AGENTS:
            continue
        if not getattr(report, "data_available", True):
            continue
        polarity = float(getattr(report, "score", 0.0) or 0.0)
        if abs(polarity) > threshold and polarity * direction < 0:
            side = "BUY" if direction > 0 else "SELL"
            return (f"News veto: sentiment polarity {polarity:+.2f} is beyond "
                    f"±{threshold:.2f} and against the {side} — no trade "
                    f"against a high-impact catalyst")
    return ""


# --------------------------------------------------------------------------- #
def load_multipliers(path: Path | None = None) -> dict[str, float]:
    """The Friday review's per-analyst multipliers, clamped; {} when absent.

    A missing, unreadable or malformed file means "no adjustment" — never an
    error that could stop the desk voting.
    """
    path = path or STRATEGY_WEIGHTS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    weights = data.get("strategy_weights") if isinstance(data, dict) else None
    if not isinstance(weights, dict):
        return {}
    out: dict[str, float] = {}
    for name, value in weights.items():
        if name not in VOTING_AGENTS or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out[name] = max(MULTIPLIER_MIN, min(MULTIPLIER_MAX, number))
    return out


def effective_weights(cfg: Any, path: Path | None = None) -> dict[str, float]:
    """Configured (and EWMA-learned) weights x the Friday multipliers."""
    base = dict(cfg.get("weights", {}) or {})
    for name, multiplier in load_multipliers(path).items():
        base[name] = round(float(base.get(name, 1.0)) * multiplier, 4)
    return base


def pipeline_view(reports: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """The reports grouped by the four seats, for display and the journal."""
    by_id = {getattr(r, "agent_id", ""): r for r in reports or []}
    return {seat: [{"agent": a, "score": round(float(by_id[a].score), 3)}
                   for a in agents if a in by_id]
            for seat, agents in PIPELINE.items()}
