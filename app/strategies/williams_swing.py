"""Williams-Crabel Swing Book — Larry Williams' volatility breakout as he
traded it, after Toby Crabel's narrow-range day, held 1-4 days.

A separate paper book (TRIAL from 6 Oct 2026, US only) with its own capital
and ledger: never the intraday desk's limits, circuit breaker or records.
Both are champion strategies in their NATIVE setting — daily ranges, orders
resting from the open, positions held overnight — not the 5-minute intraday
versions the desk had been running.

    SETUP  yesterday was an NR4 day (its range the narrowest of the last
           williams_swing.nr days, Crabel), and the 20-day trend agrees
           (yesterday's close against the close trend_days sessions before)
    ENTRY  a buy-stop at today's open + k x yesterday's range (a sell-stop at
           the open - k x range when the trend is down); k = 0.3. Filled at
           the level, or the bar's open when it gapped through. The first
           side to trigger owns the day; one entry per symbol per day.
    STOP   today's open — back there and the expansion has failed. Checked
           on every 5m bar after the entry (the entry bar only if it CLOSES
           through it), every day the trade is held.
    EXIT   the FIRST PROFITABLE OPEN (Williams' bail-out): the first later
           session that opens beyond the entry is sold at that open; a gap
           through the stop is sold at the open; else the close of the
           max_hold_days-th session.
    SIZE   1% of the book's equity at risk (entry to stop), at most 25% of
           equity in one name, at most 5 positions.

Backtest (Nov 2023 - Oct 2026, the 61-name US watchlist, HOURLY bars so the
order of the breakout and the stop is known, 0.03% a side): +70.7% in the
first half (max drawdown 5.7%, profit factor 1.39) and +34.7% in the second
(4.7%, 1.22). Without the NR4 day: +55.6% / +8.8% (6.6% / 11.2%); at k 0.5
without the trend or the NR4 day the edge was gone (+16.9% / -0.7%).
python -m scripts.backtest_williams re-runs it.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logging import get_logger

log = get_logger("williams_swing")

SETUP_NAME = "Williams-Crabel Swing · NR4 + volatility breakout"


def params(cfg: Any) -> dict[str, Any]:
    g = cfg.get
    return {"k": float(g("williams_swing.k", 0.3)),
            "nr": int(g("williams_swing.nr", 4)),
            "trend_days": int(g("williams_swing.trend_days", 20)),
            "max_hold_days": int(g("williams_swing.max_hold_days", 4)),
            "risk_pct": float(g("williams_swing.risk_pct", 1.0)),
            "slots": max(1, int(g("williams_swing.slots", 5))),
            "max_position_pct": float(g("williams_swing.max_position_pct", 25.0)),
            "min_risk_pct": float(g("williams_swing.min_risk_pct", 0.2)),
            "fresh_bars": max(1, int(g("williams_swing.fresh_bars", 2))),
            "fractional": bool(g("williams_swing.fractional", True)),
            # India's cash market cannot carry a short overnight: longs only.
            "longs_only": bool(g("williams_swing.longs_only", False))}


def _f(b: Any, key: str) -> float:
    return float(b[key] if isinstance(b, dict) else getattr(b, {"o": "open", "h": "high",
                                                               "l": "low", "c": "close"}[key]))


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #
def day_setup(daily_before: Sequence[Any], today_open: float,
              p: dict[str, Any]) -> dict[str, Any] | None:
    """Today's levels from the daily bars BEFORE today, or None: not an NR
    day, no trend, or too little history."""
    need = max(p["nr"], p["trend_days"] + 1) + 1
    if len(daily_before) < need or today_open <= 0:
        return None
    prev = daily_before[-1]
    rng = _f(prev, "h") - _f(prev, "l")
    if rng <= 0:
        return None
    if p["nr"]:
        ranges = [_f(b, "h") - _f(b, "l") for b in daily_before[-p["nr"]:]]
        if rng > min(ranges):
            return None
    slope = _f(daily_before[-1], "c") - _f(daily_before[-1 - p["trend_days"]], "c")
    if slope == 0 or (slope < 0 and p.get("longs_only")):
        return None
    return {"up": today_open + p["k"] * rng, "dn": today_open - p["k"] * rng,
            "stop": today_open, "range": rng, "slope": slope, "open": today_open}


def trigger(bars: Sequence[Any], s: dict[str, Any]) -> tuple[bool, float, int] | None:
    """(long, fill, bar index) of the first bar today through the level on
    the trend's side, or None. The first side to trigger owns the day."""
    for j, b in enumerate(bars):
        if s["slope"] > 0 and _f(b, "h") >= s["up"]:
            return True, max(s["up"], _f(b, "o")), j
        if s["slope"] < 0 and _f(b, "l") <= s["dn"]:
            return False, min(s["dn"], _f(b, "o")), j
    return None


def stopped(bars: Sequence[Any], long: bool, stop: float) -> bool:
    return any((_f(b, "l") <= stop) if long else (_f(b, "h") >= stop) for b in bars)


# --------------------------------------------------------------------------- #
# Backtest (bars as dicts: d (ISO day), o, h, l, c, t (ordering key))
# --------------------------------------------------------------------------- #
def symbol_trades(sym: str, daily: list[dict[str, Any]], intraday: list[dict[str, Any]],
                  p: dict[str, Any]) -> list[dict[str, Any]]:
    """Every trade on one symbol, on intraday bars of the regular session."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for b in intraday:
        by_day.setdefault(b["d"], []).append(b)
    days = sorted(by_day)
    index = {b["d"]: i for i, b in enumerate(daily)}
    out: list[dict[str, Any]] = []
    busy_until = ""
    for di, day in enumerate(days):
        if day <= busy_until or day not in index:
            continue
        bars = by_day[day]
        s = day_setup(daily[:index[day]], bars[0]["o"], p)
        if s is None:
            continue
        hit = trigger(bars, s)
        if hit is None:
            continue
        long, px, j = hit
        stop, sign = s["stop"], (1 if long else -1)
        risk = abs(px - stop)
        if risk <= 0 or risk / px * 100 < p["min_risk_pct"]:
            continue
        exit_px, how, exit_day = None, "", day
        eb = bars[j]
        if (eb["c"] <= stop) if long else (eb["c"] >= stop):
            exit_px, how = stop, "stop"
        elif stopped(bars[j + 1:], long, stop):
            exit_px, how = stop, "stop"
        if exit_px is None:
            for d in range(1, p["max_hold_days"] + 1):
                if di + d >= len(days):
                    break
                nb = by_day[days[di + d]]
                exit_day = days[di + d]
                o = nb[0]["o"]
                if (o - px) * sign > 0:
                    exit_px, how = o, "first_profitable_open"
                elif (o - stop) * sign <= 0:
                    exit_px, how = o, "gap_stop"
                elif stopped(nb, long, stop):
                    exit_px, how = stop, "stop"
                elif d == p["max_hold_days"]:
                    exit_px, how = nb[-1]["c"], "time"
                if exit_px is not None:
                    break
        if exit_px is None:
            continue
        out.append({"symbol": sym, "day": day, "exit_day": exit_day, "long": long,
                    "entry": px, "risk": risk, "exit": exit_px,
                    "r": (exit_px - px) * sign / risk, "why": how, "t": eb["t"]})
        busy_until = exit_day
    return out


def book(trades: list[dict[str, Any]], p: dict[str, Any], cost_pct: float = 0.03
         ) -> dict[str, Any]:
    """The account: risk_pct of equity a trade, max_position_pct a name,
    `slots` positions at once; cost_pct a side."""
    eq = peak = 1.0
    dd = 0.0
    held: list[dict[str, Any]] = []
    taken: list[dict[str, Any]] = []
    for t in sorted(trades, key=lambda x: x["t"]):
        for h in sorted([h for h in held if h["exit_day"] < t["day"]], key=lambda h: h["exit_day"]):
            eq += h["pnl"]
            held.remove(h)
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)
        if len(held) >= p["slots"] or any(h["symbol"] == t["symbol"] for h in held):
            continue
        shares = min(eq * p["risk_pct"] / 100 / t["risk"],
                     eq * p["max_position_pct"] / 100 / t["entry"])
        pnl = shares * t["r"] * t["risk"] - shares * t["entry"] * cost_pct / 100 * 2
        held.append({**t, "pnl": pnl})
        taken.append({**t, "pnl": pnl})
    for h in sorted(held, key=lambda h: h["exit_day"]):
        eq += h["pnl"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    wins = [x for x in taken if x["pnl"] > 0]
    gross_win = sum(x["pnl"] for x in wins)
    gross_loss = -sum(x["pnl"] for x in taken if x["pnl"] <= 0)
    return {"trades": len(taken), "win_rate": round(100 * len(wins) / max(1, len(taken)), 1),
            "avg_r": round(sum(x["r"] for x in taken) / max(1, len(taken)), 3),
            "return_pct": round(100 * (eq - 1), 1), "max_drawdown_pct": round(100 * dd, 1),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None}


# --------------------------------------------------------------------------- #
# The live paper book
# --------------------------------------------------------------------------- #
def _hm(s: str) -> int:
    h, _, m = str(s).partition(":")
    return int(h) * 60 + int(m or 0)


class WilliamsBook:
    """The paper ledger (a JSON file per market). Run every few minutes in
    the session: exits first, then new entries into the free slots."""

    def __init__(self, broker: Any, cfg: Any, path: Path | None = None) -> None:
        self.broker, self.cfg = broker, cfg
        self._path = path
        self._setups: dict[str, dict[str, Any]] = {}       # symbol -> today's levels
        self._setups_day = ""
        self._last_run: datetime | None = None
        self.last_note = ""

    # ---- storage ---------------------------------------------------------
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        from app.core import config as config_mod
        return config_mod.DATA_DIR / f"williams_book-{self.cfg.active_market}.json"

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path().read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("capital", float(self.cfg.get("williams_swing.capital", 4000)))
        data.setdefault("realised", 0.0)
        data.setdefault("open", [])
        data.setdefault("closed", [])
        data.setdefault("traded", {})                       # symbol -> last entry day
        return data

    def save(self, data: dict[str, Any]) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str))
        tmp.replace(p)

    # ---- the clock -------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.cfg.get("williams_swing.enabled", False))

    def tz(self) -> ZoneInfo:
        return ZoneInfo(str(self.cfg.get("system.timezone")))

    async def maybe_run(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Every `williams_swing.every_minutes` (5) while the market is open."""
        from app.core import clock
        if not self.enabled():
            return None
        now = now or clock.market_now(str(self.cfg.get("system.timezone")))
        m = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not (_hm(self.cfg.get("system.market_open", "09:30")) + 5
                                      <= m < _hm(self.cfg.get("system.market_close", "16:00"))):
            return None
        every = timedelta(minutes=int(self.cfg.get("williams_swing.every_minutes", 5)))
        if self._last_run is not None and now - self._last_run < every:
            return None
        self._last_run = now
        return await self.run(now)

    # ---- data ------------------------------------------------------------
    async def _today_bars(self, symbol: str, now: datetime) -> list[Any]:
        """Today's CLOSED 5m bars of the regular session."""
        tz = self.tz()
        open_m = _hm(self.cfg.get("system.market_open", "09:30"))
        bars = await self.broker.get_candles(symbol, "5m", 120) or []
        out = []
        for b in bars:
            t = b.ts.astimezone(tz)
            if t.date() == now.date() and t.hour * 60 + t.minute >= open_m \
                    and t + timedelta(minutes=5) <= now:
                out.append(b)
        return sorted(out, key=lambda b: b.ts)

    async def _daily_before(self, symbol: str, now: datetime) -> list[Any]:
        tz = self.tz()
        bars = await self.broker.get_candles(symbol, "1d", 60) or []
        return sorted([b for b in bars if b.ts.astimezone(tz).date() < now.date()],
                      key=lambda b: b.ts)

    # ---- one run -----------------------------------------------------------
    async def run(self, now: datetime) -> dict[str, Any]:
        from app.core.bus import bus
        p = params(self.cfg)
        today = now.date().isoformat()
        data = self.load()
        exited: list[dict[str, Any]] = []
        entered: list[dict[str, Any]] = []
        close_m = _hm(self.cfg.get("system.market_close", "16:00"))
        late = now.hour * 60 + now.minute >= close_m - 5

        # 1. exits: the first profitable open, a gap stop, the stop, time
        keep = []
        for pos in data["open"]:
            try:
                bars = await self._today_bars(pos["symbol"], now)
            except Exception as exc:                         # noqa: BLE001
                log.debug("williams %s: %s", pos["symbol"], exc)
                keep.append(pos)
                continue
            long, stop, entry = pos["side"] == "LONG", pos["stop"], pos["entry"]
            sign = 1 if long else -1
            done: tuple[str, float] | None = None
            if bars:
                pos["last"] = float(bars[-1].close)
            if pos["entry_day"] != today and bars:
                if today not in pos["sessions"]:
                    pos["sessions"].append(today)
                    o = float(bars[0].open)
                    if (o - entry) * sign > 0:
                        done = ("first_profitable_open", o)
                    elif (o - stop) * sign <= 0:
                        done = ("gap_stop", o)
                if done is None and stopped(bars, long, stop):
                    done = ("stop", stop)
            elif pos["entry_day"] == today and bars:
                after = [b for b in bars if b.ts.isoformat() > pos["entry_bar"]]
                if stopped(after, long, stop):
                    done = ("stop", stop)
            if done is None and late and len(pos["sessions"]) >= p["max_hold_days"] and bars:
                done = ("time", float(bars[-1].close))
            if done is None:
                keep.append(pos)
                continue
            exited.append(self._close(data, pos, done[0], done[1], today))
        data["open"] = keep

        # 2. today's setups, once a day; then fresh triggers into free slots
        if not late and now.hour * 60 + now.minute < _hm(
                self.cfg.get("williams_swing.last_entry", "15:45")):
            if self._setups_day != today:
                self._setups = {}
                for item in self.cfg.watchlist():
                    sym = item["symbol"]
                    try:
                        bars = await self._today_bars(sym, now)
                        if not bars:
                            continue
                        s = day_setup(await self._daily_before(sym, now), float(bars[0].open), p)
                    except Exception as exc:                 # noqa: BLE001
                        log.debug("williams setup %s: %s", sym, exc)
                        continue
                    if s:
                        self._setups[sym] = s
                self._setups_day = today
                log.info("Williams-Crabel book: %d NR%d setups today (%s)", len(self._setups),
                         p["nr"], ", ".join(sorted(self._setups)) or "none")
            held = {o["symbol"] for o in data["open"]}
            for sym, s in sorted(self._setups.items()):
                if len(data["open"]) >= p["slots"]:
                    break
                if sym in held or data["traded"].get(sym) == today:
                    continue
                try:
                    bars = await self._today_bars(sym, now)
                except Exception:                            # noqa: BLE001
                    continue
                hit = trigger(bars, s)
                if hit is None:
                    continue
                long, fill, j = hit
                data["traded"][sym] = today
                if j < len(bars) - p["fresh_bars"]:
                    continue                                 # triggered while we were away
                risk = abs(fill - s["stop"])
                if risk <= 0 or risk / fill * 100 < p["min_risk_pct"]:
                    continue
                equity = data["capital"] + data["realised"]
                qty = min(equity * p["risk_pct"] / 100 / risk,
                          equity * p["max_position_pct"] / 100 / fill)
                qty = round(qty, 3) if p["fractional"] else float(int(qty))
                if qty <= 0:
                    continue
                pos = {"symbol": sym, "side": "LONG" if long else "SHORT",
                       "entry_day": today, "entry_bar": bars[j].ts.isoformat(),
                       "entry": round(fill, 4), "stop": round(s["stop"], 4), "qty": qty,
                       "risk": round(risk, 4), "range": round(s["range"], 4),
                       "sessions": [], "last": float(bars[-1].close)}
                eb = bars[j]
                through = (float(eb.close) <= s["stop"]) if long else (float(eb.close) >= s["stop"])
                if through or stopped(bars[j + 1:], long, s["stop"]):
                    entered.append(pos)
                    exited.append(self._close(data, pos, "stop", s["stop"], today))
                    continue
                data["open"].append(pos)
                held.add(sym)
                entered.append(pos)

        self.save(data)
        if entered or exited:
            self.last_note = (f"{today} {now:%H:%M}: {len(entered)} bought, "
                              f"{len(exited)} sold, {len(data['open'])} held")
            log.info("Williams-Crabel book — %s", self.last_note)
        for row in exited:
            await bus.publish("williams.trade", {"action": "SELL", **row})
        for row in entered:
            await bus.publish("williams.trade", {"action": "BUY", **row})
        return {"entered": entered, "exited": exited, "open": data["open"]}

    @staticmethod
    def _close(data: dict[str, Any], pos: dict[str, Any], why: str, px: float,
               today: str) -> dict[str, Any]:
        sign = 1 if pos["side"] == "LONG" else -1
        pnl = (px - pos["entry"]) * pos["qty"] * sign
        data["realised"] = round(data["realised"] + pnl, 2)
        row = {**pos, "exit": round(px, 4), "exit_day": today, "why": why,
               "pnl": round(pnl, 2), "r": round((px - pos["entry"]) * sign / pos["risk"], 2)}
        data["closed"].append(row)
        return row

    # ---- the dashboard ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        data = self.load()
        p = params(self.cfg)
        closed = data["closed"]
        unreal = sum((o.get("last", o["entry"]) - o["entry"]) * o["qty"]
                     * (1 if o["side"] == "LONG" else -1) for o in data["open"])
        wins = [c for c in closed if c["pnl"] > 0]
        return {"enabled": self.enabled(), "market": self.cfg.active_market,
                "slots": p["slots"], "k": p["k"], "nr": p["nr"],
                "capital": data["capital"], "realised": round(data["realised"], 2),
                "unrealised": round(unreal, 2),
                "equity": round(data["capital"] + data["realised"] + unreal, 2),
                "trades": len(closed),
                "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
                "setups_today": sorted(self._setups) if self._setups_day else [],
                "open": data["open"], "closed": closed[-20:][::-1], "note": self.last_note}
