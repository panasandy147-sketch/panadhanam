"""Pick a working option-chain source at startup, in order, automatically.

Which chain endpoint answers is a property of the machine and of the week:
Yahoo's returns 401 for some people and works for others, and it changes
without notice. Making somebody read a log, understand the difference
between two Yahoo hosts and then run a command to switch is a design
failure — the desk can find out in one request.

The order is preference, not availability: the configured source is tried
first, and the fallbacks only get a turn when it does not answer. Nothing is
chosen silently. The choice is logged at startup, written to the activity
log, and shown on the dashboard next to whether its delta is quoted or
estimated, because delayed data from a fallback is a different thing from
real-time data from the source you picked.
"""
from __future__ import annotations

from typing import Any

from panaoptions.logging import get_logger
from panaoptions.models import OptionContract

log = get_logger("chains.auto")


class YahooChains:
    """Just the chain half of a YahooFeed, sharing its client.

    Opening a second HTTP client to the same host to ask the same questions
    would double the request count for nothing.
    """

    name = "yahoo"
    delayed = False
    greeks = False

    def __init__(self, feed: Any) -> None:
        self.feed = feed

    @property
    def options_error(self) -> str:
        return getattr(self.feed, "options_error", "")

    @options_error.setter
    def options_error(self, value: str) -> None:
        self.feed.options_error = value

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def probe(self) -> bool:
        return bool(await self.feed.expiries("SPY"))

    async def expiries(self, symbol: str) -> list[int]:
        return await self.feed.expiries(symbol)

    async def option_chain(self, symbol: str, expiry_epoch: int, spot: float):
        return await self.feed.option_chain(symbol, expiry_epoch, spot)

    async def chain_for_window(self, symbol: str, spot: float,
                               min_dte: int, max_dte: int):
        return await self.feed.chain_for_window(symbol, spot, min_dte, max_dte)


class AutoChains:
    """Try each source in order; keep the first that answers."""

    def __init__(self, candidates: list[Any]) -> None:
        self.candidates = candidates
        self.active: Any | None = None
        self.chosen: str = ""
        self.attempts: list[tuple[str, str]] = []   # (name, why it was skipped)
        self.options_error: str = ""

    @property
    def delayed(self) -> bool:
        return bool(getattr(self.active, "delayed", False))

    @property
    def greeks(self) -> bool:
        return bool(getattr(self.active, "greeks", False))

    async def open(self) -> None:
        for source in self.candidates:
            await source.open()

    async def close(self) -> None:
        for source in self.candidates:
            await source.close()

    async def probe(self) -> bool:
        """The one request per source that decides it."""
        self.attempts = []
        for source in self.candidates:
            name = getattr(source, "name", source.__class__.__name__)
            try:
                ok = await source.probe()
            except Exception as exc:                 # noqa: BLE001
                self.attempts.append((name, f"{type(exc).__name__}: {exc}"))
                continue
            if ok:
                self.active = source
                self.chosen = name
                skipped = ", ".join(f"{n} ({why})" for n, why in self.attempts)
                if skipped:
                    log.warning("option chains: %s did not answer — using %s "
                                "instead", skipped, name)
                log.info("option chains: %s%s%s", name,
                         " (delayed)" if self.delayed else "",
                         ", greeks included" if self.greeks
                         else ", delta estimated here")
                return True
            self.attempts.append(
                (name, getattr(source, "options_error", "") or "no answer"))

        self.options_error = "; ".join(f"{n}: {why}" for n, why in self.attempts)
        self.active = None
        self.chosen = ""
        log.error("no option chain source answered — %s", self.options_error)
        return False

    # -- delegate, once one has been chosen ----------------------------- #
    def _refuse(self) -> list[OptionContract]:
        self.options_error = (self.options_error
                              or "no option chain source is available")
        return []

    async def expiries(self, symbol: str) -> list[int]:
        if self.active is None:
            self._refuse()
            return []
        return await self.active.expiries(symbol)

    async def option_chain(self, symbol: str, expiry_epoch: int, spot: float):
        if self.active is None:
            return self._refuse()
        return await self.active.option_chain(symbol, expiry_epoch, spot)

    async def chain_for_window(self, symbol: str, spot: float,
                               min_dte: int, max_dte: int):
        if self.active is None:
            return self._refuse()
        self.active.options_error = ""
        out = await self.active.chain_for_window(symbol, spot, min_dte, max_dte)
        self.options_error = getattr(self.active, "options_error", "")
        return out
