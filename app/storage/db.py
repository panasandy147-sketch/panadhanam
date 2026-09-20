"""SQLite persistence. No server to install — the DB is a file under data/runtime/."""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import get_config
from app.core.logging import get_logger

log = get_logger("storage")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    tradingsymbol TEXT,
    instrument_type TEXT,
    side TEXT,
    bias TEXT,
    entry REAL, stop_loss REAL, target REAL,
    entry_spot REAL, entry_delta REAL,
    quantity INTEGER, lots INTEGER,
    risk_reward REAL, total_risk REAL, notional REAL,
    composite_score REAL,
    status TEXT,
    regime TEXT,
    confirmations TEXT,
    rationale TEXT,
    counter_argument TEXT,
    rejection_reasons TEXT,
    exit_price REAL, exit_ts TEXT, pnl REAL, r_multiple REAL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);

CREATE TABLE IF NOT EXISTS agent_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT,
    cycle_id TEXT,
    ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bias TEXT, score REAL, confidence REAL,
    data_available INTEGER, used_llm INTEGER,
    rationale TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_agent ON agent_reports(agent_id);
CREATE INDEX IF NOT EXISTS idx_reports_signal ON agent_reports(signal_id);

CREATE TABLE IF NOT EXISTS agent_performance (
    agent_id TEXT NOT NULL,
    regime TEXT NOT NULL DEFAULT 'all',
    samples INTEGER DEFAULT 0,
    correct INTEGER DEFAULT 0,
    total_r REAL DEFAULT 0.0,
    weight REAL DEFAULT 1.0,
    updated_at TEXT,
    PRIMARY KEY (agent_id, regime)
);

CREATE TABLE IF NOT EXISTS cycles (
    cycle_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT,
    bias TEXT,
    composite_score REAL,
    proceeded INTEGER,
    duration_ms INTEGER,
    payload TEXT
);
"""


def _connect() -> sqlite3.Connection:
    path: Path = get_config().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = _connect()
        _local.conn.executescript(SCHEMA)
    return _local.conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    log.info("database ready at %s", get_config().db_path)


def _dump(obj: Any) -> str:
    return json.dumps(obj, default=str)


def save_signal(signal: Any) -> None:
    d = signal.model_dump(mode="json")
    inst = d["instrument"]
    with transaction() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO signals
            (id, ts, symbol, tradingsymbol, instrument_type, side, bias,
             entry, stop_loss, target, entry_spot, entry_delta, quantity, lots,
             risk_reward, total_risk,
             notional, composite_score, status, regime, confirmations, rationale,
             counter_argument, rejection_reasons, exit_price, exit_ts, pnl,
             r_multiple, payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            d["id"], d["ts"], d["instrument"]["symbol"], inst["tradingsymbol"],
            inst["instrument_type"], d["side"], d["bias"], d["entry"], d["stop_loss"],
            d["target"], d.get("entry_spot"), d.get("entry_delta"),
            d["quantity"], d["lots"], d["risk_reward"], d["total_risk"],
            d["notional"], d["composite_score"], d["status"], d.get("regime"),
            _dump(d["confirmations"]), d["rationale"], d["counter_argument"],
            _dump(d["rejection_reasons"]), d.get("exit_price"), d.get("exit_ts"),
            d.get("pnl"), d.get("r_multiple"), _dump(d),
        ))


def save_reports(reports: list, symbol: str, cycle_id: str,
                 signal_id: str | None = None) -> None:
    rows = []
    for r in reports:
        d = r.model_dump(mode="json")
        rows.append((signal_id, cycle_id, d["ts"], d["agent_id"], symbol,
                     d["bias"], d["score"], d["confidence"],
                     int(d["data_available"]), int(d["used_llm"]),
                     d["rationale"], _dump(d)))
    if not rows:
        return
    with transaction() as conn:
        conn.executemany("""
            INSERT INTO agent_reports
            (signal_id, cycle_id, ts, agent_id, symbol, bias, score, confidence,
             data_available, used_llm, rationale, payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)


def save_cycle(result: Any, proceeded: bool) -> None:
    with transaction() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO cycles
            (cycle_id, ts, symbol, bias, composite_score, proceeded, duration_ms, payload)
            VALUES (?,?,?,?,?,?,?,?)
        """, (result.cycle_id, result.ts.isoformat(), result.symbol,
              result.bias.value, result.composite_score, int(proceeded),
              result.duration_ms, _dump({"rejected": result.rejected})))


def recent_signals(limit: int = 50, symbol: str | None = None,
                   status: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM signals"
    clauses, params = [], []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in get_conn().execute(q, params).fetchall()]


def open_signals() -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM signals WHERE status IN ('APPROVED','OPEN') ORDER BY ts DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def update_outcome(signal_id: str, exit_price: float, pnl: float,
                   r_multiple: float, status: str) -> None:
    with transaction() as conn:
        conn.execute("""
            UPDATE signals SET exit_price=?, exit_ts=?, pnl=?, r_multiple=?, status=?
            WHERE id=?
        """, (exit_price, datetime.now().isoformat(), pnl, r_multiple, status, signal_id))


def get_signal(signal_id: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    return dict(row) if row else None


def reports_for_signal(signal_id: str) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM agent_reports WHERE signal_id = ?", (signal_id,)).fetchall()
    return [dict(r) for r in rows]


def get_agent_weights() -> dict[str, float]:
    rows = get_conn().execute(
        "SELECT agent_id, weight FROM agent_performance WHERE regime='all'").fetchall()
    return {r["agent_id"]: r["weight"] for r in rows}


def upsert_agent_performance(agent_id: str, regime: str, samples: int, correct: int,
                             total_r: float, weight: float) -> None:
    with transaction() as conn:
        conn.execute("""
            INSERT INTO agent_performance (agent_id, regime, samples, correct, total_r, weight, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(agent_id, regime) DO UPDATE SET
                samples=excluded.samples, correct=excluded.correct,
                total_r=excluded.total_r, weight=excluded.weight,
                updated_at=excluded.updated_at
        """, (agent_id, regime, samples, correct, total_r, weight,
              datetime.now().isoformat()))


def agent_performance(regime: str | None = None) -> list[dict[str, Any]]:
    if regime:
        rows = get_conn().execute(
            "SELECT * FROM agent_performance WHERE regime=? ORDER BY agent_id", (regime,)).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT * FROM agent_performance ORDER BY agent_id, regime").fetchall()
    return [dict(r) for r in rows]


def stats_summary() -> dict[str, Any]:
    conn = get_conn()
    row = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='APPROVED' OR status='OPEN' THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN status='REJECTED' THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl <= 0 AND pnl IS NOT NULL THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(pnl), 0) AS total_pnl,
            COALESCE(AVG(r_multiple), 0) AS avg_r
        FROM signals
    """).fetchone()
    d = dict(row)
    closed = (d.get("wins") or 0) + (d.get("losses") or 0)
    d["win_rate"] = round((d.get("wins") or 0) / closed * 100, 1) if closed else 0.0
    d["closed"] = closed
    return d
