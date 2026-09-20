#!/usr/bin/env python3
"""Replay the desk over historical candles to sanity-check the rules.

    python -m scripts.backtest --symbol RELIANCE --bars 500

This is deliberately simple: it walks a candle series forward, rebuilds the
context at each step from data available AT THAT POINT (no lookahead), runs the
rule-based analysts, and grades the resulting signals. It exists to catch
"my thresholds produce 400 signals a day" long before real money is involved.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.candlestick import CandlestickAgent  # noqa: E402
from app.agents.risk import RiskManager  # noqa: E402
from app.brokers.factory import build_broker  # noqa: E402
from app.core.config import get_config  # noqa: E402
from app.core.models import Bias, MarketContext  # noqa: E402
from app.indicators import patterns as pattern_mod  # noqa: E402
from app.indicators import ta  # noqa: E402


async def run(symbol: str, bars: int, warmup: int = 80) -> None:
    cfg = get_config()
    cfg.settings["system"]["no_new_entry_after"] = "23:59"
    broker = await build_broker(cfg)
    candles = await broker.get_candles(symbol, "5m", bars)
    if len(candles) < warmup + 20:
        print(f"Not enough data for {symbol}: got {len(candles)} bars.")
        return

    agent = CandlestickAgent(cfg)
    risk = RiskManager(cfg)
    tech = cfg.get("technical", {}) or {}

    trades: list[dict] = []
    open_trade: dict | None = None

    for i in range(warmup, len(candles)):
        window = candles[: i + 1]
        bar = candles[i]

        if open_trade:
            long = open_trade["side"] == "BUY"
            hit_stop = bar.low <= open_trade["sl"] if long else bar.high >= open_trade["sl"]
            hit_tgt = bar.high >= open_trade["tgt"] if long else bar.low <= open_trade["tgt"]
            if hit_stop or hit_tgt:
                r = 2.0 if hit_tgt and not hit_stop else -1.0
                open_trade["r"] = r
                trades.append(open_trade)
                open_trade = None
            continue

        df = ta.candles_to_df(window)
        ctx = MarketContext(symbol=symbol, cycle_id=f"bt-{i}")
        snapshot = ta.compute_all(df, tech)
        snapshot["patterns"] = pattern_mod.scan(df, tech.get("patterns_enabled"))
        ctx.indicators = {"primary": snapshot, "by_timeframe": {"5m": snapshot},
                          "mtf_alignment": {"aligned": False, "direction": 0}}
        from app.core.models import Quote
        ctx.quote = Quote(symbol=symbol, last_price=bar.close)

        report = agent.analyse_rules(ctx)
        if abs(report.score) < float(cfg.get("consensus.min_composite_score", 0.35)):
            continue

        ctx.__dict__["_reports"] = [report]
        bias = Bias.BULLISH if report.score > 0 else Bias.BEARISH
        signal = risk.evaluate(ctx, bias, [report], report.score,
                               ["candlestick"], rationale="backtest")
        if signal.status.value != "APPROVED":
            continue
        open_trade = {"i": i, "ts": bar.ts, "side": signal.side.value,
                      "entry": signal.entry, "sl": signal.stop_loss,
                      "tgt": signal.target, "r": 0.0}

    if not trades:
        print(f"{symbol}: no completed trades over {bars} bars — thresholds may be too tight.")
        return

    wins = [t for t in trades if t["r"] > 0]
    total_r = sum(t["r"] for t in trades)
    print(f"\n=== BACKTEST {symbol} ({bars} bars, 5m) ===")
    print(f"  trades      : {len(trades)}")
    print(f"  win rate    : {len(wins) / len(trades) * 100:.1f}%")
    print(f"  total R     : {total_r:+.1f}")
    print(f"  avg R/trade : {total_r / len(trades):+.2f}")
    print(f"  expectancy  : {'POSITIVE' if total_r > 0 else 'NEGATIVE'}")
    print("\nNote: this uses the single-agent rule engine on synthetic or historical")
    print("candles. It is a smoke test for threshold sanity, not a strategy backtest.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="RELIANCE")
    ap.add_argument("--bars", type=int, default=500)
    args = ap.parse_args()
    asyncio.run(run(args.symbol, args.bars))


if __name__ == "__main__":
    main()
