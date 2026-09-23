"""Journal persistence: SQLite for queries, markdown for reading.

The markdown cards are the durable artefact. If the database is lost they
remain readable in any text editor, and they are meant to be committed — a
learning history that does not outlive the machine is not a learning history.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from panaoptions.config import ROOT
from panaoptions.journal.models import Card, JournalEntry
from panaoptions.ledger.store import get_conn
from panaoptions.logging import get_logger

log = get_logger("journal.store")

JOURNAL_DIR = ROOT / "journal"

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id TEXT PRIMARY KEY,
    trade_id TEXT,
    ts TEXT NOT NULL,
    session_date TEXT,
    symbol TEXT,
    contract TEXT,
    strategy TEXT,
    pattern TEXT,
    claimed_accuracy REAL,
    direction TEXT,
    entry_price REAL, exit_price REAL, stop_price REAL,
    quantity INTEGER,
    pnl REAL, return_pct REAL, hold_minutes REAL,
    exit_reason TEXT,
    mistakes TEXT,
    verdict TEXT,
    execution_score INTEGER,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_session ON journal(session_date);
CREATE INDEX IF NOT EXISTS idx_journal_strategy ON journal(strategy);
CREATE INDEX IF NOT EXISTS idx_journal_pattern ON journal(pattern);

CREATE TABLE IF NOT EXISTS cards (
    trade_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    verdict TEXT,
    execution_score INTEGER,
    markdown TEXT,
    generated_by TEXT
);
"""


# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS is
# a no-op on an existing database, so without this an upgrade leaves anyone
# who has already traded with a table the new INSERT cannot fill — and the
# failure would land on the first graded trade after the update, which is the
# worst possible moment to discover it.
_ADDED_COLUMNS = (
    ("journal", "pattern", "TEXT"),
    ("journal", "claimed_accuracy", "REAL"),
)


def init() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    for table, column, kind in _ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            log.info("journal: added %s.%s", table, column)
    conn.commit()
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (JOURNAL_DIR / "cards").mkdir(exist_ok=True)


def save_entry(entry: JournalEntry) -> None:
    init()
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO journal (id, trade_id, ts, session_date, symbol,
            contract, strategy, pattern, claimed_accuracy, direction,
            entry_price, exit_price, stop_price,
            quantity, pnl, return_pct, hold_minutes, exit_reason, mistakes,
            verdict, execution_score, payload)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (entry.id, entry.trade_id, entry.ts.isoformat(),
          entry.ts.date().isoformat(), entry.symbol, entry.contract,
          entry.strategy.value, entry.pattern, entry.claimed_accuracy,
          entry.direction, entry.entry_price,
          entry.exit_price, entry.stop_price, entry.quantity, entry.pnl,
          entry.return_pct, entry.hold_minutes, entry.exit_reason,
          json.dumps([m.value for m in entry.mistakes]),
          entry.verdict.value if entry.verdict else None,
          entry.execution_score, entry.model_dump_json()))
    conn.commit()


def save_card(card: Card) -> Path:
    init()
    conn = get_conn()
    markdown = card.to_markdown()
    conn.execute("""
        INSERT OR REPLACE INTO cards (trade_id, ts, verdict, execution_score,
            markdown, generated_by) VALUES (?,?,?,?,?,?)
    """, (card.trade_id, card.ts.isoformat(), card.verdict.value,
          card.execution_score, markdown, card.generated_by))
    conn.commit()

    path = JOURNAL_DIR / "cards" / f"{card.ts:%Y-%m-%d}-{card.trade_id}.md"
    try:
        path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
    return path


def entries(limit: int = 500, since: str | None = None,
            until: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM journal"
    clauses, params = [], []
    if since:
        clauses.append("session_date >= ?")
        params.append(since)
    if until:
        clauses.append("session_date <= ?")
        params.append(until)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    try:
        return [dict(r) for r in get_conn().execute(query, params).fetchall()]
    except sqlite3.OperationalError:
        # A fresh install has no tables yet; a read must not crash on that.
        return []


def cards(limit: int = 50) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in get_conn().execute(
            "SELECT * FROM cards ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()]
    except sqlite3.OperationalError:
        return []


def export_index() -> Path:
    """Regenerate journal/README.md — the summary you read on GitHub."""
    from panaoptions.journal.analytics import analyse

    rows = entries(limit=1000)
    stats = analyse(rows)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "# panaoptions — Trading Journal",
        "",
        f"*Updated {datetime.now():%Y-%m-%d %H:%M}*",
        "",
        "Process over outcome. A winning trade that broke a rule is a bad",
        "trade; a losing trade that honoured its invalidation is an",
        "acceptable one.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Trades graded | {stats['total']} |",
        f"| Clean execution | {stats['clean_pct']:.0f}% |",
        f"| Win rate | {stats['win_rate']:.0f}% |",
        f"| Total P&L | {stats['total_pnl']:+,.2f} |",
        f"| Avg discipline | {stats['avg_execution_score']:.1f}/10 |",
        f"| Mistake cost | {stats['mistake_cost']:,.2f} |",
        "",
    ]
    if stats["by_strategy"]:
        lines += ["## By strategy", "",
                  "| Strategy | Trades | Win rate | Total | Avg discipline |",
                  "|---|---|---|---|---|"]
        for name, s in sorted(stats["by_strategy"].items(),
                              key=lambda kv: -kv[1]["total_pnl"]):
            lines.append(f"| {name} | {s['count']} | {s['win_rate']:.0f}% | "
                         f"{s['total_pnl']:+,.2f} | {s['avg_score']:.1f}/10 |")
        lines.append("")

    if stats.get("by_pattern"):
        lines += ["## By pattern", "",
                  "Published win rates come from studies of DAILY bars on",
                  "equities. This desk reads a 15m intraday tape, so they are",
                  "a hypothesis here — the `Yours` column is the only one",
                  "measured on your own data, and it is marked `—` until",
                  "there are enough trades for the comparison to mean",
                  "anything.",
                  "",
                  "| Pattern | Trades | Yours | Published | Gap | Total |",
                  "|---|---|---|---|---|---|"]
        for name, s in sorted(stats["by_pattern"].items(),
                              key=lambda kv: -kv[1]["count"]):
            measured = f"{s['win_rate']:.0f}%" if s["comparable"] else "—"
            published = f"{s['claimed']:.0%}" if s["claimed"] else "—"
            gap = f"{s['gap']:+.0f} pts" if s["gap"] is not None else "—"
            lines.append(f"| {name} | {s['count']} | {measured} | {published} "
                         f"| {gap} | {s['total_pnl']:+,.2f} |")
        lines.append("")

    path = JOURNAL_DIR / "README.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
