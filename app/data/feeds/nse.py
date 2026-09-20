"""NSE India official data feed — nseindia.com, no API key.

This is the exchange's own endpoint, so the option chain here is genuine:
real Open Interest, real IV, real strike-by-strike data. It is what powers a
trustworthy PCR, Max Pain and OI-buildup read for Indian markets.

    /api/option-chain-indices?symbol=NIFTY     indices
    /api/option-chain-equities?symbol=RELIANCE stocks
    /api/quote-equity?symbol=RELIANCE          last traded price

NSE blocks plain programmatic requests. A browser-like session that first
visits the site to collect cookies is required — `_bootstrap()` does that and
re-runs whenever the cookies go stale. This is normal for NSE; it is not a
workaround for any access control, the data is public.

Be considerate: NSE rate-limits. Responses are cached briefly and the whole
watchlist is not hammered every cycle.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import httpx

from app.brokers.base import BrokerAdapter, OrderResult
from app.core.logging import get_logger
from app.core.models import (Candle, Instrument, OptionChain, OptionLeg, Quote,
                             Side)

log = get_logger("feed.nse")

BASE = "https://www.nseindia.com"
CHAIN_INDEX = f"{BASE}/api/option-chain-indices"
CHAIN_EQUITY = f"{BASE}/api/option-chain-equities"
QUOTE_EQUITY = f"{BASE}/api/quote-equity"

INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
_ALIASES = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY",
            "NIFTY FIN SERVICE": "FINNIFTY"}

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/option-chain",
}

CACHE_SECONDS = 30
COOKIE_TTL = 240


def nse_symbol(symbol: str) -> str:
    key = symbol.strip().upper()
    return _ALIASES.get(key, key)


class NSEFeed(BrokerAdapter):
    """Read-only official NSE data. Cannot place orders."""

    name = "nse"
    supports_options = True
    supports_live_orders = False

    def __init__(self, credentials: dict[str, str] | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, config)
        self._client: httpx.AsyncClient | None = None
        self._cookies_at: float = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        self._client = httpx.AsyncClient(headers=_HEADERS, timeout=20.0,
                                         follow_redirects=True)
        ok = await self._bootstrap()
        if ok:
            self._connected = True
            log.info("NSE India feed connected — official exchange data")
        else:
            log.error("NSE India unreachable. It blocks some networks and "
                      "non-Indian IPs; the Yahoo feed is the fallback.")
        return ok

    async def _bootstrap(self) -> bool:
        """Collect the session cookies NSE requires before serving its API."""
        if not self._client:
            return False
        try:
            r = await self._client.get(BASE)
            if r.status_code != 200:
                return False
            # Visiting the option-chain page sets the cookie the API checks for.
            await self._client.get(f"{BASE}/option-chain")
            self._cookies_at = time.time()
            return True
        except Exception as exc:
            log.debug("nse bootstrap failed: %s", exc)
            return False

    async def _get(self, url: str, params: dict[str, Any]) -> Any | None:
        if not self._client:
            return None
        key = f"{url}?{sorted(params.items())}"
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]

        async with self._lock:
            if time.time() - self._cookies_at > COOKIE_TTL:
                await self._bootstrap()
            try:
                r = await self._client.get(url, params=params)
                if r.status_code in (401, 403):
                    # Cookies expired mid-flight — refresh once and retry.
                    await self._bootstrap()
                    r = await self._client.get(url, params=params)
                if r.status_code != 200:
                    log.debug("nse %s → HTTP %s", url, r.status_code)
                    return None
                data = r.json()
                self._cache[key] = (time.time(), data)
                return data
            except Exception as exc:
                log.debug("nse request failed %s: %s", url, exc)
                return None

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
        self._connected = False

    # ------------------------------------------------------------------ #
    async def get_quote(self, symbol: str) -> Quote | None:
        sym = nse_symbol(symbol)
        if sym in INDEX_SYMBOLS:
            # The chain response carries the index spot, so reuse it.
            data = await self._get(CHAIN_INDEX, {"symbol": sym})
            spot = ((data or {}).get("records") or {}).get("underlyingValue")
            if not spot:
                return None
            return Quote(symbol=symbol, last_price=float(spot))

        data = await self._get(QUOTE_EQUITY, {"symbol": sym})
        info = (data or {}).get("priceInfo") or {}
        last = info.get("lastPrice")
        if last is None:
            return None
        return Quote(
            symbol=symbol, last_price=float(last),
            change_pct=float(info.get("pChange") or 0.0),
            volume=float(((data or {}).get("securityWiseDP") or {})
                         .get("quantityTraded") or 0),
        )

    async def get_candles(self, symbol: str, timeframe: str,
                          count: int = 200) -> list[Candle]:
        """NSE's public API has no clean OHLCV history endpoint — Yahoo serves
        candles. Returning empty here lets the feed stack fall through."""
        return []

    # ------------------------------------------------------------------ #
    async def get_expiries(self, underlying: str) -> list[str]:
        data = await self._fetch_chain(underlying)
        raw = ((data or {}).get("records") or {}).get("expiryDates") or []
        out = []
        for value in raw[:6]:
            try:
                out.append(datetime.strptime(value, "%d-%b-%Y").date().isoformat())
            except ValueError:
                continue
        return out

    async def _fetch_chain(self, underlying: str) -> Any | None:
        sym = nse_symbol(underlying)
        url = CHAIN_INDEX if sym in INDEX_SYMBOLS else CHAIN_EQUITY
        return await self._get(url, {"symbol": sym})

    async def get_option_chain(self, underlying: str,
                               expiry: str | None = None) -> OptionChain | None:
        data = await self._fetch_chain(underlying)
        if not data:
            return None
        return self.parse_chain(underlying, data, expiry)

    @staticmethod
    def parse_chain(underlying: str, data: dict[str, Any],
                    expiry: str | None = None) -> OptionChain | None:
        """Turn NSE's payload into an OptionChain.

        Kept static and pure so it can be unit-tested against a recorded
        response without touching the network.
        """
        records = data.get("records") or {}
        spot = records.get("underlyingValue")
        rows = records.get("data") or []
        if not spot or not rows:
            return None

        expiries = records.get("expiryDates") or []
        if expiry:
            try:
                want = datetime.strptime(
                    expiry, "%Y-%m-%d").strftime("%d-%b-%Y")
            except ValueError:
                want = expiries[0] if expiries else None
        else:
            want = expiries[0] if expiries else None
        if not want:
            return None

        legs: list[OptionLeg] = []
        for row in rows:
            if row.get("expiryDate") != want:
                continue
            strike = row.get("strikePrice")
            if strike is None:
                continue
            for key, opt_type in (("CE", "CE"), ("PE", "PE")):
                leg = row.get(key)
                if not leg:
                    continue
                legs.append(OptionLeg(
                    strike=float(strike),
                    option_type=opt_type,
                    ltp=float(leg.get("lastPrice") or 0.0),
                    oi=float(leg.get("openInterest") or 0.0),
                    # NSE gives the OI change directly — this is what makes a
                    # genuine long-buildup / short-covering read possible.
                    oi_change=float(leg.get("changeinOpenInterest") or 0.0),
                    volume=float(leg.get("totalTradedVolume") or 0.0),
                    iv=float(leg.get("impliedVolatility") or 0.0) / 100.0,
                ))
        if not legs:
            return None

        try:
            iso_expiry = datetime.strptime(want, "%d-%b-%Y").date().isoformat()
        except ValueError:
            iso_expiry = want

        return OptionChain(underlying=underlying, spot=float(spot),
                           expiry=iso_expiry, legs=legs)

    # ------------------------------------------------------------------ #
    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        return OrderResult(
            False,
            message=("NSE India is the exchange's public data feed and cannot "
                     "place orders. Connect a broker to trade."))
