#!/usr/bin/env python3
"""Start the whole system.

    python run.py                    start the server + dashboard
    python run.py --cycle            run one analysis cycle and exit
    python run.py --premarket        run the pre-market scan and exit
    python run.py --size 100000 1 24500 24400 75    position-sizing calculator
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _server() -> None:
    import uvicorn
    from dotenv import load_dotenv
    load_dotenv()
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload="--reload" in sys.argv,
        log_config=None,
    )


async def _one_cycle(premarket: bool = False) -> None:
    from app.brokers.factory import build_broker
    from app.core.config import get_config
    from app.scheduler import TradingEngine
    from app.storage import db

    cfg = get_config()
    db.init_db()
    broker = await build_broker(cfg)
    engine = TradingEngine(broker, cfg)

    if premarket:
        summary = await engine.run_premarket_scan()
        print("\n=== PRE-MARKET SCAN ===")
        print(f"Eligible   : {[e['symbol'] for e in summary['eligible']]}")
        print(f"Screened out: {[s['symbol'] for s in summary['screened_out']]}")
        for note in summary["macro_notes"]:
            print(f"  macro: {note}")
    else:
        results = await engine.run_cycle()
        print("\n=== CYCLE RESULTS ===")
        for r in results:
            line = f"{r['symbol']:<12} {r['bias']:<8} score={r['composite_score']:+.3f}"
            if r["signal"]:
                print(f"  ✅ {line}\n      {r['signal']}")
            else:
                reason = (r["rejected"] or ["no setup"])[0]
                print(f"  ·  {line}  → {reason[:96]}")
        print(f"\nRisk state: {engine.risk.snapshot()}")

    await broker.disconnect()


def _sizing(args: list[str]) -> None:
    from app.agents.risk import RiskManager
    capital, risk_pct, entry, stop = (float(x) for x in args[:4])
    lot = int(args[4]) if len(args) > 4 else 1
    result = RiskManager().size_calculator(capital, risk_pct, entry, stop, lot)
    print("\n=== POSITION SIZING ===")
    for k, v in result.items():
        print(f"  {k:<32} {v}")


def main() -> None:
    parser = argparse.ArgumentParser(description="panadhanam trading intelligence")
    parser.add_argument("--cycle", action="store_true", help="run one cycle and exit")
    parser.add_argument("--premarket", action="store_true", help="run the pre-market scan and exit")
    parser.add_argument("--size", nargs="+", metavar="N",
                        help="CAPITAL RISK_PCT ENTRY STOP [LOT_SIZE]")
    parser.add_argument("--reload", action="store_true", help="auto-reload the server")
    args = parser.parse_args()

    if args.size:
        _sizing(args.size)
    elif args.cycle:
        asyncio.run(_one_cycle())
    elif args.premarket:
        asyncio.run(_one_cycle(premarket=True))
    else:
        _server()


if __name__ == "__main__":
    main()
