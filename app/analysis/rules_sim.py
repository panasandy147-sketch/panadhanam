"""The desk's account rules applied to replayed trades.

The weekly replay grades every setup the rule engine would have fired, on its
own. The desk never takes them all: it trades only the day's screened names,
in their windows, a few at a time, and stops for the day at its loss limit.
This runs the replayed trades (in time order, across symbols) through:

  the screener   each morning's Band A/B list, rebuilt from the daily bars
                 BEFORE that day (no look-ahead) — only those names; a Band B
                 name only on the side its close pointed to
  the windows    Band A in the morning window; nothing in the midday freeze;
                 Band A/B VWAP pullbacks only in the afternoon window
  the limits     risk.max_open_positions at once, risk.max_daily_trades a
                 day, and the daily lockout at risk.max_daily_loss_pct
                 (realised), each trade risking risk.risk_per_trade_pct

and reports the result in R and in % of capital, with the max drawdown.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


def _local(ts: str, tz: ZoneInfo) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=ZoneInfo("UTC"))
    return at.astimezone(tz)


def daily_bands(cfg: Any, daily: dict[str, list[Any]], day: date) -> dict[str, dict[str, Any]]:
    """The screener's pick for `day`, from bars dated before it."""
    from app.analysis import pre_market_screener as scr
    rows = {}
    for sym, bars in daily.items():
        done = [b for b in bars if scr._day(b) and scr._day(b) < day.isoformat()]
        m = scr.metrics(done)
        if m:
            rows[sym] = m
    picked = scr.bands(cfg, rows)
    return {r["symbol"]: r for r in picked["A"] + picked["B"]}


def simulate(cfg: Any, trades: list[dict[str, Any]], r_key: str, exit_key: str,
             daily: dict[str, list[Any]] | None = None) -> dict[str, Any]:
    from app.analysis import pre_market_screener as scr
    g = cfg.get
    tz = ZoneInfo(str(g("system.timezone", "Asia/Kolkata")))
    risk_pct = min(float(g("risk.risk_per_trade_pct", 1.0)),
                   float(g("risk.max_risk_per_trade_pct", 2.0)))
    limit_pct = float(g("risk.max_daily_loss_pct", 3.0))
    max_open = int(g("risk.max_open_positions", 2) or 2)
    max_daily = int(g("risk.max_daily_trades", 0) or 0)
    # No more entries after this many losing trades in a day (0 = off).
    max_losses = int(g("risk.max_losses_per_day", 0) or 0)
    losses: dict[str, int] = defaultdict(int)
    screened = bool(daily) and bool(g("screener.enabled", False))
    pull_atr = float(g("screener.vwap_pullback_atr", 0.5))

    bands_by_day: dict[date, dict[str, dict[str, Any]]] = {}
    skipped: Counter = Counter()
    taken: list[dict[str, Any]] = []
    open_pos: list[tuple[datetime, float, str]] = []      # (exit, r, day)
    realised: dict[str, float] = defaultdict(float)
    count: Counter = Counter()
    locked: set[str] = set()
    equity = peak = drawdown = 0.0                         # % of capital

    def book_until(t: datetime | None) -> None:
        nonlocal equity, peak, drawdown
        open_pos.sort(key=lambda p: p[0])
        while open_pos and (t is None or open_pos[0][0] <= t):
            _, r, day = open_pos.pop(0)
            equity += r * risk_pct
            realised[day] += r * risk_pct
            if r < 0:
                losses[day] += 1
            if realised[day] <= -limit_pct:
                locked.add(day)
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)

    rows = [t for t in trades if r_key in t and t.get("outcome_intraday") != "NONE"]
    rows.sort(key=lambda t: t.get("entry_ts", ""))
    for t in rows:
        start = _local(t.get("entry_ts", ""), tz)
        end = _local(t.get(exit_key) or t.get("entry_ts", ""), tz)
        if start is None or end is None:
            continue
        book_until(start)
        day = start.date().isoformat()
        long = t.get("side") == "BUY"
        if screened:
            if start.date() not in bands_by_day:
                bands_by_day[start.date()] = daily_bands(cfg, daily or {}, start.date())
            band = bands_by_day[start.date()].get(t["symbol"])
            if band is None:
                skipped["not on that morning's screened list"] += 1
                continue
            where = scr.window(cfg, start)
            if where in ("freeze", "closed"):
                skipped[f"outside the entry windows ({where})"] += 1
                continue
            if where == "morning" and band["band"] != "A":
                skipped["Band B before the afternoon window"] += 1
                continue
            if where == "afternoon" and not scr.is_vwap_pullback(
                    {"last_close": t["entry"], "vwap": t.get("vwap"), "atr": t.get("atr")},
                    long, pull_atr):
                skipped["afternoon: not a VWAP pullback"] += 1
                continue
            if (band["side"] == "LONG" and not long) or (band["side"] == "SHORT" and long):
                skipped["Band B: the wrong side"] += 1
                continue
        if day in locked:
            skipped["daily lockout"] += 1
            continue
        if max_losses and losses[day] >= max_losses:
            skipped[f"{max_losses} losses today — stopped for the day"] += 1
            continue
        if max_daily and count[day] >= max_daily:
            skipped[f"daily trade limit ({max_daily})"] += 1
            continue
        if len(open_pos) >= max_open:
            skipped[f"max open positions ({max_open})"] += 1
            continue
        r = float(t[r_key])
        taken.append({"symbol": t["symbol"], "day": day, "r": r})
        count[day] += 1
        open_pos.append((end, r, day))
    book_until(None)

    rs = [x["r"] for x in taken]
    wins = [r for r in rs if r > 0]
    return {
        "screened": screened,
        "trades": len(rs), "days": len(count),
        "win_rate": round(len(wins) / len(rs) * 100, 1) if rs else 0.0,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
        "total_r": round(sum(rs), 2),
        "return_pct": round(equity, 2),
        "max_drawdown_pct": round(drawdown, 2),
        "locked_days": len(locked),
        "skipped": dict(skipped.most_common()),
    }
