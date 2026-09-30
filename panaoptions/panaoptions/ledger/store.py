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

-- The daily circuit breaker's lockout: one row per calendar day it tripped.
CREATE TABLE IF NOT EXISTS lockouts (
    day TEXT PRIMARY KEY,
    reason TEXT,
    at TEXT
);

CREATE TABLE IF NOT EXISTS open_book (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

-- One at-the-money implied volatility per symbol per day, so the Derivatives
-- & Flow agent can say whether today's premium is dear against its own past.
CREATE TABLE IF NOT EXISTS iv_history (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    iv REAL NOT NULL,
    PRIMARY KEY (symbol, day)
);

-- Open interest per symbol per day: the first reading and the latest, so
-- "rising" can be judged intraday (NSE) or day over day (US chains carry
-- the previous close's OI).
CREATE TABLE IF NOT EXISTS fno_oi (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    first_ts TEXT, call_oi_first INTEGER, put_oi_first INTEGER,
    last_ts TEXT, call_oi INTEGER, put_oi INTEGER,
    PRIMARY KEY (symbol, day)
);

-- The previous day's F&O picture per symbol, as the desk mapped it.
CREATE TABLE IF NOT EXISTS fno_daily (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (symbol, day)
);

-- Every strategy check the desk makes, tallied per day: fired or not, and
-- the reason when not (numbers stripped, so the same rule groups together).
CREATE TABLE IF NOT EXISTS strategy_checks (
    day TEXT NOT NULL,
    strategy TEXT NOT NULL,
    outcome TEXT NOT NULL,          -- fired / not_fired
    reason TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, strategy, outcome, reason)
);

-- The same, per symbol: which names each strategy looked at and why not.
CREATE TABLE IF NOT EXISTS symbol_checks (
    day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, symbol, strategy, outcome, reason)
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
    """The shared connection, opened on first use.

    Built in a local and published only when ready: the web server's worker
    threads and the desk loop both call this, and reading the global back
    mid-setup could hand one of them a half-made (or reset) connection.
    """
    global _conn
    conn = _conn
    if conn is None:
        conn = sqlite3.connect(str(db_path()), check_same_thread=False, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.commit()
        _conn = conn
    return conn


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


def load_session(date_iso: str) -> dict[str, Any] | None:
    """Today's saved counters, so a restart does not wipe the day's losses,
    its trade count or a tripped breaker."""
    row = get_conn().execute("SELECT * FROM sessions WHERE session_date = ?",
                             (date_iso,)).fetchone()
    return dict(row) if row else None


def _generic(reason: str) -> str:
    """'volume 1.2x below 1.5x at 101.35' -> 'volume #x below #x at #': the
    same rule groups together whatever the numbers were that cycle."""
    import re
    text = re.sub(r"[-+]?\$?₹?\d[\d,]*(\.\d+)?", "#", str(reason or "no setup"))
    return re.sub(r"\s+", " ", text).strip()[:160]


def tally_checks(day: str, attempts: list[Any], symbol: str = "") -> None:
    """Count one cycle's strategy checks for one symbol (per strategy, and
    per symbol and strategy when the symbol is given)."""
    rows: dict[tuple[str, str, str], int] = {}
    for a in attempts:
        fired = bool(getattr(a, "triggered", False))
        key = (a.strategy.value, "fired" if fired else "not_fired",
               "" if fired else _generic((a.blockers or ["no setup"])[0]))
        rows[key] = rows.get(key, 0) + 1
    if not rows:
        return
    conn = get_conn()
    conn.executemany(
        "INSERT INTO strategy_checks (day, strategy, outcome, reason, n) VALUES (?,?,?,?,?) "
        "ON CONFLICT(day, strategy, outcome, reason) DO UPDATE SET n = n + excluded.n",
        [(day, *k, n) for k, n in rows.items()])
    if symbol:
        conn.executemany(
            "INSERT INTO symbol_checks (day, symbol, strategy, outcome, reason, n) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(day, symbol, strategy, outcome, reason) "
            "DO UPDATE SET n = n + excluded.n",
            [(day, symbol, *k, n) for k, n in rows.items()])
    conn.commit()


def symbol_checks(day: str) -> list[dict[str, Any]]:
    return [dict(r) for r in get_conn().execute(
        "SELECT symbol, strategy, outcome, reason, n FROM symbol_checks WHERE day = ? "
        "ORDER BY symbol, strategy, n DESC", (day,)).fetchall()]


def checks(day: str) -> list[dict[str, Any]]:
    return [dict(r) for r in get_conn().execute(
        "SELECT strategy, outcome, reason, n FROM strategy_checks WHERE day = ? "
        "ORDER BY n DESC", (day,)).fetchall()]


def save_lockout(date_iso: str, reason: str, at: datetime) -> None:
    """The circuit breaker's lockout for a calendar day: survives restarts."""
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO lockouts (day, reason, at) VALUES (?,?,?)",
                 (date_iso, reason, at.isoformat()))
    conn.commit()


def lockout(date_iso: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM lockouts WHERE day = ?",
                             (date_iso,)).fetchone()
    return dict(row) if row else None


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


def record_iv(symbol: str, day: str, iv: float) -> None:
    """Keep today's at-the-money IV for `symbol` (the latest reading wins)."""
    if not iv or iv <= 0:
        return
    try:
        conn = get_conn()
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO iv_history(symbol, day, iv) "
                     "VALUES (?, ?, ?)", (symbol, day, float(iv)))
        conn.commit()
    except sqlite3.Error as exc:
        log.debug("could not record IV for %s: %s", symbol, exc)


def iv_percentile(symbol: str, iv: float, before: str,
                  min_days: int = 10, days: int = 252) -> float | None:
    """Where `iv` sits among this symbol's past daily readings, 0-100.

    None until there are `min_days` of history — a percentile of three days
    is a guess dressed as a number.
    """
    try:
        rows = get_conn().execute(
            "SELECT iv FROM iv_history WHERE symbol = ? AND day < ? "
            "ORDER BY day DESC LIMIT ?", (symbol, before, days)).fetchall()
    except sqlite3.Error:
        return None
    past = [float(r[0]) for r in rows]
    if len(past) < min_days or not iv:
        return None
    return round(sum(1 for v in past if v <= iv) / len(past) * 100, 1)
