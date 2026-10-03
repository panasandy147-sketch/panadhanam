#!/usr/bin/env python3
"""Backtest the user's SJK strategies on real candles: SJK 9-15-21 (the 9 /
15 / 21 EMA fan, the default) or SJK 50-200 (the 50 / 200 EMA pullback).

    python -m scripts.backtest_sjk_9_15_21 --market US --days 40
    python -m scripts.backtest_sjk_9_15_21 --market IN --days 40 --rr 2.5
    python -m scripts.backtest_sjk_9_15_21 --strategy sjk50_200 --market IN --days 20

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
from app.strategies import sjk50_200, sjk912_vwapadx, sjk912rsi  # noqa: E402
from app.strategies import sjk_9_15_21 as fan  # noqa: E402

# --strategy -> (settings section, name, detector, warmup bars, extra history)
STRATEGIES = {
    "sjk_9_15_21": ("sjk_9_15_21", "SJK 9-15-21", fan.detect_candles, 63, 300),
    "sjk50_200": ("sjk50_200", "SJK 50-200", sjk50_200.detect_candles, 210, 700),
    # Its own engine walks the bars (and exits on the opposing crossover
    # when exit_mode says so): detector None = sjk912_vwapadx.backtest.
    "sjk912_vwapadx": ("sjk912_vwapadx", "SJK 9/21 · VWAP · ADX", None, 30, 300),
    "sjk912rsi": ("sjk912rsi", "sjk912RSi", None, 30, 300),
}
# The engines that walk the bars themselves.
OWN_WALK = {"sjk912_vwapadx": sjk912_vwapadx.backtest, "sjk912rsi": sjk912rsi.backtest}


async def trades_for(feed, cfg, symbols: list[str], days: int, tz: str,
                     strategy: str = "sjk_9_15_21"):
    _, _, detector, warmup, extra = STRATEGIES[strategy]
    rows, dailies = [], {}
    tech = cfg.get("technical", {}) or {}
    for sym in symbols:
        try:
            bars = await feed.get_candles(sym, "5m", 75 * days + extra)
            dailies[sym] = await feed.get_candles(sym, "1d", 90)
        except Exception as exc:                          # noqa: BLE001
            print(f"  skip {sym}: {exc}", file=sys.stderr)
            continue
        index = {b.ts.isoformat(): k for k, b in enumerate(bars)}
        # Only the last `days` sessions count; the bars before are warm-up.
        sessions = sorted({b.ts.date() for b in bars})[-days:]
        walk = fan.backtest if detector is not None else OWN_WALK[strategy]
        for t in walk(bars, cfg, tz, warmup=warmup, detector=detector):
            if t["ts"][:10] < sessions[0].isoformat():
                continue
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
    ap.add_argument("--strategy", default="sjk_9_15_21", choices=sorted(STRATEGIES))
    ap.add_argument("--rr", type=float, default=None, help="override the strategy's rr")
    ap.add_argument("--exit-mode", default=None, help="sjk912_vwapadx: target | cross | both")
    args = ap.parse_args()
    cfg = get_config()
    cfg.switch_market(args.market)
    section, label = STRATEGIES[args.strategy][:2]
    if args.rr:
        cfg.settings[section]["rr"] = args.rr
    if args.exit_mode:
        cfg.settings[section]["exit_mode"] = args.exit_mode
    from app.data.feeds.yahoo import YahooFeed
    feed = YahooFeed()
    await feed.connect()
    tz = str(cfg.get("system.timezone"))
    symbols = [w["symbol"] for w in cfg.watchlist()]
    print(f"{label} · {args.market} · {len(symbols)} symbols · {args.days} sessions "
          f"· 1:{cfg.get(section + '.rr')}")
    rows, dailies = await trades_for(feed, cfg, symbols, args.days, tz, args.strategy)
    out = report(cfg, rows, dailies)
    print(f"\n{out['sessions']} sessions, split at {out['split_at']}")
    for name in ("earlier", "later", "all"):
        print(f"  {name:8s} alone {out[name]['alone']}")
        print(f"  {'':8s} rules {out[name]['rules']}")


if __name__ == "__main__":
    asyncio.run(main())
