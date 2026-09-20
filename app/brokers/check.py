"""Broker self-check.

Answers "is my broker actually connected, and is it pointed at real money?"
with evidence rather than a config echo.

    python run.py --check-broker
"""
from __future__ import annotations

from typing import Any

from app.core.config import get_config
from app.core.logging import get_logger

log = get_logger("broker.check")


async def check_broker() -> dict[str, Any]:
    from app.brokers.factory import build_broker

    cfg = get_config()
    report: dict[str, Any] = {"market": cfg.active_market,
                             "configured": cfg.broker_name}

    broker = await build_broker(cfg)
    report["connected_as"] = broker.name
    report["fell_back"] = broker.name != cfg.broker_name
    report["health"] = broker.health()
    report["is_paper_account"] = getattr(broker, "is_paper_account", True)

    try:
        report["funds"] = await broker.get_funds()
    except Exception as exc:
        report["funds"] = {"error": str(exc)}

    symbols = [w["symbol"] for w in cfg.watchlist()][:2]
    samples = []
    for symbol in symbols:
        sample: dict[str, Any] = {"symbol": symbol}
        try:
            quote = await broker.get_quote(symbol)
            sample["last_price"] = round(quote.last_price, 2) if quote else None
        except Exception as exc:
            sample["error"] = str(exc)
        try:
            candles = await broker.get_candles(symbol, "5m", 20)
            sample["candles"] = len(candles)
        except Exception:
            sample["candles"] = 0
        try:
            chain = await broker.get_option_chain(symbol)
            sample["option_legs"] = len(chain.legs) if chain else 0
        except Exception:
            sample["option_legs"] = 0
        samples.append(sample)
    report["samples"] = samples

    try:
        positions = await broker.get_positions()
        report["open_positions"] = len(positions)
    except Exception:
        report["open_positions"] = 0

    report["ok"] = bool(broker.connected and any(
        s.get("last_price") for s in samples))
    await broker.disconnect()
    return report


def print_report(report: dict[str, Any]) -> None:
    cfg = get_config()
    cur = cfg.market.currency_symbol
    bar = "=" * 72
    print(f"\n{bar}\n  BROKER CHECK — {report['market']} market\n{bar}")

    print(f"\n  Configured : {report['configured']}")
    print(f"  Connected  : {report['connected_as']}")
    if report["fell_back"]:
        print("               ^ FELL BACK — the configured broker could not "
              "authenticate.")
        print("                 Check your .env credentials. No orders will "
              "reach it.")

    paper = report["is_paper_account"]
    print(f"\n  Account    : {'SIMULATOR (paper)' if paper else '*** REAL MONEY ***'}")
    if not paper:
        print("               Orders from this desk can move real money.")

    funds = report.get("funds") or {}
    if funds and "error" not in funds:
        for key, value in funds.items():
            try:
                print(f"  {key:<11}: {cur}{float(value):,.2f}")
            except (TypeError, ValueError):
                print(f"  {key:<11}: {value}")

    print("\n  Data:")
    for s in report.get("samples", []):
        price = s.get("last_price")
        if price is None:
            print(f"    {s['symbol']:<12} no quote  {s.get('error', '')[:50]}")
            continue
        print(f"    {s['symbol']:<12} {cur}{price:>10,.2f}   "
              f"{s.get('candles', 0)} candles, "
              f"{s.get('option_legs', 0)} option legs")

    print(f"\n  Open positions at the broker: {report.get('open_positions', 0)}")

    print(f"\n{bar}")
    if report["ok"]:
        mode = "practice safely" if paper else "TRADE REAL MONEY"
        print(f"  VERDICT: {report['connected_as']} is connected and serving data.")
        print(f"           You can {mode} with this account.")
    else:
        print("  VERDICT: NOT USABLE. See the messages above.")
    print(f"{bar}\n")


async def run_check() -> bool:
    report = await check_broker()
    print_report(report)
    return bool(report["ok"])
