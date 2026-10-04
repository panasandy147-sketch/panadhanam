#!/usr/bin/env python3
"""Backtest the RSI(2) swing book (Larry Connors' 2-period RSI pullback) on
years of daily bars, with the live settings (rsi2_swing in settings.yaml).

    python -m scripts.backtest_rsi2 --market US
    python -m scripts.backtest_rsi2 --market US --years 5 --rsi-max 10
    python -m scripts.backtest_rsi2 --market US --symbols SPY QQQ IWM DIA
    python -m scripts.backtest_rsi2 --market US --stop-pct 0      # no stop

Every watchlist symbol's daily bars from Yahoo (10 years by default), the
book's rules through app/strategies/rsi2_swing.backtest: entered at the
signal day's close, out at the first close above the 5-day average, the
max_hold_days-th close, or the emergency stop; `slots` positions of equal
equity, the lowest RSI(2) first. Costs per side: US 0.03%, India 0.12%
(STT, stamp). Reported for the earlier and the later half and all of it.

Today's watchlist holds names that did well, which flatters a long-only
test; --symbols SPY QQQ IWM DIA is the check without that bias.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import get_config  # noqa: E402
from app.strategies import rsi2_swing  # noqa: E402

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
           "Accept": "application/json"}


async def daily(client: httpx.AsyncClient, ticker: str, years: int) -> list:
    r = await client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                         params={"interval": "1d", "range": f"{years}y"}, headers=HEADERS)
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res["timestamp"]):
        o, h, lo, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, lo, c) or c <= 0:
            continue
        out.append(SimpleNamespace(ts=datetime.fromtimestamp(ts, UTC), open=o, high=h,
                                   low=lo, close=c))
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--market", default="US", choices=["US", "IN"])
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--rsi-max", type=float, default=None)
    ap.add_argument("--stop-pct", type=float, default=None)
    ap.add_argument("--symbols", nargs="*", default=None)
    args = ap.parse_args()

    cfg = get_config()
    cfg.switch_market(args.market)
    if args.rsi_max is not None:
        cfg.settings.setdefault("rsi2_swing", {})["rsi_max"] = args.rsi_max
    if args.stop_pct is not None:
        cfg.settings.setdefault("rsi2_swing", {})["stop_pct"] = args.stop_pct
    from app.data.feeds.yahoo import yahoo_ticker
    symbols = args.symbols or [w["symbol"] for w in cfg.watchlist()]
    tz = str(cfg.get("system.timezone"))
    data = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for sym in symbols:
            try:
                bars = await daily(client, yahoo_ticker(sym), args.years)
            except Exception as exc:                      # noqa: BLE001
                print(f"skip {sym}: {exc}", file=sys.stderr)
                continue
            if len(bars) > 400:
                data[sym] = bars
    cost = 0.03 if args.market == "US" else 0.12
    first = sorted(b[200].ts for b in data.values())[len(data) // 2]
    last = max(b[-1].ts for b in data.values())
    mid = (first + (last - first) / 2).date().isoformat()
    full = rsi2_swing.backtest(data, cfg, tz, cost)
    trades = [t for t in full["trades"] if t["entry_day"] >= first.date().isoformat()]
    out = {"market": args.market, "symbols": len(data), "from": first.date().isoformat(),
           "mid": mid, "to": last.date().isoformat(), "settings": rsi2_swing.params(cfg)}
    for name, pick in (("earlier", [t for t in trades if t["entry_day"] < mid]),
                       ("later", [t for t in trades if t["entry_day"] >= mid]),
                       ("all", trades)):
        book = rsi2_swing.portfolio(pick, out["settings"]["slots"])
        out[name] = {**rsi2_swing.summary(pick), "return_pct": book["return_pct"],
                     "max_drawdown_pct": book["max_drawdown_pct"]}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
