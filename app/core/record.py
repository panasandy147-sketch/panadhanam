"""The paper record: every intraday trade the desk actually took, and how it went.

Only FILLED trades count. A signal approved on a day nobody armed is an
alert — the outcome tracker still follows it to see what it would have done,
but it was never bought, so it is kept out of the P&L and counted separately.
A trade counts as filled when it was saved as OPEN, which the dispatcher sets
the moment the paper broker fills the order; the status column later becomes
CLOSED_*, but the saved payload keeps what it was at entry.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.explain import why_sold

_EXITS = {"CLOSED_TARGET": "target", "CLOSED_STOP": "stop", "CLOSED_TIME": "time"}


def _filled(row: dict[str, Any]) -> bool:
    try:
        return json.loads(row.get("payload") or "{}").get("status") == "OPEN"
    except (TypeError, ValueError):
        return False


def _version(row: dict[str, Any]) -> str:
    try:
        return json.loads(row.get("payload") or "{}").get("code_version") or ""
    except (TypeError, ValueError):
        return ""


def _summary(closed: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [r for r in closed if (r.get("pnl") or 0) > 0]
    losses = [r for r in closed if (r.get("pnl") or 0) < 0]
    total = sum(r.get("pnl") or 0.0 for r in closed)
    exits = {name: 0 for name in _EXITS.values()}
    for r in closed:
        exits[_EXITS[r["status"]]] += 1
    return {
        "closed": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_pnl": round(total, 2),
        "total_r": round(sum(r.get("r_multiple") or 0.0 for r in closed), 2),
        "exits": exits,
    }


def build(cfg: Any, days: int = 30, recent: int = 12) -> dict[str, Any]:
    from app.storage import db

    universe = {i["symbol"] for i in (cfg.universe.get("indices") or [])} | \
               {i["symbol"] for i in (cfg.universe.get("stocks") or [])}
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = [r for r in db.recent_signals(limit=5000)
            if r["symbol"] in universe and (r.get("ts") or "") >= since
            and r["status"] != "REJECTED"]

    filled = [r for r in rows if _filled(r)]
    alerts = [r for r in rows if not _filled(r)]
    open_now = [r for r in filled if r["status"] in {"OPEN", "APPROVED"}]
    closed = [r for r in filled if r["status"] in _EXITS]

    wins = [r for r in closed if (r.get("pnl") or 0) > 0]
    losses = [r for r in closed if (r.get("pnl") or 0) < 0]
    flat = len(closed) - len(wins) - len(losses)     # out at breakeven
    total = sum(r.get("pnl") or 0.0 for r in closed)

    def avg(items: list[dict[str, Any]], key: str) -> float:
        return round(sum(r.get(key) or 0.0 for r in items) / len(items), 2) if items else 0.0

    exits = {name: 0 for name in _EXITS.values()}
    for r in closed:
        exits[_EXITS[r["status"]]] += 1

    latest = sorted(closed, key=lambda r: r.get("exit_ts") or r["ts"], reverse=True)[:recent]
    from app.core.version import code_version
    current = code_version()
    return {
        # Only trades placed by the code running now — so a fix is judged on
        # what it did, not blended with the trades from before it.
        "current": {"version": current,
                    **_summary([r for r in closed if _version(r) == current]),
                    "open_now": len([r for r in open_now if _version(r) == current])},
        "days": days,
        "currency": getattr(cfg.market, "currency_symbol", ""),
        "open_now": len(open_now),
        "open_symbols": [r["symbol"] for r in open_now],
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "flat": flat,
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_pnl": round(total, 2),
        "avg_win": avg(wins, "pnl"),
        "avg_loss": avg(losses, "pnl"),
        "expectancy": round(total / len(closed), 2) if closed else 0.0,
        "total_r": round(sum(r.get("r_multiple") or 0.0 for r in closed), 2),
        "avg_r": avg(closed, "r_multiple"),
        "exits": exits,
        "alerts_not_traded": len(alerts),
        "trades": [{
            "id": r["id"],
            "opened": r["ts"],
            "closed": r.get("exit_ts"),
            "symbol": r["symbol"],
            "side": r["side"],
            "quantity": r["quantity"],
            "entry": r["entry"],
            "exit": r.get("exit_price"),
            "pnl": r.get("pnl"),
            "r_multiple": r.get("r_multiple"),
            "exit_reason": why_sold(r),
        } for r in latest],
    }
