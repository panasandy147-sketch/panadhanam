#!/usr/bin/env python3
"""Backtest SJK 9-15-21 (the user's 9 / 15 / 21 EMA fan) on real candles.

    python -m scripts.backtest_sjk_9_15_21 --market US --days 40
    python -m scripts.backtest_sjk_9_15_21 --market IN --days 40 --rr 2.5

Walks every watchlist symbol's 5m candles bar by bar (no lookahead) through
app/strategies/sjk_9_15_21.backtest — one position at a time, out at the
stop, the 1:rr target or the square-off — and reports, for the earlier and
the later half of the sessions and for all of them:

  alone   every trade the strategy took, in R
  rules   the same trades through the desk's account rules (the morning's
          screened list, the entry windows, max open, trades a day, the daily
          lockout), in % of capital — what the desk would actually have done

Fills at the stop / target exactly, no slippage or costs; a candle touching
both is a stop. Real results will be a little worse.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import rules_sim  # noqa: E402
from app.core.config import get_config  # noqa: E402
from app.indicators import ta  # noqa: E402
from app.strategies import sjk_9_15_21 as fan  # noqa: E402


async def trades_for(feed, cfg, symbols: list[str], days: int, tz: str):
    rows, dailies = [], {}
    tech = cfg.get("technical", {}) or {}
    for sym in symbols:
        try:
            bars = await feed.get_candles(sym, "5m", 75 * days + 300)
            dailies[sym] = await feed.get_candles(sym, "1d", 90)
        except Exception as exc:                          # noqa: BLE001
            print(f"  skip {sym}: {exc}", file=sys.stderr)
            continue
        index = {b.ts.isoformat(): k for k, b in enumerate(bars)}
        for t in fan.backtest(bars, cfg, tz):
            k = index.get(t["ts"])
            snap = ta.compute_all(ta.candles_to_df(bars[max(0, k - 300):k + 1]), tech) \
                if k is not None else {}
            rows.append({**t, "symbol": sym, "side": "BUY" if t["direction"] == "LONG" else "SELL",
                         "entry_ts": t["ts"], "r_intraday": t["r"],
                         "exit_ts_intraday": t["exit_ts"], "outcome_intraday": t["outcome"],
                         "vwap": (snap or {}).get("vwap"), "atr": (snap or {}).get("atr")})
        print(f"  {sym}: {sum(1 for r in rows if r['symbol'] == sym)} trades", flush=True)
    return rows, dailies


def report(cfg, rows, dailies) -> dict:
    days = sorted({r["entry_ts"][:10] for r in rows})
    half = days[len(days) // 2] if days else ""
    out = {"sessions": len(days), "split_at": half}
    for name, pick in (("earlier", [r for r in rows if r["entry_ts"][:10] < half]),
                       ("later", [r for r in rows if r["entry_ts"][:10] >= half]),
                       ("all", rows)):
        pick = sorted(pick, key=lambda r: r["entry_ts"])
        sim = rules_sim.simulate(cfg, pick, "r_intraday", "exit_ts_intraday", dailies)
        out[name] = {"alone": fan.summary(pick),
                     "rules": {k: sim.get(k) for k in ("trades", "expectancy_r", "return_pct",
                                                       "max_drawdown_pct", "win_rate")}}
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--market", default="US", choices=["US", "IN"])
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--rr", type=float, default=None, help="override sjk_9_15_21.rr")
    args = ap.parse_args()
    cfg = get_config()
    cfg.switch_market(args.market)
    if args.rr:
        cfg.settings["sjk_9_15_21"]["rr"] = args.rr
    from app.data.feeds.yahoo import YahooFeed
    feed = YahooFeed()
    await feed.connect()
    tz = str(cfg.get("system.timezone"))
    symbols = [w["symbol"] for w in cfg.watchlist()]
    print(f"SJK 9-15-21 · {args.market} · {len(symbols)} symbols · {args.days} sessions "
          f"· 1:{cfg.get('sjk_9_15_21.rr')}")
    rows, dailies = await trades_for(feed, cfg, symbols, args.days, tz)
    out = report(cfg, rows, dailies)
    print(f"\n{out['sessions']} sessions, split at {out['split_at']}")
    for name in ("earlier", "later", "all"):
        print(f"  {name:8s} alone {out[name]['alone']}")
        print(f"  {'':8s} rules {out[name]['rules']}")


if __name__ == "__main__":
    asyncio.run(main())
