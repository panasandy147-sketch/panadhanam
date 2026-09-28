"""The four-agent consensus pipeline, and the news veto that overrides it.

    Alpha / Technical     candlestick         the chart: patterns, trend, VWAP
                          volume_profile      POC, value area, LVN/HVN setups
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
    "Alpha / Technical": ("candlestick", "volume_profile"),
    "Derivative / Flow": ("derivatives",),
    "Macro / Sentiment": ("news_sentiment", "macro_flow"),
    "CMIO / Risk Manager": ("cmio", "risk"),
}
# The analysts whose weight the Friday review may tune.
VOTING_AGENTS: tuple[str, ...] = ("candlestick", "volume_profile", "derivatives",
                                  "news_sentiment", "macro_flow")
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


# --------------------------------------------------------------------------- #
# Volume profile confluence — on every trade, whichever analyst led it
# --------------------------------------------------------------------------- #
def volume_profile_confluence(ctx: Any, reports: list[Any], composite: float,
                              cfg: Any) -> tuple[float, list[str], str]:
    """(adjusted composite, notes, veto reason).

    An entry at a volume-profile level in the trade's favour — long at a VAL
    or POC, short at a VAH or POC — moves the composite
    `volume_profile.alignment_boost` (0.30) toward the trade. A thick High
    Volume Node straight ahead moves it `hvn_penalty` toward zero, or is a
    veto when it is within `hvn_veto_atr` ATR (buying straight into accepted
    inventory). A node that holds the trade's own target is where it is meant
    to go, not a wall.
    """
    if composite == 0:
        return composite, [], ""
    from app.strategies import volume_profile_strategies as vp

    tf = str(cfg.get("technical.primary_timeframe", "5m"))
    candles = (getattr(ctx, "candles", None) or {}).get(tf) or []
    quote = getattr(ctx, "quote", None)
    price = float(getattr(quote, "last_price", 0.0) or 0.0)
    atr = float(((getattr(ctx, "indicators", None) or {}).get("primary") or {})
                .get("atr", 0.0) or 0.0)
    if not candles or price <= 0:
        return composite, [], ""
    profiles = vp.profiles_for(candles, cfg)
    if not profiles:
        return composite, [], ""

    want = 1 if composite > 0 else -1
    g = cfg.get
    notes: list[str] = []
    aligned = vp.level_alignment(price, want, profiles, atr,
                                 float(g("volume_profile.alignment_tolerance_atr", 0.25)))
    if aligned:
        gain = float(g("volume_profile.alignment_boost", 0.30))
        composite = max(-1.0, min(1.0, composite + want * gain))
        notes.append(f"Volume profile: {'long' if want > 0 else 'short'} at the "
                     f"{aligned} (+{gain:.2f})")

    target = None
    for report in reports or []:
        if getattr(report, "agent_id", "") in {"volume_profile", "candlestick"} \
                and getattr(report, "suggested_target", None) \
                and (getattr(report, "score", 0.0) or 0.0) * want > 0:
            target = float(report.suggested_target)
            break
    wall, zone = vp.hvn_wall(price, want, profiles, atr, target,
                             float(g("volume_profile.hvn_near_atr", 1.0)),
                             float(g("volume_profile.hvn_veto_atr", 0.25)))
    veto = ""
    if wall == "veto" and zone is not None:
        veto = (f"Volume profile veto: {'buying' if want > 0 else 'selling'} straight "
                f"into a thick HVN {zone.low:.2f}-{zone.high:.2f} — accepted "
                f"inventory stalls the move")
    elif wall == "penalty" and zone is not None:
        cost = float(g("volume_profile.hvn_penalty", 0.30))
        composite = composite - want * min(cost, abs(composite))
        notes.append(f"Volume profile: HVN {zone.low:.2f}-{zone.high:.2f} within "
                     f"{g('volume_profile.hvn_near_atr', 1.0)} ATR ahead (-{cost:.2f})")
    return composite, notes, veto


def volume_profile_backs(reports: list[Any], composite: float) -> bool:
    """Did the Volume Profile analyst trigger in this trade's direction?"""
    for report in reports or []:
        score = float(getattr(report, "score", 0.0) or 0.0)
        if getattr(report, "agent_id", "") == "volume_profile" \
                and getattr(report, "data_available", True) \
                and abs(score) >= 0.25 and score * composite > 0:
            return True
    return False
