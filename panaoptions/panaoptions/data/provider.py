"""Which market data feed the desk uses.

The desk, the screener and the strategies only ever hold "a feed" — an object
with connect, quote, candles, expiries, option_chain and chain_for_window. So
swapping providers is a configuration choice rather than a code change, and
neither the rules nor the journal can tell which one is underneath.

    data:
      provider: yahoo | tradier
      tradier_env: sandbox | production

and `TRADIER_TOKEN` in panaoptions/.env for Tradier.
"""
from __future__ import annotations

import os
from typing import Any

from panaoptions.logging import get_logger

log = get_logger("provider")

PROVIDERS = ("yahoo", "cboe", "tradier")

# What each one is, in one line, for --check and the dashboard.
NOTES = {
    "yahoo": ("Yahoo serves no greeks, so delta is estimated with "
              "Black-Scholes from implied volatility"),
    "cboe": ("charts from Yahoo, option chains from CBOE's public delayed "
             "feed — no account, greeks included, about 15 minutes late"),
    "tradier": "greeks come from the exchange",
}


def describe(cfg) -> dict[str, Any]:
    """What the desk will use, and whether it can. Never makes a request."""
    name = str(cfg.get("data.provider", "yahoo")).strip().lower()
    known = name in PROVIDERS
    ready, note = True, ""

    if not known:
        ready = False
        note = f"unknown provider {name!r} — pick one of {', '.join(PROVIDERS)}"
    elif name == "tradier":
        if not os.getenv("TRADIER_TOKEN"):
            ready = False
            note = ("TRADIER_TOKEN is not set. Put it in panaoptions/.env — "
                    "note that Tradier's signup opens a US brokerage account "
                    "and asks for SSN and phone. `cboe` needs no account.")
        else:
            note = (f"{cfg.get('data.tradier_env', 'sandbox')} environment, "
                    f"{NOTES['tradier']}")
    else:
        note = NOTES[name]

    return {"provider": name if known else "yahoo", "configured": name,
            "ready": ready, "note": note}


def make_feed(cfg) -> Any:
    """Build the configured feed.

    An unknown name falls back to Yahoo WITH A WARNING rather than raising:
    a typo in one config key should not stop a paper desk from starting, but
    it must never be silent either, because a desk quietly running on a
    different data source than intended is the worst of both.
    """
    name = str(cfg.get("data.provider", "yahoo")).strip().lower()

    if name == "tradier":
        from panaoptions.data.tradier import TradierFeed

        token = os.getenv("TRADIER_TOKEN", "")
        env = str(cfg.get("data.tradier_env", "sandbox"))
        if not token:
            log.error("data.provider is 'tradier' but TRADIER_TOKEN is not "
                      "set — falling back to Yahoo, whose option chains may "
                      "refuse every request. Put the token in "
                      "panaoptions/.env and restart.")
        else:
            log.info("market data: Tradier (%s), greeks from the exchange", env)
            return TradierFeed(token=token, environment=env)
    elif name == "cboe":
        from panaoptions.data.cboe import CboeChains
        from panaoptions.data.feed import YahooFeed
        from panaoptions.data.hybrid import HybridFeed

        log.info("market data: Yahoo charts + CBOE chains (no account, "
                 "greeks included, delayed)")
        return HybridFeed(charts=YahooFeed(), chains=CboeChains(),
                          chains_name="CBOE")
    elif name not in PROVIDERS:
        log.warning("unknown data.provider %r — using Yahoo. Valid: %s",
                    name, ", ".join(PROVIDERS))

    from panaoptions.data.feed import YahooFeed

    log.info("market data: Yahoo (delta estimated from implied volatility)")
    return YahooFeed()
