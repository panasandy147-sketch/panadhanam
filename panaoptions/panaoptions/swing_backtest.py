"""The swing desk's evidence, re-runnable: Larry Williams' volatility breakout
held 1-4 days, on Yahoo's hourly bars (two years) and 5-minute bars (the last
60 days), US, India and gold.

    python run.py --swing-backtest            # every market, both bar sizes

Per symbol and day: yesterday's range from daily bars, today's open from the
first regular-session bar. Long: the first bar whose high reaches
open + k x range fills at max(level, that bar's open); short the mirror; the
first side to trigger owns the day; only with the 20-day trend. Stop at
today's open, checked on every bar AFTER the entry bar (the entry bar counts
only if it CLOSES through). Held overnight; out at the first profitable open,
else the stop on a later day, else the close of the 4th session.

Option R: a ~30-day at-the-money option, Black-Scholes, IV = 1.1 x the
20-day realised volatility at entry and at exit; R = option P&L over what
the option loses at the stop.

Hourly bars can hide a dip back to the open inside the entry hour: at k 0.5
with the trend, the hourly and the 5-minute results matched exactly on the
same 60 days; at k 0.3 the hourly result was inflated (+0.32R vs +0.09R on
the US), so k 0.3 is not the desk's setting.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx

GROUPS = {
    "US": ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "MSFT", "AMZN", "META", "GOOGL"],
    "IN": ["^NSEI", "^NSEBANK", "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
           "TCS.NS", "SBIN.NS", "AXISBANK.NS"],
    "GOLD": ["GC=F"],
}
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"


def bs(spot: float, strike: float, t: float, vol: float, call: bool = True,
       r: float = 0.04) -> float:
    """Black-Scholes price."""
    if t <= 0:
        return max(0.0, (spot - strike) if call else (strike - spot))
    d1 = (math.log(spot / strike) + (r + vol * vol / 2) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)

    def nd(x: float) -> float:
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    if call:
        return spot * nd(d1) - strike * math.exp(-r * t) * nd(d2)
    return strike * math.exp(-r * t) * nd(-d2) - spot * nd(-d1)


def realised_vol(daily: list[dict[str, Any]], i: int, n: int = 20) -> float:
    rets = [math.log(daily[j]["c"] / daily[j - 1]["c"])
            for j in range(max(1, i - n + 1), i + 1)]
    if len(rets) < 5:
        return 0.25
    m = sum(rets) / len(rets)
    return max(0.08, math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
               * math.sqrt(252))


async def chart(client: httpx.AsyncClient, sym: str, interval: str,
                rng: str) -> list[dict[str, Any]]:
    """Bars on the exchange's clock: {ts, d (date), o, h, l, c}."""
    r = await client.get(CHART.format(sym=sym), params={"interval": interval, "range": rng})
    res = r.json()["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", 0)
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, lo, cl = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, lo, cl):
            continue
        local = datetime.fromtimestamp(ts + off, UTC).replace(tzinfo=None)
        out.append({"ts": local, "d": local.date().isoformat(), "o": o, "h": h, "l": lo,
                    "c": cl})
    return out


def run(daily: list[dict[str, Any]], intraday: list[dict[str, Any]], k: float = 0.5,
        trend: bool = True, max_days: int = 4, dte: int = 30) -> list[dict[str, Any]]:
    """Every trade: {day, long, ur (stock R), or (option R), how, held}."""
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for b in intraday:
        by_day[b["d"]].append(b)
    days = sorted(by_day)
    didx = {b["d"]: i for i, b in enumerate(daily)}
    trades = []
    for n, day in enumerate(days):
        i = didx.get(day)
        if i is None or i < 22:
            continue
        p = daily[i - 1]
        rng = p["h"] - p["l"]
        if rng <= 0:
            continue
        bars = by_day[day]
        o = bars[0]["o"]
        up, dn = o + k * rng, o - k * rng
        slope = daily[i - 1]["c"] - daily[i - 21]["c"]
        entry = None
        for j, b in enumerate(bars):
            if b["h"] >= up and (not trend or slope > 0):
                entry = (j, True, max(up, b["o"]))
                break
            if b["l"] <= dn and (not trend or slope < 0):
                entry = (j, False, min(dn, b["o"]))
                break
        if not entry:
            continue
        j, long, px = entry
        stop, risk, sign = o, abs(px - o), (1 if long else -1)
        if risk <= 0:
            continue
        out = None
        if (bars[j]["c"] - stop) * sign <= 0:
            out = (stop, "STOP", 0)
        else:
            for b in bars[j + 1:]:
                if (b["l"] <= stop) if long else (b["h"] >= stop):
                    out = (stop, "STOP", 0)
                    break
        d = 0
        while out is None:
            d += 1
            if n + d >= len(days):
                break
            nb = by_day[days[n + d]]
            if (nb[0]["o"] - px) * sign > 0:
                out = (nb[0]["o"], "FIRST_PROFITABLE_OPEN", d)
                break
            if (nb[0]["o"] - stop) * sign <= 0:
                out = (nb[0]["o"], "GAP_STOP", d)
                break
            for b in nb:
                if (b["l"] <= stop) if long else (b["h"] >= stop):
                    out = (stop, "STOP", d)
                    break
            if out is None and d == max_days:
                out = (nb[-1]["c"], "TIME", d)
        if out is None:
            continue
        exit_px, how, held = out
        vol = realised_vol(daily, i - 1) * 1.1
        c0 = bs(px, px, dte / 365, vol, long)
        c1 = bs(exit_px, px, max(0, dte - held * 1.4) / 365, vol, long)
        cs = bs(stop, px, dte / 365, vol, long)
        trades.append({"day": day, "long": long, "ur": (exit_px - px) * sign / risk,
                       "or": (c1 - c0) / (c0 - cs) if c0 > cs else None,
                       "how": how, "held": held})
    return trades


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ors = [r["or"] for r in rows if r["or"] is not None]
    return {"trades": len(rows),
            "win_pct": round(sum(r["ur"] > 0 for r in rows) / len(rows) * 100) if rows else 0,
            "stock_r": round(sum(r["ur"] for r in rows) / len(rows), 3) if rows else 0.0,
            "option_r": round(sum(ors) / len(ors), 3) if ors else 0.0}


async def backtest(k: float = 0.5, trend: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for group, syms in GROUPS.items():
            hourly, five = [], []
            for s in syms:
                try:
                    dly = await chart(c, s, "1d", "5y")
                    daily = [{"d": b["d"], **{x: b[x] for x in ("o", "h", "l", "c")}}
                             for b in dly]
                    hourly += run(daily, await chart(c, s, "60m", "730d"), k, trend)
                    five += run(daily, await chart(c, s, "5m", "60d"), k, trend)
                except Exception:                           # noqa: BLE001
                    continue
            days = sorted({r["day"] for r in hourly})
            half = days[len(days) // 2] if days else ""
            out[group] = {
                "hourly_first_year": summarise([r for r in hourly if r["day"] < half]),
                "hourly_second_year": summarise([r for r in hourly if r["day"] >= half]),
                "five_minute_last_60_days": summarise(five)}
    return out
