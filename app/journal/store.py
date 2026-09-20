"""Journal persistence.

Two copies on purpose:
  * SQLite — queryable, drives the analytics.
  * Markdown under `journal/` — human-readable, diffable, and committed to git
    so your learning history survives the database, the machine and the app.

The markdown is the durable artefact. If everything else is lost, the cards
remain readable in any text editor.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import ROOT
from app.core.logging import get_logger
from app.journal.models import JournalEntry, MistakeCard
from app.storage.db import get_conn, transaction

log = get_logger("journal.store")

JOURNAL_DIR = ROOT / "journal"

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_entries (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    market TEXT,
    symbol TEXT NOT NULL,
    instrument TEXT,
    setup TEXT NOT NULL,
    side TEXT,
    planned_entry REAL, planned_stop REAL, planned_target REAL, planned_quantity INTEGER,
    actual_entry REAL, actual_exit REAL, actual_quantity INTEGER,
    entry_ts TEXT, exit_ts TEXT,
    slippage REAL, hold_minutes REAL,
    pnl REAL, initial_risk REAL, r_multiple REAL,
    stop_honoured INTEGER,
    mistakes TEXT,
    verdict TEXT,
    execution_score INTEGER,
    notes TEXT,
    signal_id TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal_entries(ts);
CREATE INDEX IF NOT EXISTS idx_journal_setup ON journal_entries(setup);
CREATE INDEX IF NOT EXISTS idx_journal_verdict ON journal_entries(verdict);

CREATE TABLE IF NOT EXISTS mistake_cards (
    trade_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    execution_score INTEGER,
    core_violation TEXT,
    verdict TEXT,
    r_multiple REAL,
    generated_by TEXT,
    markdown TEXT,
    payload TEXT
);
"""


def init_journal() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (JOURNAL_DIR / "cards").mkdir(exist_ok=True)


def save_entry(entry: JournalEntry) -> None:
    entry.compute()
    d = entry.model_dump(mode="json")
    with transaction() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO journal_entries
            (id, ts, market, symbol, instrument, setup, side,
             planned_entry, planned_stop, planned_target, planned_quantity,
             actual_entry, actual_exit, actual_quantity, entry_ts, exit_ts,
             slippage, hold_minutes, pnl, initial_risk, r_multiple,
             stop_honoured, mistakes, verdict, execution_score, notes,
             signal_id, payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            d["id"], d["ts"], d["market"], d["symbol"], d["instrument"],
            d["setup"], d["side"], d["planned_entry"], d["planned_stop"],
            d["planned_target"], d["planned_quantity"], d["actual_entry"],
            d["actual_exit"], d["actual_quantity"], d["entry_ts"], d["exit_ts"],
            d["slippage"], d["hold_minutes"], d["pnl"], d["initial_risk"],
            d["r_multiple"], int(d["stop_honoured"]), json.dumps(d["mistakes"]),
            d["verdict"], d["execution_score"], d["notes"], d["signal_id"],
            json.dumps(d, default=str),
        ))


def save_card(card: MistakeCard) -> Path:
    """Persist the card and write its markdown into the git-tracked journal."""
    d = card.model_dump(mode="json")
    markdown = card.to_markdown()

    with transaction() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO mistake_cards
            (trade_id, ts, execution_score, core_violation, verdict,
             r_multiple, generated_by, markdown, payload)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (card.trade_id, d["ts"], card.execution_score, card.core_violation,
              d["verdict"], card.r_multiple, card.generated_by, markdown,
              json.dumps(d, default=str)))

    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (JOURNAL_DIR / "cards").mkdir(exist_ok=True)
    path = JOURNAL_DIR / "cards" / f"{card.ts:%Y-%m-%d}-{card.trade_id}.md"
    path.write_text(markdown, encoding="utf-8")
    try:
        shown = path.relative_to(ROOT)
    except ValueError:
        # The journal can be configured outside the repo; a cosmetic path
        # calculation must never abort a save that already succeeded.
        shown = path
    log.info("mistake card written: %s", shown)
    return path


def entries(limit: int = 200, setup: str | None = None,
            verdict: str | None = None) -> list[dict[str, Any]]:
    """Journal rows, or an empty list if the journal has never been written.

    The tables are created on first use, so any read-only caller — the day
    report, the analytics panel — must tolerate their absence rather than
    crash on a fresh install.
    """
    import sqlite3

    q = "SELECT * FROM journal_entries"
    clauses, params = [], []
    if setup:
        clauses.append("setup = ?")
        params.append(setup)
    if verdict:
        clauses.append("verdict = ?")
        params.append(verdict)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    try:
        return [dict(r) for r in get_conn().execute(q, params).fetchall()]
    except sqlite3.OperationalError:
        return []


def cards(limit: int = 50) -> list[dict[str, Any]]:
    import sqlite3
    try:
        rows = get_conn().execute(
            "SELECT * FROM mistake_cards ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def get_entry(trade_id: str) -> dict[str, Any] | None:
    import sqlite3
    try:
        row = get_conn().execute(
            "SELECT * FROM journal_entries WHERE id = ?", (trade_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def export_summary() -> Path:
    """Regenerate journal/README.md — the index you read on GitHub."""
    from app.journal.analytics import compute_analytics

    stats = compute_analytics()
    rows = entries(limit=500)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Trading Journal",
        "",
        f"*Last updated {datetime.now():%Y-%m-%d %H:%M}*",
        "",
        "Process over outcome. A winning trade that broke the rules is a bad",
        "trade; a losing trade that honoured its stop is an acceptable one.",
        "",
        "## Scorecard",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Trades logged | {stats['total']} |",
        f"| Clean execution | {stats['clean_pct']:.0f}% |",
        f"| Win rate | {stats['win_rate']:.0f}% |",
        f"| Total R | {stats['total_r']:+.2f}R |",
        f"| Avg R per trade | {stats['avg_r']:+.3f}R |",
        f"| Avg execution score | {stats['avg_execution_score']:.1f}/10 |",
        f"| **Mistake Cost Index** | **{stats['mistake_cost_index']:,.0f}** |",
        "",
        "The Mistake Cost Index is the money lost on rule-violation trades. It is",
        "the cost of indiscipline alone — losses from valid setups that simply",
        "failed are excluded, because those are the price of doing business.",
        "",
    ]

    if stats["by_verdict"]:
        lines += ["## Verdicts", "", "| Verdict | Count | Total R |", "|---|---|---|"]
        for verdict, v in sorted(stats["by_verdict"].items()):
            lines.append(f"| {verdict} | {v['count']} | {v['total_r']:+.2f}R |")
        lines.append("")

    if stats["by_mistake"]:
        lines += ["## Most expensive mistakes", "",
                  "| Mistake | Times | Cost |", "|---|---|---|"]
        for name, v in sorted(stats["by_mistake"].items(),
                              key=lambda kv: kv[1]["cost"]):
            lines.append(f"| {name} | {v['count']} | {v['cost']:,.0f} |")
        lines.append("")

    if stats["by_setup"]:
        lines += ["## By setup", "",
                  "| Setup | Trades | Win rate | Avg R |", "|---|---|---|---|"]
        for name, v in stats["by_setup"].items():
            lines.append(f"| {name} | {v['count']} | {v['win_rate']:.0f}% | "
                         f"{v['avg_r']:+.3f}R |")
        lines.append("")

    if rows:
        lines += ["## Trade log", "",
                  "| Date | Symbol | Setup | Verdict | R | Mistakes |",
                  "|---|---|---|---|---|---|"]
        for r in rows[:100]:
            tags = ", ".join(json.loads(r["mistakes"] or "[]")) or "—"
            lines.append(
                f"| {r['ts'][:10]} | {r['symbol']} | {r['setup']} | "
                f"{r['verdict']} | {(r['r_multiple'] or 0):+.2f} | {tags} |")
        lines.append("")

    lines += ["## Cards", "",
              "Individual post-mortems live in [`cards/`](cards/).", ""]

    path = JOURNAL_DIR / "README.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
