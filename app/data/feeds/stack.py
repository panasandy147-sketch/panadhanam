"""Layered market-data feed.

Different sources are good at different things, so the stack asks each in turn
and takes the first real answer:

    India : NSE (official option chain + index spot) → Yahoo (candles, quotes)
    US    : Yahoo (quotes, candles, option chain)

Nothing here fabricates a price. If every source fails, the caller gets None
and the agents correctly abstain — which is the entire point of the
`data_available` contract.
"""
from __future__ import annotations

from typing import Any

from app.brokers.base import BrokerAdapter, OrderResult
from app.core.config import get_config
from app.core.logging import get_logger
from app.core.models import Candle, Instrument, OptionChain, Quote, Side

log = get_logger("feed.stack")


class FeedStack(BrokerAdapter):
    """Tries each feed in order and returns the first genuine result."""

    name = "feeds"
    supports_options = True
    supports_live_orders = False

    def __init__(self, feeds: list[BrokerAdapter]) -> None:
        super().__init__({}, {})
        self.feeds = feeds

    async def connect(self) -> bool:
        live: list[BrokerAdapter] = []
        for feed in self.feeds:
            try:
                if await feed.connect():
                    live.append(feed)
            except Exception as exc:
                log.warning("feed %s failed to connect: %s", feed.name, exc)
        self.feeds = live
        self._connected = bool(live)
        if live:
            log.info("market data: %s", " → ".join(f.name for f in live))
        else:
            log.error("no market-data feed could connect — check your internet "
                      "connection, then restart.")
        return self._connected

    async def disconnect(self) -> None:
        for feed in self.feeds:
            await feed.disconnect()
        self._connected = False

    @property
    def sources(self) -> list[str]:
        return [f.name for f in self.feeds]

    # ------------------------------------------------------------------ #
    async def get_quote(self, symbol: str) -> Quote | None:
        for feed in self.feeds:
            try:
                quote = await feed.get_quote(symbol)
                if quote and quote.last_price > 0:
                    return quote
            except Exception as exc:
                log.debug("%s quote failed for %s: %s", feed.name, symbol, exc)
        return None

    async def get_candles(self, symbol: str, timeframe: str,
                          count: int = 200) -> list[Candle]:
        for feed in self.feeds:
            try:
                candles = await feed.get_candles(symbol, timeframe, count)
                if candles:
                    return candles
            except Exception as exc:
                log.debug("%s candles failed for %s: %s", feed.name, symbol, exc)
        return []

    async def get_expiries(self, underlying: str) -> list[str]:
        for feed in self.feeds:
            try:
                expiries = await feed.get_expiries(underlying)
                if expiries:
                    return expiries
            except Exception:
                continue
        return []

    async def get_option_chain(self, underlying: str,
                               expiry: str | None = None) -> OptionChain | None:
        for feed in self.feeds:
            try:
                chain = await feed.get_option_chain(underlying, expiry)
                if chain and chain.legs:
                    return chain
            except Exception as exc:
                log.debug("%s chain failed for %s: %s", feed.name, underlying, exc)
        return None

    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        return OrderResult(False, message="data feeds cannot place orders")


def describe_data_source(broker: Any) -> dict[str, Any]:
    """Where the prices on screen actually come from.

    Single source of truth, used by the engine status, the opportunity board
    and the replay, so those three can never disagree about whether the user
    is looking at real data.

    A data source may be a FeedStack (several layered feeds) or a single bare
    feed, so fall back to its `.name` rather than assuming `.sources` exists —
    mislabelling a real feed as "paper" is exactly the error that matters here.
    """
    broker_name = getattr(broker, "name", "unknown")
    feed = getattr(broker, "data_source", None)

    sources: list[str] = []
    if feed is not None:
        sources = list(getattr(feed, "sources", None) or [])
        if not sources:
            name = getattr(feed, "name", None)
            if name:
                sources = [name]

    simulated = broker_name == "paper" and not sources
    if not simulated and broker_name != "paper" and not sources:
        # A real broker serving its own data.
        sources = [broker_name]

    return {
        "simulated": simulated,
        "sources": sources,
        "label": ("SIMULATED DATA — prices are generated, not real"
                  if simulated else
                  f"Real market data via {', '.join(sources)}"),
        "execution": ("simulated fills (paper)" if broker_name == "paper"
                      else f"live broker: {broker_name}"),
        "broker": broker_name,
    }


async def build_feed_stack() -> FeedStack | None:
    """Assemble the right feeds for the active market."""
    cfg = get_config()
    if not bool(cfg.get("data.use_real_data", True)):
        log.warning("data.use_real_data is false — running on SIMULATED prices")
        return None

    from app.data.feeds.nse import NSEFeed
    from app.data.feeds.stooq import StooqFeed
    from app.data.feeds.yahoo import YahooFeed

    feeds: list[BrokerAdapter] = []
    if cfg.active_market == "IN" and bool(cfg.get("data.use_nse", True)):
        # NSE first: only the exchange serves genuine OI and IV per strike.
        feeds.append(NSEFeed())
    feeds.append(YahooFeed())
    if bool(cfg.get("data.use_stooq", True)):
        # Last resort for price history when Yahoo is blocked or throttled.
        feeds.append(StooqFeed())

    stack = FeedStack(feeds)
    if await stack.connect():
        return stack
    return None
