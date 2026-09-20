"""Agent re-weighting from realised outcomes.

The desk keeps a per-agent, per-regime scorecard. An agent whose calls keep
being right in trending markets earns a higher weight there without also being
rewarded in chop. Weights are clamped so a hot streak can never let one agent
dominate the vote, and a cold streak can never silence it entirely.
"""
from __future__ import annotations

from typing import Any

from app.core.bus import Topic, bus
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.storage import db

log = get_logger("learning.feedback")


class FeedbackLoop:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("learning.enabled", True)) and \
            self.cfg.get("learning.method", "ewma") != "off"

    async def update_from_closed(self, closed: list[dict[str, Any]]) -> dict[str, Any]:
        """Grade every agent that contributed to each closed signal."""
        if not self.enabled or not closed:
            return {}

        alpha = float(self.cfg.get("learning.alpha", 0.08))
        floor = float(self.cfg.get("learning.weight_floor", 0.2))
        ceiling = float(self.cfg.get("learning.weight_ceiling", 2.0))
        min_samples = int(self.cfg.get("learning.min_samples_before_update", 15))

        # agent -> regime -> [correct, samples, r_sum]
        tallies: dict[str, dict[str, list[float]]] = {}

        for item in closed:
            signal_id = item["signal_id"]
            rows = db.reports_for_signal(signal_id)
            if not rows:
                continue
            stored = db.get_signal(signal_id)
            regime = (stored or {}).get("regime") or "all"

            r_multiple = item["r_multiple"]
            winning_direction = 1 if r_multiple > 0 else -1

            for row in rows:
                if not row["data_available"]:
                    continue          # abstention is neither right nor wrong
                agent_id = row["agent_id"]
                score = row["score"] or 0.0
                if abs(score) < 0.15:
                    continue          # a neutral call isn't a prediction

                agent_direction = 1 if score > 0 else -1
                correct = 1.0 if agent_direction == winning_direction else 0.0
                # Credit is proportional to how strongly the agent committed.
                contribution = abs(score) * r_multiple * agent_direction * winning_direction

                for bucket in (regime, "all"):
                    slot = tallies.setdefault(agent_id, {}).setdefault(bucket, [0.0, 0.0, 0.0])
                    slot[0] += correct
                    slot[1] += 1.0
                    slot[2] += contribution

        if not tallies:
            return {}

        existing = {(p["agent_id"], p["regime"]): p for p in db.agent_performance()}
        base_weights = self.cfg.get("weights", {}) or {}
        updates: dict[str, Any] = {}

        for agent_id, buckets in tallies.items():
            for regime, (correct, samples, r_sum) in buckets.items():
                prev = existing.get((agent_id, regime))
                total_samples = (prev["samples"] if prev else 0) + int(samples)
                total_correct = (prev["correct"] if prev else 0) + int(correct)
                total_r = (prev["total_r"] if prev else 0.0) + r_sum
                weight = prev["weight"] if prev else float(base_weights.get(agent_id, 1.0))

                if total_samples >= min_samples:
                    hit_rate = total_correct / total_samples
                    avg_r = total_r / total_samples
                    # Two signals: was it right, and was being right worth anything.
                    performance = (hit_rate - 0.5) * 2.0 * 0.5 + max(-1.0, min(1.0, avg_r)) * 0.5
                    weight = weight * (1 - alpha) + (1.0 + performance) * alpha
                    weight = max(floor, min(ceiling, weight))

                db.upsert_agent_performance(agent_id, regime, total_samples,
                                            total_correct, round(total_r, 4),
                                            round(weight, 4))
                if regime == "all":
                    updates[agent_id] = {
                        "weight": round(weight, 4),
                        "samples": total_samples,
                        "hit_rate": round(total_correct / total_samples, 3) if total_samples else 0.0,
                        "avg_r": round(total_r / total_samples, 3) if total_samples else 0.0,
                    }

        if updates:
            log.info("agent weights updated: %s",
                     ", ".join(f"{k}={v['weight']}" for k, v in updates.items()))
            await bus.publish(Topic.LEARNING, {"weights": updates})
        return updates

    def apply_learned_weights(self) -> dict[str, float]:
        """Merge learned weights into the live config so the CMIO uses them."""
        if not self.enabled:
            return self.cfg.get("weights", {}) or {}
        learned = db.get_agent_weights()
        if not learned:
            return self.cfg.get("weights", {}) or {}
        weights = dict(self.cfg.get("weights", {}) or {})
        weights.update(learned)
        self.cfg.settings["weights"] = weights
        return weights

    def recall_for(self, symbol: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Recent graded signals fed back into agent prompts — the 'learn from what
        actually happened' channel."""
        limit = limit or int(self.cfg.get("learning.recall_examples", 8))
        out = []
        for row in db.recent_signals(limit=60, symbol=symbol):
            if row.get("r_multiple") is None:
                continue
            out.append({
                "ts": row["ts"],
                "bias": row["bias"],
                "outcome": row["status"],
                "r_multiple": row["r_multiple"] or 0.0,
                "regime": row.get("regime"),
                "note": (row.get("rationale") or "")[:140],
            })
            if len(out) >= limit:
                break
        return out

    def scorecard(self) -> dict[str, Any]:
        rows = db.agent_performance()
        by_agent: dict[str, Any] = {}
        for r in rows:
            entry = by_agent.setdefault(r["agent_id"], {"regimes": {}})
            stats = {
                "samples": r["samples"],
                "hit_rate": round(r["correct"] / r["samples"], 3) if r["samples"] else 0.0,
                "avg_r": round(r["total_r"] / r["samples"], 3) if r["samples"] else 0.0,
                "weight": r["weight"],
            }
            if r["regime"] == "all":
                entry.update(stats)
            else:
                entry["regimes"][r["regime"]] = stats
        return by_agent
