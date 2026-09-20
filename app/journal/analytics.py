"""Journal analytics: R-multiple realisation and the Mistake Cost Index."""
from __future__ import annotations

import json
from typing import Any

from app.journal.store import entries


def compute_analytics(limit: int = 1000) -> dict[str, Any]:
    rows = entries(limit=limit)
    if not rows:
        return _empty()

    total = len(rows)
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    total_r = sum(r["r_multiple"] or 0.0 for r in rows)
    clean = sum(1 for r in rows if not json.loads(r["mistakes"] or "[]")
                and r["stop_honoured"])

    scores = [r["execution_score"] for r in rows if r["execution_score"]]

    # --- Mistake Cost Index -------------------------------------------------
    # Money lost specifically on trades that broke a rule. A loss on a clean
    # setup is the cost of doing business and is deliberately excluded; this
    # number isolates what indiscipline alone costs.
    mistake_cost = 0.0
    clean_loss = 0.0
    by_mistake: dict[str, dict[str, Any]] = {}

    for r in rows:
        tags = json.loads(r["mistakes"] or "[]")
        violated = bool(tags) or not r["stop_honoured"]
        pnl = r["pnl"] or 0.0
        if violated and pnl < 0:
            mistake_cost += abs(pnl)
        elif not violated and pnl < 0:
            clean_loss += abs(pnl)
        for tag in tags:
            slot = by_mistake.setdefault(tag, {"count": 0, "cost": 0.0})
            slot["count"] += 1
            if pnl < 0:
                slot["cost"] += pnl          # negative: shown as a loss

    by_verdict: dict[str, dict[str, Any]] = {}
    for r in rows:
        v = r["verdict"] or "UNKNOWN"
        slot = by_verdict.setdefault(v, {"count": 0, "total_r": 0.0})
        slot["count"] += 1
        slot["total_r"] += r["r_multiple"] or 0.0

    by_setup: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = r["setup"] or "Other"
        slot = by_setup.setdefault(s, {"count": 0, "wins": 0, "total_r": 0.0})
        slot["count"] += 1
        slot["total_r"] += r["r_multiple"] or 0.0
        if (r["pnl"] or 0) > 0:
            slot["wins"] += 1
    for slot in by_setup.values():
        slot["win_rate"] = slot["wins"] / slot["count"] * 100 if slot["count"] else 0.0
        slot["avg_r"] = slot["total_r"] / slot["count"] if slot["count"] else 0.0

    return {
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": wins / total * 100,
        "total_r": round(total_r, 3),
        "avg_r": round(total_r / total, 4),
        "clean_count": clean,
        "clean_pct": clean / total * 100,
        "avg_execution_score": sum(scores) / len(scores) if scores else 0.0,
        "mistake_cost_index": round(mistake_cost, 2),
        "clean_loss_total": round(clean_loss, 2),
        # What your discipline is worth: the share of losses that were avoidable.
        "avoidable_loss_pct": round(
            mistake_cost / (mistake_cost + clean_loss) * 100, 1)
        if (mistake_cost + clean_loss) else 0.0,
        "by_mistake": {k: {"count": v["count"], "cost": round(v["cost"], 2)}
                       for k, v in by_mistake.items()},
        "by_verdict": {k: {"count": v["count"], "total_r": round(v["total_r"], 3)}
                       for k, v in by_verdict.items()},
        "by_setup": {k: {"count": v["count"], "win_rate": round(v["win_rate"], 1),
                         "avg_r": round(v["avg_r"], 4)}
                     for k, v in by_setup.items()},
    }


def _empty() -> dict[str, Any]:
    return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_r": 0.0,
            "avg_r": 0.0, "clean_count": 0, "clean_pct": 0.0,
            "avg_execution_score": 0.0, "mistake_cost_index": 0.0,
            "clean_loss_total": 0.0, "avoidable_loss_pct": 0.0,
            "by_mistake": {}, "by_verdict": {}, "by_setup": {}}
