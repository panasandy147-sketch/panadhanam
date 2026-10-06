#!/usr/bin/env python3
"""Backtest the Williams-Crabel swing book (app/strategies/williams_swing.py)
on two years of HOURLY bars, so the order of the breakout and the stop is
known, with the live settings and the book's account rules.

    python -m scripts.backtest_williams                 # the US watchlist
    python -m scripts.backtest_williams --nr 0          # without Crabel's NR day
    python -m scripts.backtest_williams --k 0.5 --nr 7

Reports the earlier and the later half and all of it: trades, win rate,
average R, return, max drawdown and profit factor; cost a side 0.03% (US),
0.12% (India: STT, stamp). --market IN --longs-only for India's cash market.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import get_config  # noqa: E402
from app.strategies import williams_swing as ws  # noqa: E402

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}


async def chart(client: httpx.AsyncClient, ticker: str, interval: str, rng: str,
                tz: ZoneInfo) -> list[dict]:
    r = await client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                         params={"interval": interval, "range": rng}, headers=HEADERS)
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, lo, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, lo, c):
            continue
        t = datetime.fromtimestamp(ts, UTC).astimezone(tz)
        out.append({"t": t.isoformat(), "d": t.date().isoformat(), "m": t.hour * 60 + t.minute,
                    "o": o, "h": h, "l": lo, "c": c})
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--market", default="US", choices=["US", "IN"])
    ap.add_argument("--longs-only", action="store_true",
                    help="no shorts (India's cash market cannot hold one overnight)")
    ap.add_argument("--k", type=float, default=None)
    ap.add_argument("--nr", type=int, default=None)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--interval", default="60m", choices=["60m", "5m"],
                    help="5m: the last 60 days at the live book's own resolution")
    args = ap.parse_args()
    cfg = get_config()
    cfg.switch_market(args.market)
    p = ws.params(cfg)
    if args.k is not None:
        p["k"] = args.k
    if args.nr is not None:
        p["nr"] = args.nr
    if args.longs_only:
        p["longs_only"] = True
    tz = ZoneInfo(str(cfg.get("system.timezone")))
    open_m = 9 * 60 + (15 if args.market == "IN" else 30)
    close_m = 15 * 60 + 30 if args.market == "IN" else 16 * 60
    cost = 0.12 if args.market == "IN" else 0.03          # % a side (India: STT, stamp)
    from app.data.feeds.yahoo import yahoo_ticker
    symbols = args.symbols or [w["symbol"] for w in cfg.watchlist()]
    trades = []
    first = last = ""
    async with httpx.AsyncClient(timeout=40) as client:
        for sym in symbols:
            try:
                daily = await chart(client, yahoo_ticker(sym), "1d", "3y", tz)
                hourly = [b for b in await chart(client, yahoo_ticker(sym), args.interval,
                                                 "730d" if args.interval == "60m" else "60d", tz)
                          if open_m <= b["m"] < close_m]
            except Exception as exc:                          # noqa: BLE001
                print(f"skip {sym}: {exc}", file=sys.stderr)
                continue
            if hourly:
                first = min(first or hourly[0]["d"], hourly[0]["d"])
                last = max(last, hourly[-1]["d"])
            trades += ws.symbol_trades(sym, daily, hourly, p)
    mid = (datetime.fromisoformat(first) + (datetime.fromisoformat(last)
                                            - datetime.fromisoformat(first)) / 2).date().isoformat()
    out = {"from": first, "mid": mid, "to": last, "settings": p,
           "cost_pct_a_side": cost,
           "earlier": ws.book([t for t in trades if t["day"] < mid], p, cost),
           "later": ws.book([t for t in trades if t["day"] >= mid], p, cost),
           "all": ws.book(trades, p, cost)}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
