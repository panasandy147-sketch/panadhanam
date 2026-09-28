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
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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


def _market_date(ts: str | None, tz: str) -> date | None:
    """The market-local date a trade was opened on (rows are stored in UTC)."""
    try:
        stamp = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(ZoneInfo(tz)).date()


def build(cfg: Any, days: int = 30, recent: int = 12, period: str = "days",
          day: date | None = None) -> dict[str, Any]:
    """The record for the last `days` days, or with period="day" for one
    market day (today by default) — every trade that day, not a sample."""
    from app.storage import db

    universe = {i["symbol"] for i in (cfg.universe.get("indices") or [])} | \
               {i["symbol"] for i in (cfg.universe.get("stocks") or [])}
    tz = str(cfg.get("system.timezone", "Asia/Kolkata"))
    if period == "day":
        from app.core import clock
        day = day or clock.market_now(tz).date()
        since = (datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(tz))
                 - timedelta(days=1)).astimezone(UTC).isoformat()
        rows = [r for r in db.recent_signals(limit=5000)
                if r["symbol"] in universe and (r.get("ts") or "") >= since
                and r["status"] != "REJECTED" and _market_date(r.get("ts"), tz) == day]
        recent = max(recent, 200)
    else:
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
        "period": period,
        "date": day.isoformat() if period == "day" and day else None,
        "market": getattr(cfg, "active_market", ""),
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
            "status": r["status"],
        } for r in latest],
        # Where the losses come from — the question the weekend review asks.
        "by_exit": _breakdown(closed, lambda r: _EXITS[r["status"]]),
        "by_symbol": _breakdown(closed, lambda r: r["symbol"]),
        "by_side": _breakdown(closed, lambda r: r["side"]),
    }


def _breakdown(closed: list[dict[str, Any]], key) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in closed:
        b = out.setdefault(str(key(r)), {"trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if (r.get("pnl") or 0) > 0 else 0
        b["pnl"] = round(b["pnl"] + (r.get("pnl") or 0.0), 2)
        b["r"] = round(b["r"] + (r.get("r_multiple") or 0.0), 2)
    for b in out.values():
        b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0
    return out


def save_day(cfg: Any, day: date | None = None) -> dict[str, str]:
    """Write the day's record and its audit to journal/daily/ for the weekly
    review: <date>-<market>-record.json (machine) and .md (you)."""
    import json

    from app.core import audit
    from app.journal import store

    rec = build(cfg, period="day", day=day)
    day = date.fromisoformat(rec["date"])
    market = str(rec.get("market") or "US")
    folder = store.JOURNAL_DIR / "daily"
    folder.mkdir(parents=True, exist_ok=True)
    events = audit.entries(day, market=market)
    stem = f"{day.isoformat()}-{market.lower()}-record"
    js = folder / f"{stem}.json"
    js.write_text(json.dumps({"record": rec, "audit": events}, indent=2, default=str),
                  encoding="utf-8")
    md = folder / f"{stem}.md"
    md.write_text(day_markdown(rec) + "\n" + audit.day_markdown(day, market),
                  encoding="utf-8")
    return {"json": str(js), "markdown": str(md)}


def day_markdown(rec: dict[str, Any]) -> str:
    cur = rec.get("currency", "")
    lines = [f"# Paper record — {rec.get('date')} ({rec.get('market')})", "",
             f"- Closed **{rec['closed']}** · win rate **{rec['win_rate']}%** "
             f"({rec['wins']}W / {rec['losses']}L"
             + (f" / {rec['flat']} flat" if rec.get("flat") else "") + ")",
             f"- P&L **{cur}{rec['total_pnl']:,.2f}** ({rec['total_r']:+.2f}R) · "
             f"avg win {cur}{rec['avg_win']:,.2f} · avg loss {cur}{rec['avg_loss']:,.2f} · "
             f"expectancy {cur}{rec['expectancy']:,.2f}",
             f"- Exits: {rec['exits']['target']} target · {rec['exits']['stop']} stop · "
             f"{rec['exits']['time']} time", ""]
    if rec.get("by_symbol"):
        lines += ["| Symbol | Trades | Win % | P&L | R |", "|---|---|---|---|---|"]
        for sym, b in sorted(rec["by_symbol"].items(), key=lambda kv: kv[1]["pnl"]):
            lines.append(f"| {sym} | {b['trades']} | {b['win_rate']} | "
                         f"{cur}{b['pnl']:,.2f} | {b['r']:+.2f} |")
        lines.append("")
    return "\n".join(lines)
