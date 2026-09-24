"""Charts from one source, option chains from another.

Nothing says the two have to come from the same place, and right now they
cannot: Yahoo's charts are reliable and its chain endpoint returns 401, while
CBOE publishes chains with greeks and no intraday bars at all. Each half of
the problem has a good free answer; they are just different halves.

The desk holds this and cannot tell it apart from a single feed.
"""
from __future__ import annotations

from typing import Any

from panaoptions.logging import get_logger

log = get_logger("hybrid")


class HybridFeed:
    """`charts` supplies prices and bars; `chains` supplies options."""

    def __init__(self, charts: Any, chains: Any, chains_name: str = "chains") -> None:
        self.charts = charts
        self.chains = chains
        self.chains_name = chains_name
        self.connected = False
        self.options_available: bool | None = None

    # -- the two error fields the dashboard reads ----------------------- #
    @property
    def options_error(self) -> str:
        return getattr(self.chains, "options_error", "")

    @options_error.setter
    def options_error(self, value: str) -> None:
        # The desk clears this before a lookup; forward it rather than
        # shadowing the chain source's own field, or the reason a chain came
        # back empty would be written to an attribute nobody reads.
        if hasattr(self.chains, "options_error"):
            self.chains.options_error = value

    @property
    def chart_error(self) -> str:
        return getattr(self.charts, "chart_error", "")

    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> HybridFeed:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> bool:
        # Charts first: without prices there is nothing to judge, so that
        # failure is fatal in a way a missing chain is not.
        if not await self.charts.connect():
            return False
        self.connected = True

        await self.chains.open()
        self.options_available = await self.chains.probe()
        if self.options_available:
            log.info("option chains from %s — greeks included, delayed",
                     self.chains_name)
        else:
            log.error("%s is not answering (%s). Charts work, so the desk "
                      "will screen and fire setups and every one will report "
                      "'no contract'.", self.chains_name,
                      self.options_error or "empty response")
        return True

    async def close(self) -> None:
        await self.charts.close()
        await self.chains.close()
        self.connected = False

    # -- charts --------------------------------------------------------- #
    async def quote(self, symbol: str):
        return await self.charts.quote(symbol)

    async def candles(self, symbol: str, interval: str = "5m",
                      include_prepost: bool = False):
        return await self.charts.candles(symbol, interval, include_prepost)

    # -- chains --------------------------------------------------------- #
    async def expiries(self, symbol: str):
        return await self.chains.expiries(symbol)

    async def option_chain(self, symbol: str, expiry_epoch: int, spot: float):
        return await self.chains.option_chain(symbol, expiry_epoch, spot)

    async def chain_for_window(self, symbol: str, spot: float,
                               min_dte: int, max_dte: int):
        return await self.chains.chain_for_window(symbol, spot, min_dte, max_dte)
