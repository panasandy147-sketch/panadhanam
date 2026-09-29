"""Why today's setups were, or were not, bought — from the desk's own records.

Every setup the desk considers is written to signals_seen with the reason it
was taken or refused. Grouped, that turns "it is not buying" into one line
naming the rule doing it. Shared by `run.py --why` and the dashboard panel.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def code_version() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, cwd=ROOT,
                              timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def report(cfg: Any, budget: float) -> dict[str, Any]:
    from panaoptions import clock
    from panaoptions.ledger import store

    store.init()
    # The desk's day is New York's, not the computer's: on a PC in another
    # timezone (or after 8 PM ET) the two differ and "today" came up empty.
    today = clock.now(cfg.timezone).date().isoformat()
    rows = store.get_conn().execute(
        "SELECT ts, symbol, direction, taken, reason, payload FROM signals_seen "
        "WHERE ts >= ? ORDER BY ts", (today,)).fetchall()
    # A strategy that looked and passed ("no close beyond the opening range")
    # is not a setup that failed to buy; keep the two apart.
    fired = [r for r in rows if _fired(r)]
    taken = [r for r in fired if r[3]]
    refused = [r for r in fired if not r[3]]
    return {
        "market": getattr(cfg, "market_name", "") or getattr(cfg, "market", ""),
        "currency": getattr(cfg, "currency", "$"),
        "day": today,
        "strategies": strategy_table(cfg, today),
        "version": code_version(),
        "capital": cfg.capital,
        "budget": round(budget, 2),
        "provider": cfg.get("data.provider"),
        "setups_fired": len(fired),
        "bought": len(taken),
        "not_bought": len(refused),
        "strategies_looked": len(rows) - len(fired),
        "reasons": [{"reason": reason, "count": n} for reason, n in
                    Counter((r[4] or "")[:220] for r in refused).most_common(8)],
        "latest": [{"time": str(r[0])[11:16], "symbol": r[1], "direction": r[2],
                    "reason": r[4]} for r in refused[-6:]][::-1],
    }


def strategy_table(cfg: Any, day: str) -> list[dict[str, Any]]:
    """Each strategy today: how often it was checked, how often it fired, and
    its most common reasons for NOT firing — or why it was never asked."""
    from panaoptions.engine.strategies import ALL
    from panaoptions.ledger import store
    try:
        rows = store.checks(day)
    except Exception:                                   # noqa: BLE001
        rows = []
    out = []
    for cls in ALL:
        s = cls(cfg)
        name = s.name.value
        mine = [r for r in rows if r["strategy"] == name]
        checked = sum(r["n"] for r in mine)
        fired = sum(r["n"] for r in mine if r["outcome"] == "fired")
        misses = sorted(((r["reason"], r["n"]) for r in mine if r["outcome"] == "not_fired"),
                        key=lambda x: -x[1])
        window = (f"{cfg.get(f'strategies.{s.key}.from', '?')}–"
                  f"{cfg.get(f'strategies.{s.key}.to', '?')}")
        note = ("switched off" if not s.enabled
                else f"never asked yet — its window is {window}" if not checked else "")
        out.append({"strategy": name, "enabled": s.enabled, "window": window,
                    "checked": checked, "fired": fired, "note": note,
                    "not_fired": [{"reason": r, "count": n} for r, n in misses[:2]]})
    return out


def _fired(row) -> bool:
    """A setup that triggered, rather than a strategy that looked and passed.

    Passes are saved with {"strategy", "confirmations"} only; a fired setup's
    refusal names a contract, a budget or a risk rule, or it was taken.
    """
    import json

    if row[3]:
        return True
    try:
        payload = json.loads(row[5] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return not ("strategy" in payload and "confirmations" in payload)


def render(r: dict[str, Any]) -> str:
    cur = r.get("currency") or "$"
    lines = [
        "",
        f"  ===== {r.get('market', '')} — {r.get('day', '')} =====",
        f"  Code version      {r['version']}   (the desk must be RESTARTED after a git pull)",
        f"  Capital           {cur}{r['capital']:,.0f}",
        f"  Budget per trade  {cur}{r['budget']:,.0f}",
        f"  Data provider     {r['provider']}",
    ]
    lines += _strategy_lines(r.get("strategies") or [])
    looked = sum(t["checked"] for t in r.get("strategies") or []) or r["strategies_looked"]
    if not r["setups_fired"] and not looked:
        lines += ["", "  The desk has not hunted yet today — before the first strategy's "
                      "window opens, or the desk was not running."]
        return "\n".join(lines) + "\n"
    if not r["setups_fired"]:
        lines += ["", "  No setup has fired today — every strategy looked and passed "
                      f"({looked} checks). Nothing reached the contract step."]
        return "\n".join(lines) + "\n"
    lines += ["", f"  Setups fired today {r['setups_fired']}   bought {r['bought']}   "
                  f"not bought {r['not_bought']}"]
    if r["reasons"]:
        lines += ["", "  Why not, most common first:"]
        lines += [f"    {x['count']:>3}x  {x['reason']}" for x in r["reasons"]]
    return "\n".join(lines) + "\n"


def _strategy_lines(table: list[dict[str, Any]]) -> list[str]:
    if not table:
        return []
    lines = ["", "  By strategy (every check today; a check = one symbol, one cycle):"]
    for t in table:
        head = f"    {t['strategy']:<28s}"
        if t["note"]:
            lines.append(f"{head} {t['note']}")
            continue
        lines.append(f"{head} checked {t['checked']:>5}   fired {t['fired']:>4}")
        for m in t["not_fired"]:
            lines.append(f"        not fired {m['count']:>4}x  {m['reason']}")
    return lines
