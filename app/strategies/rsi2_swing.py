"""RSI(2) Swing Book — Larry Connors' 2-period RSI pullback, held 1-10 days.

A separate paper book (TRIAL from 5 Oct 2026, US only), on daily bars, with
its own capital and ledger: it never uses the intraday desk's trade limits,
its circuit breaker or its records.

    ENTRY  once a day, rsi2_swing.at (15:45 ET), using the live price as
           today's close: the close above its rsi2_swing.trend_sma (200)-day
           average and the 2-period RSI below rsi2_swing.rsi_max (5). The
           lowest RSI first, one position per symbol, up to rsi2_swing.slots
           (5) positions of 1/slots of the book's equity each.
    EXIT   the first close above the rsi2_swing.exit_sma (5)-day average, or
           the close of the rsi2_swing.max_hold_days-th (10th) session after
           the entry, or the emergency stop rsi2_swing.stop_pct (25%) below
           the entry — far away on purpose: Connors' rule has no stop; a 5%
           stop cut the tested return, and 25% beat 15% in both halves.

Backtest (Jul 2017 - Oct 2026, the 61-name US watchlist, 5 slots, 0.03% a
side): with no stop, win rate 67.5%, +65.3% in the first half (Jul 2017 -
Feb 2022) and +136.0% in the second, max drawdown 15.4%, profit factor 1.59;
with the 25% stop +58.6% / +127.4%, max drawdown 14.6% (SPY held: +70.8% /
+79.7%, max drawdown 34.1%). On SPY / QQQ / IWM / DIA alone: 77% wins, profit factor
2.25, max drawdown 3.7%. India lost in every version — the 0.24% delivery
cost of a round trip is more than the average trade — so India keeps it off.
python -m scripts.backtest_rsi2 re-runs it.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logging import get_logger

log = get_logger("rsi2_swing")

SETUP_NAME = "RSI(2) Swing · Connors pullback"


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def rsi(closes: Sequence[float], n: int = 2) -> list[float | None]:
    """Wilder's RSI (the first value a simple average of n changes)."""
    out: list[float | None] = [None] * len(closes)
    gain = loss = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, lo = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            gain += g / n
            loss += lo / n
            if i < n:
                continue
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + lo) / n
        out[i] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def sma(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def params(cfg: Any) -> dict[str, Any]:
    g = cfg.get
    return {"rsi_max": float(g("rsi2_swing.rsi_max", 5)),
            "trend_sma": int(g("rsi2_swing.trend_sma", 200)),
            "exit_sma": int(g("rsi2_swing.exit_sma", 5)),
            "max_hold_days": int(g("rsi2_swing.max_hold_days", 10)),
            "stop_pct": float(g("rsi2_swing.stop_pct", 25.0) or 0.0),
            "slots": max(1, int(g("rsi2_swing.slots", 5))),
            "fractional": bool(g("rsi2_swing.fractional", True))}


def entry_signal(closes: Sequence[float], p: dict[str, Any]) -> dict[str, Any] | None:
    """The setup on the LAST close (today's, live), or None."""
    if len(closes) < p["trend_sma"] + 2:
        return None
    r = rsi(closes)[-1]
    trend = sma(closes, p["trend_sma"])[-1]
    if r is None or trend is None:
        return None
    if closes[-1] > trend and r < p["rsi_max"]:
        return {"rsi2": round(r, 2), "sma200": round(trend, 2), "close": closes[-1]}
    return None


def exit_reason(closes: Sequence[float], low: float, entry: float, sessions: int,
                p: dict[str, Any]) -> tuple[str, float] | None:
    """Why the position goes today, and at what price — or None to hold.

    `closes` ends with today's (live) close; `low` is today's low;
    `sessions` the sessions since the entry day (the entry day is 0)."""
    if sessions <= 0:
        return None
    if p["stop_pct"] > 0:
        stop = entry * (1 - p["stop_pct"] / 100)
        if low <= stop:
            return "stop", min(stop, closes[-1])
    fast = sma(closes, p["exit_sma"])[-1]
    if fast is not None and closes[-1] > fast:
        return "sma5", closes[-1]
    if sessions >= p["max_hold_days"]:
        return "time", closes[-1]
    return None


# --------------------------------------------------------------------------- #
# Backtest (daily bars), the same rules as the book
# --------------------------------------------------------------------------- #
def _bars(candles: Sequence[Any], tz: str) -> list[dict[str, Any]]:
    z = ZoneInfo(tz)
    out = []
    for c in candles:
        ts = c.ts.astimezone(z) if getattr(c.ts, "tzinfo", None) else c.ts
        out.append({"d": ts.date().isoformat(), "o": float(c.open), "h": float(c.high),
                    "l": float(c.low), "c": float(c.close)})
    return out


def symbol_trades(bars: list[dict[str, Any]], symbol: str, p: dict[str, Any],
                  cost_pct: float = 0.03) -> list[dict[str, Any]]:
    """Every trade on one symbol, entered at the signal day's close."""
    closes = [b["c"] for b in bars]
    r2, trend = rsi(closes), sma(closes, p["trend_sma"])
    trades: list[dict[str, Any]] = []
    i = p["trend_sma"]
    while i < len(bars) - 1:
        if r2[i] is None or trend[i] is None or not (
                closes[i] > trend[i] and r2[i] < p["rsi_max"]):
            i += 1
            continue
        entry, j, done = closes[i], i, None
        while j < len(bars) - 1 and done is None:
            j += 1
            done = exit_reason(closes[: j + 1], bars[j]["l"], entry, j - i, p)
        why, price = done if done else ("open", closes[j])
        ret = (price - entry) / entry - 2 * cost_pct / 100
        trades.append({"symbol": symbol, "entry_day": bars[i]["d"], "exit_day": bars[j]["d"],
                       "rsi2": round(r2[i], 2), "entry": entry, "exit": price,
                       "ret": ret, "sessions": j - i, "why": why})
        i = j + 1
    return trades


def portfolio(trades: list[dict[str, Any]], slots: int) -> dict[str, Any]:
    """`slots` equal slots of the book's equity; the lowest RSI first."""
    order = sorted(trades, key=lambda t: (t["entry_day"], t["rsi2"]))
    eq = peak = 1.0
    dd = 0.0
    held: list[dict[str, Any]] = []
    taken: list[dict[str, Any]] = []
    for t in order:
        for o in sorted([o for o in held if o["exit_day"] < t["entry_day"]],
                        key=lambda o: o["exit_day"]):
            eq += o["stake"] * o["ret"]
            held.remove(o)
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)
        if len(held) >= slots or any(o["symbol"] == t["symbol"] for o in held):
            continue
        held.append({**t, "stake": eq / slots})
        taken.append(t)
    for o in sorted(held, key=lambda o: o["exit_day"]):
        eq += o["stake"] * o["ret"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    return {"taken": taken, "return_pct": round(100 * (eq - 1), 2),
            "max_drawdown_pct": round(100 * dd, 2)}


def summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    rs = [t["ret"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {"trades": len(rs), "win_rate": round(100 * len(wins) / len(rs), 1),
            "avg_pct": round(100 * sum(rs) / len(rs), 3),
            "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                              if losses and sum(losses) else None),
            "avg_sessions": round(sum(t["sessions"] for t in trades) / len(trades), 1)}


def backtest(daily: dict[str, Sequence[Any]], cfg: Any, tz: str,
             cost_pct: float = 0.03) -> dict[str, Any]:
    p = params(cfg)
    trades = [t for sym, candles in daily.items()
              for t in symbol_trades(_bars(candles, tz), sym, p, cost_pct)]
    book = portfolio(trades, p["slots"])
    return {"all_signals": summary(trades), "book": {**summary(book["taken"]),
            "return_pct": book["return_pct"], "max_drawdown_pct": book["max_drawdown_pct"]},
            "trades": book["taken"]}


# --------------------------------------------------------------------------- #
# The live paper book
# --------------------------------------------------------------------------- #
def _hm(s: str) -> int:
    h, _, m = str(s).partition(":")
    return int(h) * 60 + int(m or 0)


class Rsi2Book:
    """The paper ledger: a JSON file per market under the runtime data folder.

    Run once a day from rsi2_swing.at until the close. Exits first, then
    entries into the free slots. Nothing is sent to a broker."""

    def __init__(self, broker: Any, cfg: Any, path: Path | None = None) -> None:
        self.broker, self.cfg = broker, cfg
        self._path = path
        self.last_note = ""

    # ---- storage ---------------------------------------------------------
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        from app.core import config as config_mod
        return config_mod.DATA_DIR / f"rsi2_book-{self.cfg.active_market}.json"

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path().read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("capital", float(self.cfg.get("rsi2_swing.capital", 4000)))
        data.setdefault("realised", 0.0)
        data.setdefault("open", [])
        data.setdefault("closed", [])
        data.setdefault("last_run", "")
        return data

    def save(self, data: dict[str, Any]) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str))
        tmp.replace(p)

    # ---- the clock -------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.cfg.get("rsi2_swing.enabled", False))

    def due(self, now: datetime) -> bool:
        if not self.enabled() or now.weekday() >= 5:
            return False
        m = now.hour * 60 + now.minute
        start = _hm(self.cfg.get("rsi2_swing.at", "15:45"))
        close = _hm(self.cfg.get("system.market_close", "16:00"))
        return start <= m < close and self.load()["last_run"] != now.date().isoformat()

    async def maybe_run(self, now: datetime | None = None) -> dict[str, Any] | None:
        from app.core import clock
        now = now or clock.market_now(str(self.cfg.get("system.timezone")))
        if not self.due(now):
            return None
        return await self.run(now)

    # ---- one daily run ----------------------------------------------------
    async def _series(self, symbol: str, today: date, tz: str
                      ) -> tuple[list[float], float, float, list[str]] | None:
        """Daily closes ending with today's live price, today's low, and the
        dates of the completed sessions before today."""
        candles = await self.broker.get_candles(symbol, "1d", 320)
        if not candles:
            return None
        z = ZoneInfo(tz)
        past = [c for c in candles if c.ts.astimezone(z).date() < today]
        todays = [c for c in candles if c.ts.astimezone(z).date() == today]
        quote = await self.broker.get_quote(symbol)
        price = float(quote.last_price) if quote and quote.last_price else (
            float(todays[-1].close) if todays else 0.0)
        if price <= 0 or not past:
            return None
        low = min([float(todays[-1].low)] if todays else [price])
        return ([float(c.close) for c in past] + [price], price, min(low, price),
                [c.ts.astimezone(z).date().isoformat() for c in past])

    async def run(self, now: datetime) -> dict[str, Any]:
        from app.core.bus import bus
        tz = str(self.cfg.get("system.timezone"))
        today = now.date()
        p = params(self.cfg)
        book = self.load()
        exited: list[dict[str, Any]] = []
        entered: list[dict[str, Any]] = []

        # 1. exits
        keep = []
        for pos in book["open"]:
            series = await self._series(pos["symbol"], today, tz)
            if series is None:
                keep.append(pos)
                continue
            closes, price, low, days = series
            # Sessions since the entry day, today included, from the bars —
            # a day the app was not running still counts.
            sessions = sum(1 for d in days if d > pos["entry_day"]) + 1
            pos.update(sessions=sessions, last_seen=today.isoformat(), last=price)
            done = exit_reason(closes, low, pos["entry"], sessions, p)
            if done is None:
                keep.append(pos)
                continue
            why, px = done
            pnl = (px - pos["entry"]) * pos["qty"]
            book["realised"] = round(book["realised"] + pnl, 2)
            row = {**pos, "exit": round(px, 4), "exit_day": today.isoformat(), "why": why,
                   "pnl": round(pnl, 2), "ret_pct": round(100 * (px / pos["entry"] - 1), 2)}
            book["closed"].append(row)
            exited.append(row)
        book["open"] = keep

        # 2. entries into the free slots, the lowest RSI(2) first
        held = {o["symbol"] for o in book["open"]} | {o["symbol"] for o in exited}
        free = p["slots"] - len(book["open"])
        if free > 0:
            found = []
            for item in self.cfg.watchlist():
                sym = item["symbol"]
                if sym in held:
                    continue
                try:
                    series = await self._series(sym, today, tz)
                except Exception as exc:                  # noqa: BLE001
                    log.debug("rsi2 %s: %s", sym, exc)
                    continue
                if series is None:
                    continue
                sig = entry_signal(series[0], p)
                if sig:
                    found.append((sig["rsi2"], sym, sig))
            equity = book["capital"] + book["realised"]
            stake = equity / p["slots"]
            for _, sym, sig in sorted(found)[:free]:
                px = sig["close"]
                qty = round(stake / px, 3) if p["fractional"] else float(int(stake // px))
                if qty <= 0:
                    continue
                pos = {"symbol": sym, "entry_day": today.isoformat(), "entry": round(px, 4),
                       "qty": qty, "rsi2": sig["rsi2"], "sma200": sig["sma200"],
                       "stop": round(px * (1 - p["stop_pct"] / 100), 4) if p["stop_pct"] else None,
                       "sessions": 0, "last_seen": today.isoformat(), "last": px}
                book["open"].append(pos)
                entered.append(pos)

        book["last_run"] = today.isoformat()
        self.save(book)
        self.last_note = (f"{today}: {len(entered)} bought, {len(exited)} sold, "
                          f"{len(book['open'])} held")
        log.info("RSI(2) swing book — %s", self.last_note)
        for row in exited:
            await bus.publish("rsi2.trade", {"action": "SELL", **row})
        for row in entered:
            await bus.publish("rsi2.trade", {"action": "BUY", **row})
        return {"entered": entered, "exited": exited, "open": book["open"]}

    # ---- the dashboard ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        book = self.load()
        p = params(self.cfg)
        closed = book["closed"]
        unreal = sum((o.get("last", o["entry"]) - o["entry"]) * o["qty"] for o in book["open"])
        wins = [c for c in closed if c["pnl"] > 0]
        return {"enabled": self.enabled(), "market": self.cfg.active_market,
                "at": self.cfg.get("rsi2_swing.at", "15:45"), "slots": p["slots"],
                "capital": book["capital"], "realised": round(book["realised"], 2),
                "unrealised": round(unreal, 2),
                "equity": round(book["capital"] + book["realised"] + unreal, 2),
                "trades": len(closed),
                "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
                "open": book["open"], "closed": closed[-20:][::-1],
                "last_run": book["last_run"], "note": self.last_note}
