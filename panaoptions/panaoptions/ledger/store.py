"""Persistence: SQLite for queries, CSV for a spreadsheet, both under data/.

The point of a 30-day paper run is the record it leaves. It has to survive a
restart, a crash and the app being rewritten, so every closed trade is written
as soon as it closes rather than held in memory until the end of the day.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from panaoptions.config import DATA_DIR
from panaoptions.logging import get_logger
from panaoptions.models import PaperTrade

log = get_logger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    direction TEXT,
    contract TEXT,
    opened_at TEXT,
    closed_at TEXT,
    quantity INTEGER,
    entry_price REAL,
    stop_price REAL,
    target_1 REAL,
    target_2 REAL,
    realised_pnl REAL,
    exit_reason TEXT,
    session_date TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_date);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

CREATE TABLE IF NOT EXISTS sessions (
    session_date TEXT PRIMARY KEY,
    trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    realised_pnl REAL,
    halted INTEGER,
    rejections TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS open_book (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals_seen (
    id TEXT PRIMARY KEY,
    ts TEXT,
    symbol TEXT,
    direction TEXT,
    taken INTEGER,
    reason TEXT,
    payload TEXT
);
"""

_conn: sqlite3.Connection | None = None


def db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "panaoptions.db"


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(db_path()), check_same_thread=False, timeout=15.0)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def init() -> None:
    get_conn()
    log.info("ledger database at %s", db_path())


def save_open_book(trades: list[PaperTrade]) -> None:
    """The positions still open, exactly as held — replaced wholesale.

    Trades were written only when they closed, so a restart (every git pull)
    dropped whatever was open: never sold, never graded, gone from the record.
    """
    conn = get_conn()
    conn.execute("DELETE FROM open_book")
    conn.executemany("INSERT INTO open_book (id, payload) VALUES (?, ?)",
                     [(t.id, t.model_dump_json()) for t in trades])
    conn.commit()


def load_open_book() -> list[PaperTrade]:
    out: list[PaperTrade] = []
    for row in get_conn().execute("SELECT payload FROM open_book").fetchall():
        try:
            out.append(PaperTrade.model_validate_json(row[0]))
        except Exception as exc:                       # noqa: BLE001
            log.warning("could not restore an open trade: %s", exc)
    return out


def save_trade(trade: PaperTrade) -> None:
    session_date = (trade.closed_at or trade.opened_at).date().isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO trades (id, signal_id, symbol, direction, contract,
            opened_at, closed_at, quantity, entry_price, stop_price, target_1,
            target_2, realised_pnl, exit_reason, session_date, payload)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (trade.id, trade.signal_id, trade.symbol, trade.direction.value,
          trade.contract_label, trade.opened_at.isoformat(),
          trade.closed_at.isoformat() if trade.closed_at else None,
          trade.quantity, trade.entry_price, trade.stop_price, trade.target_1,
          trade.target_2, trade.realised_pnl,
          trade.exit_reason.value if trade.exit_reason else None,
          session_date, trade.model_dump_json()))
    conn.commit()
    _append_csv(trade, session_date)


def _append_csv(trade: PaperTrade, session_date: str) -> None:
    """A spreadsheet-friendly copy, because not everything needs SQL."""
    path = DATA_DIR / "trades.csv"
    is_new = not path.exists()
    fields = ["session_date", "id", "symbol", "direction", "contract",
              "opened_at", "closed_at", "quantity", "entry_price",
              "stop_price", "target_1", "target_2", "realised_pnl",
              "exit_reason"]
    try:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            if is_new:
                writer.writeheader()
            writer.writerow({
                "session_date": session_date, "id": trade.id,
                "symbol": trade.symbol, "direction": trade.direction.value,
                "contract": trade.contract_label,
                "opened_at": trade.opened_at.isoformat(),
                "closed_at": trade.closed_at.isoformat() if trade.closed_at else "",
                "quantity": trade.quantity, "entry_price": trade.entry_price,
                "stop_price": trade.stop_price, "target_1": trade.target_1,
                "target_2": trade.target_2,
                "realised_pnl": trade.realised_pnl,
                "exit_reason": trade.exit_reason.value if trade.exit_reason else "",
            })
    except OSError as exc:
        log.warning("could not append to trades.csv: %s", exc)


def save_session(date_iso: str, state: Any) -> None:
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO sessions (session_date, trades, wins, losses,
            realised_pnl, halted, rejections, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (date_iso, state.trades_taken, state.wins, state.losses,
          round(state.realised_pnl, 2), int(state.halted),
          json.dumps(state.rejections), datetime.now().isoformat()))
    conn.commit()


def save_signal_seen(signal_id: str, ts: datetime, symbol: str,
                     direction: str, taken: bool, reason: str,
                     payload: dict[str, Any] | None = None) -> None:
    """Every setup considered, taken or not.

    The rejected ones are the more useful half: they are what tells you
    whether a rule is selective or simply impossible.
    """
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO signals_seen (id, ts, symbol, direction, taken,
            reason, payload) VALUES (?,?,?,?,?,?,?)
    """, (signal_id, ts.isoformat(), symbol, direction, int(taken), reason,
          json.dumps(payload or {}, default=str)))
    conn.commit()


def trades(limit: int = 500, since: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM trades"
    params: list[Any] = []
    if since:
        q += " WHERE session_date >= ?"
        params.append(since)
    q += " ORDER BY opened_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in get_conn().execute(q, params).fetchall()]


def sessions(limit: int = 60) -> list[dict[str, Any]]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM sessions ORDER BY session_date DESC LIMIT ?",
        (limit,)).fetchall()]


def rejection_tally(since: str | None = None) -> dict[str, int]:
    """Why setups did not become trades, across sessions."""
    q = "SELECT reason FROM signals_seen WHERE taken = 0"
    params: list[Any] = []
    if since:
        q += " AND ts >= ?"
        params.append(since)
    out: dict[str, int] = {}
    for row in get_conn().execute(q, params).fetchall():
        key = (row["reason"] or "unknown").split(".")[0][:70]
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
