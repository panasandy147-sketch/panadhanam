"""Why today's setups were, or were not, bought — from the desk's own records.

Every setup the desk considers is written to signals_seen with the reason it
was taken or refused. Grouped, that turns "it is not buying" into one line
naming the rule doing it. Shared by `run.py --why` and the dashboard panel.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from datetime import datetime
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
    from panaoptions.ledger import store

    store.init()
    today = datetime.now().date().isoformat()
    rows = store.get_conn().execute(
        "SELECT ts, symbol, direction, taken, reason, payload FROM signals_seen "
        "WHERE ts >= ? ORDER BY ts", (today,)).fetchall()
    # A strategy that looked and passed ("no close beyond the opening range")
    # is not a setup that failed to buy; keep the two apart.
    fired = [r for r in rows if _fired(r)]
    taken = [r for r in fired if r[3]]
    refused = [r for r in fired if not r[3]]
    return {
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
    lines = [
        "",
        f"  Code version      {r['version']}   (the desk must be RESTARTED after a git pull)",
        f"  Capital           ${r['capital']:,.0f}",
        f"  Budget per trade  ${r['budget']:,.0f}",
        f"  Data provider     {r['provider']}",
    ]
    if not r["setups_fired"]:
        lines += ["", "  No setup has fired today — every strategy looked and passed "
                      f"({r['strategies_looked']} checks). Nothing reached the contract step."]
        return "\n".join(lines) + "\n"
    lines += ["", f"  Setups fired today {r['setups_fired']}   bought {r['bought']}   "
                  f"not bought {r['not_bought']}"]
    if r["reasons"]:
        lines += ["", "  Why not, most common first:"]
        lines += [f"    {x['count']:>3}x  {x['reason']}" for x in r["reasons"]]
    return "\n".join(lines) + "\n"
