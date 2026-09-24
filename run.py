#!/usr/bin/env python3
"""Start the whole system.

    python run.py                    start the server + dashboard
    python run.py --cycle            run one analysis cycle and exit
    python run.py --premarket        run the pre-market scan and exit
    python run.py --size 100000 1 24500 24400 75    position-sizing calculator
    python run.py --check-data       verify you are getting REAL market data
    python run.py --check-llm        verify your LLM (Claude or local Ollama)
    python run.py --check-broker     verify your broker connection and account
    python run.py --set KEY=VALUE    change a setting in .env safely
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


async def _check_llm() -> bool:
    from pydantic import BaseModel, Field

    from app.core.config import get_config
    from app.core.llm import probe, structured_complete

    cfg = get_config()
    bar = "=" * 72
    print(f"\n{bar}\n  LLM CHECK\n{bar}")

    info = await probe()
    print(f"\n  Provider : {info['provider']}")
    if info["provider"] == "ollama":
        print(f"  Host     : {info.get('host')}")
        print(f"  Model    : {info.get('model')}")
        installed = info.get("installed") or []
        print(f"  Installed: {', '.join(installed) if installed else '(none)'}")
    elif info["provider"] == "anthropic":
        print(f"  Model    : {info.get('model')}")

    if not info["ok"]:
        print(f"\n  [XX] NOT READY: {info.get('error')}")
        print("\n  The system still runs — every agent has a deterministic rule")
        print("  engine. An LLM adds judgement on top, it is not required.")
        print(f"{bar}\n")
        return False

    # A live round trip proves more than a version check does.
    class _Ping(BaseModel):
        answer: str = Field(description="The single word: pong")
        confidence: float = Field(description="0.0 to 1.0")

    print("\n  Sending a test prompt (a local model may take a minute)…")
    result = await structured_complete(
        system="You are a test harness. Reply exactly as the schema requires.",
        prompt="Reply with the single word 'pong' and confidence 1.0.",
        schema=_Ping, max_tokens=200, cfg=cfg)

    if result is None:
        print("\n  [XX] The provider is reachable but produced no valid response.")
        print("       A small local model may struggle with structured output —")
        print("       try a larger one, e.g.  ollama pull qwen2.5:14b")
        print(f"{bar}\n")
        return False

    print(f"\n  [OK] Round trip succeeded: {result.answer!r}")
    print(f"\n  VERDICT: {cfg.llm_label} is working. Agents will use it.")
    print(f"{bar}\n")
    return True


def _sizing(args: list[str]) -> None:
    from app.agents.risk import RiskManager
    capital, risk_pct, entry, stop = (float(x) for x in args[:4])
    lot = int(args[4]) if len(args) > 4 else 1
    result = RiskManager().size_calculator(capital, risk_pct, entry, stop, lot)
    print("\n=== POSITION SIZING ===")
    for k, v in result.items():
        print(f"  {k:<32} {v}")


def _set_env(assignments: list[str]) -> int:
    """Edit .env for the user rather than making them do it by hand.

    Every setting that differs per machine lives in .env, which is untracked —
    so it cannot be changed by a git pull, and it has to be edited locally.
    Doing that by hand on Windows is where this keeps going wrong, so the app
    does it: `python run.py --set TOTAL_CAPITAL=100 --set OLLAMA_MODEL=qwen2.5:7b`.
    """
    from pathlib import Path

    from app.core.envfile import mask, parse_assignment, set_values

    root = Path(__file__).resolve().parent
    env, template = root / ".env", root / ".env.example"

    try:
        updates = dict(parse_assignment(a) for a in assignments)
    except ValueError as exc:
        print(f"\n  {exc}\n\n  Expected: --set KEY=VALUE\n")
        return 2

    existed = env.exists()
    outcome = set_values(env, updates, template=template)

    print(f"\n=== {env} ===")
    if not existed:
        print("  created from .env.example")
    for key, value in updates.items():
        print(f"  {key:<24} {mask(key, value):<28} ({outcome[key]})")

    # Two settings are worth a sentence of their own, because getting them
    # wrong is the difference between a simulation and real money.
    truthy = {"1", "true", "yes", "on"}
    if updates.get("AUTO_PLACE_ORDERS", "").strip().lower() in truthy:
        broker = updates.get("BROKER") or os.getenv("BROKER") or "paper"
        real_money = (broker.lower() not in {"paper", ""}
                      and os.getenv("ALPACA_PAPER", "true").lower() not in truthy)
        print("\n  Approved signals will now become orders on the "
              f"'{broker}' broker.")
        if real_money:
            print("  That broker may be pointed at REAL MONEY. Orders are still\n"
                  "  blocked unless TRADING_MODE=live and ENABLE_LIVE_ORDERS=true.")
        else:
            print("  That is a simulator, so the fills are simulated.")
        print("  You must still press 'Start trading day' each morning —\n"
              "  arming lasts one session and expires at square-off.")

    print("\n  Restart the app for this to take effect.\n")
    return 0


# Brokers that are real money by definition. Alpaca is real money only when
# ALPACA_PAPER is explicitly false; everything else here is a simulator.
_REAL_MONEY_BROKERS = {"zerodha", "upstox", "angelone"}


# The values .env.example used to ship. A .env still holding exactly these was
# copied from the template rather than chosen, and .env beats settings.yaml —
# so they would pin the desk to the old conservative risk for ever.
_TEMPLATE_RISK = {"RISK_PER_TRADE_PCT": 1.0, "MAX_DAILY_LOSS_PCT": 3.0,
                  "MIN_RISK_REWARD": 2.0}


def _retire_template_risk(env_path) -> list[str]:
    """Comment out template-default risk lines so settings.yaml decides.

    Only a line whose value is still exactly the old template default is
    touched; anything else was a choice and is left alone. Called on paper
    accounts only.
    """
    if not env_path.exists():
        return []
    lines = env_path.read_text(encoding="utf-8").splitlines()
    retired: list[str] = []
    for i, line in enumerate(lines):
        key, sep, value = line.strip().partition("=")
        if not sep or key not in _TEMPLATE_RISK:
            continue
        try:
            is_default = float(value.split("#")[0].strip()) == _TEMPLATE_RISK[key]
        except ValueError:
            continue
        if is_default:
            lines[i] = (f"# {line.strip()}   # retired: the template default; "
                        f"config/settings.yaml decides (see Rules on the dashboard)")
            retired.append(key)
    if retired:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"- Risk: {', '.join(retired)} in .env were the old template "
              f"defaults — retired, so config/settings.yaml decides")
    return retired


def _ensure_paper_orders() -> int:
    """Keep simulated order placement on for a PAPER account. Never real money.

    AUTO_PLACE_ORDERS in .env wins over settings.yaml by design — and
    .env.example used to ship it as false, so anyone whose .env was created
    from the template got a desk that analysed every signal, approved the good
    ones and placed nothing, while settings.yaml said it would trade. Paper
    trading is the whole point of a paper account, so on one this turns it
    on. On anything that can touch real money it does nothing at all; those
    still need TRADING_MODE=live and ENABLE_LIVE_ORDERS=true as well, and are
    never armed automatically.
    """
    from pathlib import Path

    from app.core.config import get_config
    from app.core.envfile import set_values

    cfg = get_config()
    broker = str(cfg.get("execution.broker", "paper")).strip().lower()
    alpaca_real = (broker == "alpaca" and os.getenv("ALPACA_PAPER", "true")
                   .strip().lower() in {"false", "0", "no", "off"})
    if broker in _REAL_MONEY_BROKERS or alpaca_real:
        print(f"- Orders: broker '{broker}' can reach real money — leaving "
              f"AUTO_PLACE_ORDERS exactly as it is.")
        return 0

    root = Path(__file__).resolve().parent
    _retire_template_risk(root / ".env")

    if bool(cfg.get("execution.auto_place_orders", False)):
        print(f"- Paper trading: ON ({broker} — simulated fills, no real money)")
        return 0

    set_values(root / ".env", {"AUTO_PLACE_ORDERS": "true"},
               template=root / ".env.example")
    print(f"- Paper trading: was OFF in .env — turned ON ({broker} is a "
          f"simulator, so fills are simulated and no real money moves)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="panadhanam trading intelligence")
    parser.add_argument("--cycle", action="store_true", help="run one cycle and exit")
    parser.add_argument("--premarket", action="store_true", help="run the pre-market scan and exit")
    parser.add_argument("--size", nargs="+", metavar="N",
                        help="CAPITAL RISK_PCT ENTRY STOP [LOT_SIZE]")
    parser.add_argument("--reload", action="store_true", help="auto-reload the server")
    parser.add_argument("--check-data", action="store_true",
                        help="probe every market-data feed and report what is real")
    parser.add_argument("--check-llm", action="store_true",
                        help="check the configured LLM provider is reachable")
    parser.add_argument("--check-broker", action="store_true",
                        help="check the broker connection, account type and data")
    parser.add_argument("--market", metavar="CODE",
                        help="market for --check-data / --cycle (IN or US)")
    # action="extend" matters: with a plain nargs="+" argparse keeps only the
    # LAST --set on the line and silently drops the rest, so
    # `--set A=1 --set B=2` would write B and quietly lose A.
    parser.add_argument("--set", nargs="+", action="extend", metavar="KEY=VALUE",
                        dest="set_env",
                        help="write settings into .env; repeatable "
                             "(e.g. --set TOTAL_CAPITAL=10000 --set LLM_PROVIDER=ollama)")
    parser.add_argument("--ensure-paper-orders", action="store_true",
                        help="turn simulated order placement on for a paper "
                             "account (never touches a real-money broker)")
    args = parser.parse_args()

    if args.ensure_paper_orders:
        raise SystemExit(_ensure_paper_orders())
    if args.set_env:
        raise SystemExit(_set_env(args.set_env))

    if args.check_broker:
        from app.brokers.check import run_check as _broker_check
        raise SystemExit(0 if asyncio.run(_broker_check()) else 1)
    if args.check_llm:
        raise SystemExit(0 if asyncio.run(_check_llm()) else 1)
    if args.check_data:
        from app.data.feeds.check import run_check
        ok = asyncio.run(run_check(args.market))
        raise SystemExit(0 if ok else 1)
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
