"""Journal analytics: which strategy pays, and what indiscipline costs."""
from __future__ import annotations

import json
from typing import Any

from panaoptions.journal.store import entries


def compute(limit: int = 1000, since: str | None = None,
            until: str | None = None) -> dict[str, Any]:
    return analyse(entries(limit=limit, since=since, until=until))


def analyse(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return _empty()

    total = len(rows)
    wins = [r for r in rows if (r["pnl"] or 0) > 0]
    scores = [r["execution_score"] for r in rows if r["execution_score"]]

    # Money lost ONLY on trades that broke a rule. A loss on a clean setup is
    # the cost of having an edge and is excluded deliberately: this number
    # isolates what indiscipline alone cost.
    mistake_cost = 0.0
    clean_loss = 0.0
    clean = 0
    by_mistake: dict[str, dict[str, Any]] = {}

    for row in rows:
        tags = json.loads(row["mistakes"] or "[]")
        pnl = row["pnl"] or 0.0
        if not tags:
            clean += 1
            if pnl < 0:
                clean_loss += abs(pnl)
        elif pnl < 0:
            mistake_cost += abs(pnl)
        for tag in tags:
            slot = by_mistake.setdefault(tag, {"count": 0, "cost": 0.0})
            slot["count"] += 1
            if pnl < 0:
                slot["cost"] = round(slot["cost"] + pnl, 2)

    by_strategy: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row["strategy"] or "Other"
        slot = by_strategy.setdefault(
            name, {"count": 0, "wins": 0, "total_pnl": 0.0, "scores": []})
        slot["count"] += 1
        slot["total_pnl"] = round(slot["total_pnl"] + (row["pnl"] or 0), 2)
        if (row["pnl"] or 0) > 0:
            slot["wins"] += 1
        if row["execution_score"]:
            slot["scores"].append(row["execution_score"])
    for slot in by_strategy.values():
        slot["win_rate"] = slot["wins"] / slot["count"] * 100
        slot["avg_pnl"] = round(slot["total_pnl"] / slot["count"], 2)
        slot["avg_score"] = (sum(slot["scores"]) / len(slot["scores"])
                             if slot["scores"] else 0.0)
        slot.pop("scores")

    by_verdict: dict[str, int] = {}
    for row in rows:
        key = row["verdict"] or "UNKNOWN"
        by_verdict[key] = by_verdict.get(key, 0) + 1

    return {
        "total": total,
        "wins": len(wins),
        "losses": total - len(wins),
        "win_rate": len(wins) / total * 100,
        "total_pnl": round(sum(r["pnl"] or 0 for r in rows), 2),
        "avg_pnl": round(sum(r["pnl"] or 0 for r in rows) / total, 2),
        "clean_count": clean,
        "clean_pct": clean / total * 100,
        "avg_execution_score": sum(scores) / len(scores) if scores else 0.0,
        "mistake_cost": round(mistake_cost, 2),
        "clean_loss": round(clean_loss, 2),
        "avoidable_loss_pct": round(
            mistake_cost / (mistake_cost + clean_loss) * 100, 1)
        if (mistake_cost + clean_loss) else 0.0,
        "by_mistake": by_mistake,
        "by_strategy": by_strategy,
        "by_verdict": by_verdict,
    }


def _empty() -> dict[str, Any]:
    return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "avg_pnl": 0.0, "clean_count": 0,
            "clean_pct": 0.0, "avg_execution_score": 0.0, "mistake_cost": 0.0,
            "clean_loss": 0.0, "avoidable_loss_pct": 0.0,
            "by_mistake": {}, "by_strategy": {}, "by_verdict": {}}
