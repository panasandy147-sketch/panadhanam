"""Data-feed self-check.

Answers one question with evidence: "am I looking at real market data?"

Probes every feed the active market uses, fetches a real quote and candle for
a known symbol, and prints what came back. Run it whenever you are unsure —
especially before acting on anything the dashboard shows.

    python run.py --check-data
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_config
from app.core.logging import get_logger

log = get_logger("feed.check")

# A liquid, always-present symbol per market to probe with.
PROBE = {"IN": ["NIFTY 50", "RELIANCE"], "US": ["SPY", "AAPL"]}


async def check_feeds(market: str | None = None) -> dict[str, Any]:
    cfg = get_config()
    if market and market.upper() != cfg.active_market:
        cfg.switch_market(market)

    report: dict[str, Any] = {
        "market": cfg.active_market,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "feeds": [],
        "verdict": "unknown",
    }

    from app.data.feeds.nse import NSEFeed
    from app.data.feeds.yahoo import YahooFeed

    candidates = []
    if cfg.active_market == "IN":
        candidates.append(("nse", NSEFeed(), "NSE India (official exchange API)"))
    candidates.append(("yahoo", YahooFeed(), "Yahoo Finance"))

    symbols = PROBE.get(cfg.active_market, ["SPY"])
    any_real = False

    for name, feed, label in candidates:
        entry: dict[str, Any] = {"name": name, "label": label,
                                 "connected": False, "samples": [], "errors": []}
        try:
            entry["connected"] = await feed.connect()
        except Exception as exc:
            entry["errors"].append(f"connect: {exc}")

        if entry["connected"]:
            for symbol in symbols:
                sample: dict[str, Any] = {"symbol": symbol}
                try:
                    quote = await feed.get_quote(symbol)
                    if quote and quote.last_price > 0:
                        sample["last_price"] = round(quote.last_price, 2)
                        sample["change_pct"] = quote.change_pct
                        sample["as_of"] = quote.ts.isoformat()
                        any_real = True
                    else:
                        sample["last_price"] = None
                except Exception as exc:
                    entry["errors"].append(f"{symbol} quote: {exc}")

                try:
                    candles = await feed.get_candles(symbol, "5m", 20)
                    sample["candles"] = len(candles)
                    if candles:
                        sample["latest_candle"] = {
                            "ts": candles[-1].ts.isoformat(),
                            "close": candles[-1].close,
                            "volume": candles[-1].volume,
                        }
                except Exception as exc:
                    entry["errors"].append(f"{symbol} candles: {exc}")

                try:
                    chain = await feed.get_option_chain(symbol)
                    if chain:
                        sample["option_legs"] = len(chain.legs)
                        sample["expiry"] = chain.expiry
                        # Real OI change is only available from the exchange.
                        sample["has_oi_change"] = any(
                            leg.oi_change != 0 for leg in chain.legs)
                except Exception:
                    pass

                entry["samples"].append(sample)

        await feed.disconnect()
        report["feeds"].append(entry)

    report["verdict"] = "REAL" if any_real else "NO REAL DATA"
    report["any_real"] = any_real
    return report


def print_report(report: dict[str, Any]) -> None:
    cfg = get_config()
    cur = cfg.market.currency_symbol
    bar = "=" * 72

    print(f"\n{bar}")
    print(f"  DATA FEED CHECK — {report['market']} market")
    print(bar)

    for feed in report["feeds"]:
        status = "CONNECTED" if feed["connected"] else "UNREACHABLE"
        mark = "OK " if feed["connected"] else "XX "
        print(f"\n  [{mark}] {feed['label']}: {status}")

        for sample in feed["samples"]:
            price = sample.get("last_price")
            if price is None:
                print(f"        {sample['symbol']:<12} no quote returned")
                continue
            change = sample.get("change_pct", 0.0)
            print(f"        {sample['symbol']:<12} {cur}{price:>12,.2f}  "
                  f"({change:+.2f}%)   as of {sample.get('as_of', '?')[:16]}")
            if sample.get("candles"):
                latest = sample.get("latest_candle", {})
                print(f"        {'':<12} {sample['candles']} candles, "
                      f"latest close {cur}{latest.get('close', 0):,.2f} "
                      f"at {latest.get('ts', '?')[:16]}")
            if sample.get("option_legs"):
                oi = "with real OI change" if sample.get("has_oi_change") \
                     else "no OI change (Yahoo does not publish it)"
                print(f"        {'':<12} option chain: {sample['option_legs']} legs, "
                      f"expiry {sample.get('expiry')}, {oi}")

        for err in feed["errors"][:3]:
            print(f"        ! {err}")

    print(f"\n{bar}")
    if report["any_real"]:
        print("  VERDICT: REAL MARKET DATA IS AVAILABLE.")
        print("  The dashboard will show genuine prices. When the market is")
        print("  closed these are last-traded values, which is correct.")
    else:
        print("  VERDICT: NO REAL DATA.")
        print("  Every feed failed, so the dashboard falls back to a SYNTHETIC")
        print("  market. Those prices refer to nothing real — do not trade on")
        print("  them. Check your internet connection, VPN, or firewall.")
        print("  Neither feed needs an API key, so this is a network issue.")
    print(f"{bar}\n")


async def run_check(market: str | None = None) -> bool:
    report = await check_feeds(market)
    print_report(report)
    return bool(report["any_real"])
