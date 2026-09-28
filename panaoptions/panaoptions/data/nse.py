"""India: NSE option chains, and Yahoo charts under Yahoo's names.

    NseChains     the exchange's own option chain (nseindia.com, no key):
                  /api/option-chain-indices?symbol=NIFTY      indices
                  /api/option-chain-equities?symbol=RELIANCE  stocks
                  Real bid/ask, IV, open interest and volume per strike. No
                  greeks, so delta comes from the implied volatility
                  (data/greeks.py), as it does for Yahoo.
    YahooNames    wraps the Yahoo feed so the desk can say NIFTY / RELIANCE
                  while Yahoo is asked for ^NSEI / RELIANCE.NS.

NSE serves its API only to a session that has visited the site first (it
hands out cookies to something that looks like a browser); `_bootstrap`
does that and repeats it when the cookies go stale. The data is public. NSE
rate-limits, so each symbol's chain is cached for 30 seconds. From outside
India, or through a VPN, NSE often refuses outright — the desk then screens
and charts but reports "no contract", and says so on the dashboard.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from panaoptions.data.greeks import delta as bs_delta
from panaoptions.logging import get_logger
from panaoptions.models import OptionContract, OptionRight

log = get_logger("feed.nse")

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://www.nseindia.com"
CHAIN_INDEX = f"{BASE}/api/option-chain-indices"
CHAIN_EQUITY = f"{BASE}/api/option-chain-equities"
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_PAGE_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive", "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
}
_API_HEADERS = {
    "User-Agent": _UA, "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive", "Referer": f"{BASE}/option-chain",
    "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin", "X-Requested-With": "XMLHttpRequest",
}
CACHE_SECONDS = 30
COOKIE_TTL = 240


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _years_to_close(expiry: date, now: datetime) -> float:
    """Days to the 15:30 IST close on expiry day, never quite zero — so a
    same-day contract still gets a delta."""
    close = datetime.combine(expiry, datetime.min.time(), tzinfo=IST) + timedelta(
        hours=15, minutes=30)
    return max((close - now.astimezone(IST)).total_seconds() / 86400.0, 1 / 390)


def parse_chain(payload: dict[str, Any] | None, symbol: str, lot: int,
                now: datetime | None = None) -> list[OptionContract]:
    """Every contract in an NSE option-chain response, every expiry.

    Pure, so it can be tested against a recorded response.
    """
    records = (payload or {}).get("records") or {}
    spot = _num(records.get("underlyingValue"))
    rows = records.get("data") or []
    if spot <= 0 or not isinstance(rows, list):
        return []
    now = now or datetime.now(IST)
    today = now.astimezone(IST).date()
    out: list[OptionContract] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            expiry = datetime.strptime(str(row.get("expiryDate")), "%d-%b-%Y").date()
        except ValueError:
            continue
        strike = _num(row.get("strikePrice"))
        if strike <= 0:
            continue
        for key, right in (("CE", OptionRight.CALL), ("PE", OptionRight.PUT)):
            leg = row.get(key)
            if not isinstance(leg, dict):
                continue
            iv = _num(leg.get("impliedVolatility")) / 100.0
            days = _years_to_close(expiry, now)
            out.append(OptionContract(
                symbol=symbol, right=right, strike=strike,
                expiry=expiry.isoformat(), dte=(expiry - today).days,
                bid=_num(leg.get("bidprice")), ask=_num(leg.get("askPrice")),
                delta=bs_delta(spot, strike, days, iv, right is OptionRight.CALL,
                               risk_free_rate=0.065),
                implied_volatility=iv,
                open_interest=int(_num(leg.get("openInterest"))),
                volume=int(_num(leg.get("totalTradedVolume"))),
                multiplier=int(lot)))
    return out


class NseChains:
    """NSE option chains, in the shape HybridFeed expects of a chain source."""

    name = "nse"
    delayed = False
    greeks = False          # delta is estimated from IV

    def __init__(self, lot_sizes: dict[str, int] | None = None,
                 default_lot: int = 1, timeout: float = 20.0) -> None:
        self.lot_sizes = {k.upper(): int(v) for k, v in (lot_sizes or {}).items()}
        self.default_lot = default_lot
        self.timeout = timeout
        self.options_error = ""
        self._client: httpx.AsyncClient | None = None
        self._cookies_at = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._client = httpx.AsyncClient(headers=_API_HEADERS, timeout=self.timeout,
                                         follow_redirects=True)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _bootstrap(self) -> bool:
        if not self._client:
            return False
        try:
            r = await self._client.get(BASE, headers=_PAGE_HEADERS)
            if r.status_code != 200:
                self.options_error = f"nseindia.com answered HTTP {r.status_code}"
                return False
            await self._client.get(f"{BASE}/option-chain", headers=_PAGE_HEADERS)
            self._cookies_at = time.time()
            return True
        except Exception as exc:                        # noqa: BLE001
            self.options_error = f"nseindia.com unreachable: {type(exc).__name__}"
            return False

    async def _payload(self, symbol: str) -> Any | None:
        if not self._client:
            await self.open()
        sym = symbol.upper()
        hit = self._cache.get(sym)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        url = CHAIN_INDEX if sym in INDEX_SYMBOLS else CHAIN_EQUITY
        async with self._lock:
            if time.time() - self._cookies_at > COOKIE_TTL:
                await self._bootstrap()
            try:
                r = await self._client.get(url, params={"symbol": sym})
                if r.status_code in (401, 403):
                    await self._bootstrap()
                    r = await self._client.get(url, params={"symbol": sym})
                if r.status_code != 200:
                    self.options_error = f"NSE option chain for {sym}: HTTP {r.status_code}"
                    return None
                data = r.json()
            except Exception as exc:                    # noqa: BLE001
                self.options_error = f"NSE option chain for {sym}: {type(exc).__name__}"
                return None
        self._cache[sym] = (time.time(), data)
        return data

    async def probe(self) -> bool:
        self.options_error = ""
        if not await self._bootstrap():
            return False
        data = await self._payload("NIFTY")
        ok = bool(((data or {}).get("records") or {}).get("data"))
        if not ok and not self.options_error:
            self.options_error = ("NSE answered without a chain — often a non-Indian "
                                  "IP or a VPN")
        return ok

    def lot(self, symbol: str) -> int:
        return self.lot_sizes.get(symbol.upper(), self.default_lot)

    async def all_contracts(self, symbol: str) -> list[OptionContract]:
        return parse_chain(await self._payload(symbol), symbol, self.lot(symbol))

    async def expiries(self, symbol: str) -> list[int]:
        out = sorted({int(datetime.combine(date.fromisoformat(c.expiry),
                                           datetime.min.time(), tzinfo=IST).timestamp())
                      for c in await self.all_contracts(symbol)})
        return out

    async def option_chain(self, symbol: str, expiry_epoch: int, spot: float):
        want = datetime.fromtimestamp(expiry_epoch, tz=IST).date().isoformat()
        return [c for c in await self.all_contracts(symbol) if c.expiry == want]

    async def chain_for_window(self, symbol: str, spot: float,
                               min_dte: int, max_dte: int) -> list[OptionContract]:
        self.options_error = ""
        contracts = await self.all_contracts(symbol)
        if not contracts:
            self.options_error = self.options_error or f"NSE returned no chain for {symbol}"
            return []
        inside = [c for c in contracts if min_dte <= c.dte <= max_dte]
        if not inside:
            listed = sorted({c.dte for c in contracts})[:6]
            self.options_error = (f"no {symbol} expiry between {min_dte} and {max_dte} "
                                  f"days; listed: {', '.join(map(str, listed))} days")
        return inside


class YahooNames:
    """The Yahoo chart feed, asked for India's symbols by Yahoo's names."""

    def __init__(self, inner: Any, mapping: dict[str, str] | None = None,
                 suffix: str = ".NS") -> None:
        self.inner = inner
        self.mapping = {k.upper(): v for k, v in (mapping or {}).items()}
        self.suffix = suffix

    def name_for(self, symbol: str) -> str:
        sym = symbol.upper()
        if sym in self.mapping:
            return self.mapping[sym]
        if sym.startswith("^") or "." in sym or "=" in sym:
            return sym
        return f"{sym}{self.suffix}"

    def __getattr__(self, item: str) -> Any:
        return getattr(self.inner, item)

    async def connect(self) -> bool:
        return await self.inner.connect()

    async def close(self) -> None:
        await self.inner.close()

    async def candles(self, symbol: str, interval: str = "5m",
                      include_prepost: bool = False):
        return await self.inner.candles(self.name_for(symbol), interval, include_prepost)

    async def quote(self, symbol: str):
        q = await self.inner.quote(self.name_for(symbol))
        if isinstance(q, dict):
            q = {**q, "symbol": symbol}
        return q

    async def news(self, symbol: str, count: int = 10):
        source = getattr(self.inner, "news", None)
        return await source(self.name_for(symbol), count) if source else []

    async def futures_change(self, symbol: str):
        source = getattr(self.inner, "futures_change", None)
        return await source(symbol) if source else None


def make_india_feed(cfg: Any) -> Any:
    """Yahoo charts under NSE names + NSE chains, as one feed."""
    from panaoptions.data.feed import YahooFeed
    from panaoptions.data.hybrid import HybridFeed

    charts = YahooNames(YahooFeed(), cfg.get("data.yahoo_symbols") or {},
                        str(cfg.get("data.yahoo_suffix", ".NS")))
    chains = NseChains(cfg.get("data.lot_sizes") or {}, default_lot=1)
    log.info("market data (India): Yahoo charts + NSE option chains "
             "(delta estimated from IV)")
    return HybridFeed(charts=charts, chains=chains, chains_name="NSE")
