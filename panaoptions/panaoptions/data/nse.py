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


# --------------------------------------------------------------------------- #
# The estimated-price fallback: no account, no KYC
# --------------------------------------------------------------------------- #
def expiries(symbol: str, today: date, weekly: set[str], weekday: int = 1,
             horizon_days: int = 40) -> list[date]:
    """The NSE expiry calendar, approximately.

    Weekly symbols (NIFTY) expire every `weekday` (Tuesday = 1); everything
    else on the LAST `weekday` of the month. Exchange holidays are not
    modelled — on a holiday week the real expiry moves a day earlier.
    """
    out: set[date] = set()
    end = today + timedelta(days=horizon_days)
    d = today
    while d <= end:
        if d.weekday() == weekday:
            last_of_month = (d + timedelta(days=7)).month != d.month
            if symbol.upper() in weekly or last_of_month:
                out.add(d)
        d += timedelta(days=1)
    return sorted(out)


def strike_step(symbol: str, spot: float, steps: dict[str, float]) -> float:
    if symbol.upper() in steps:
        return float(steps[symbol.upper()])
    for limit, step in ((250, 2.5), (500, 5), (1000, 10), (2500, 20), (5000, 50)):
        if spot < limit:
            return step
    return 100.0


def _tick(value: float, tick: float = 0.05) -> float:
    return max(round(round(value / tick) * tick, 2), tick)


def estimate_chain(symbol: str, spot: float, iv: float, lot: int, now: datetime,
                   *, weekly: set[str], weekday: int = 1, steps: dict[str, float] | None = None,
                   strikes_each_side: int = 25, spread_pct: float = 2.0,
                   min_dte: int = 0, max_dte: int = 40) -> list[OptionContract]:
    """A Black-Scholes chain around `spot`: every listed-style strike and
    expiry in the window, calls and puts, with a quoted spread.

    Every contract is marked `estimated` — these are model prices, not quotes.
    """
    from panaoptions.data.greeks import price as bs_price

    if spot <= 0 or iv <= 0:
        return []
    local = now.astimezone(IST)
    today = local.date()
    step = strike_step(symbol, spot, steps or {})
    atm = round(spot / step) * step
    out: list[OptionContract] = []
    for expiry in expiries(symbol, today, weekly, weekday, max_dte + 7):
        dte = (expiry - today).days
        if not min_dte <= dte <= max_dte:
            continue
        if dte == 0 and (local.hour, local.minute) >= (15, 30):
            continue                                    # expired at the close
        days = _years_to_close(expiry, now)
        for k in range(-strikes_each_side, strikes_each_side + 1):
            strike = round(atm + k * step, 2)
            if strike <= 0:
                continue
            for right in (OptionRight.CALL, OptionRight.PUT):
                call = right is OptionRight.CALL
                mid = bs_price(spot, strike, days, iv, call, risk_free_rate=0.065)
                if mid < 0.05:
                    continue
                half = max(mid * spread_pct / 200.0, 0.05)
                out.append(OptionContract(
                    symbol=symbol, right=right, strike=strike,
                    expiry=expiry.isoformat(), dte=dte,
                    bid=_tick(mid - half), ask=_tick(mid + half),
                    delta=bs_delta(spot, strike, days, iv, call, risk_free_rate=0.065),
                    implied_volatility=round(iv, 4), multiplier=int(lot),
                    estimated=True))
    return out


class EstimatedChains:
    """Model-priced chains from Yahoo's spot and India VIX / realised volatility.

    Used only when NSE refuses. No open interest or volume exists here, so
    the options-flow reads stay silent rather than inventing a signal.
    """

    name = "estimated"
    delayed = False
    greeks = False

    def __init__(self, charts: Any, cfg: Any) -> None:
        g = cfg.get
        self.charts = charts
        self.lots = {k.upper(): int(v) for k, v in (g("data.lot_sizes") or {}).items()}
        est = g("data.estimated") or {}
        self.weekly = {s.upper() for s in est.get("weekly_symbols", ["NIFTY"])}
        self.weekday = int(est.get("expiry_weekday", 1))
        self.steps = {k.upper(): float(v) for k, v in (est.get("strike_steps") or {}).items()}
        self.vix_symbol = str(est.get("vix_symbol", "^INDIAVIX"))
        self.index_iv = {k.upper(): float(v) for k, v in
                         (est.get("index_iv_multiplier") or {"NIFTY": 1.0}).items()}
        self.stock_premium = float(est.get("stock_iv_premium", 1.10))
        self.index_spread = float(est.get("index_spread_pct", 1.5))
        self.stock_spread = float(est.get("stock_spread_pct", 3.0))
        self.options_error = ""
        self._iv: dict[str, tuple[float, float]] = {}

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def probe(self) -> bool:
        return await self._vix() > 0

    async def _vix(self) -> float:
        hit = self._iv.get("__vix__")
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        try:
            q = await self.charts.quote(self.vix_symbol)
            vix = _num((q or {}).get("last_price")) / 100.0
        except Exception:                               # noqa: BLE001
            vix = 0.0
        if vix > 0:
            self._iv["__vix__"] = (time.time(), vix)
        return vix

    async def iv(self, symbol: str) -> float:
        """India VIX (scaled) for an index; recent realised volatility for a stock."""
        sym = symbol.upper()
        hit = self._iv.get(sym)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        vix = await self._vix()
        if sym in INDEX_SYMBOLS:
            value = (vix or 0.14) * self.index_iv.get(sym, 1.1)
        else:
            value = 0.0
            try:
                bars = await self.charts.candles(symbol, "1d")
                closes = [b.close for b in bars][-21:]
                import math
                rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False)
                        if a > 0 and b > 0]
                if len(rets) >= 10:
                    mean = sum(rets) / len(rets)
                    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
                    value = math.sqrt(var * 252) * self.stock_premium
            except Exception:                           # noqa: BLE001
                value = 0.0
            if value <= 0:
                value = (vix or 0.14) * 1.4
            value = min(max(value, 0.12), 0.90)
        self._iv[sym] = (time.time(), value)
        return value

    async def _spot(self, symbol: str, spot: float) -> float:
        if spot and spot > 0:
            return spot
        try:
            q = await self.charts.quote(symbol)
            return _num((q or {}).get("last_price"))
        except Exception:                               # noqa: BLE001
            return 0.0

    async def chain_for_window(self, symbol: str, spot: float,
                               min_dte: int, max_dte: int) -> list[OptionContract]:
        self.options_error = ""
        spot = await self._spot(symbol, spot)
        if spot <= 0:
            self.options_error = f"no price for {symbol} to estimate options from"
            return []
        sym = symbol.upper()
        out = estimate_chain(
            symbol, spot, await self.iv(symbol), self.lots.get(sym, 1),
            datetime.now(IST), weekly=self.weekly, weekday=self.weekday,
            steps=self.steps, min_dte=min_dte, max_dte=max_dte,
            spread_pct=self.index_spread if sym in INDEX_SYMBOLS else self.stock_spread)
        if not out:
            self.options_error = f"no estimated {symbol} expiry between {min_dte} and {max_dte} days"
        return out

    async def expiries(self, symbol: str) -> list[int]:
        today = datetime.now(IST).date()
        return [int(datetime.combine(d, datetime.min.time(), tzinfo=IST).timestamp())
                for d in expiries(symbol, today, self.weekly, self.weekday)]

    async def option_chain(self, symbol: str, expiry_epoch: int, spot: float):
        want = datetime.fromtimestamp(expiry_epoch, tz=IST).date().isoformat()
        return [c for c in await self.chain_for_window(symbol, spot, 0, 60)
                if c.expiry == want]


class IndiaChains:
    """NSE first; estimated prices when NSE refuses.

    Which one is serving is always visible (`chosen`, `estimated_now`), so a
    model price is never taken for a market quote.
    """

    delayed = False
    greeks = False

    def __init__(self, nse: NseChains, estimated: EstimatedChains | None) -> None:
        self.nse = nse
        self.estimated = estimated
        self.nse_ok: bool | None = None
        self.estimated_now = False
        self.chosen = "NSE"
        self._last_nse_try = 0.0

    @property
    def options_error(self) -> str:
        return (self.estimated.options_error if self.estimated_now and self.estimated
                else self.nse.options_error)

    @options_error.setter
    def options_error(self, value: str) -> None:
        self.nse.options_error = value
        if self.estimated:
            self.estimated.options_error = value

    async def open(self) -> None:
        await self.nse.open()

    async def close(self) -> None:
        await self.nse.close()

    async def probe(self) -> bool:
        self.nse_ok = await self.nse.probe()
        self._last_nse_try = time.time()
        if self.nse_ok or self.estimated is None:
            self._use(estimated=False)
            return bool(self.nse_ok)
        log.warning("NSE refused (%s) — India options will use ESTIMATED prices "
                    "(Black-Scholes from Yahoo spot and India VIX). NSE is retried "
                    "every 15 minutes.", self.nse.options_error or "no chain")
        self._use(estimated=True)
        return await self.estimated.probe() or True

    def _use(self, estimated: bool) -> None:
        self.estimated_now = estimated
        self.chosen = "estimated (NSE refused)" if estimated else "NSE"

    async def chain_for_window(self, symbol: str, spot: float,
                               min_dte: int, max_dte: int) -> list[OptionContract]:
        # Give NSE another chance now and then; it refuses in spells.
        if self.estimated_now and time.time() - self._last_nse_try > 900:
            self._last_nse_try = time.time()
            self.nse_ok = await self.nse.probe()
            if self.nse_ok:
                log.info("NSE is answering again — back to real option quotes")
                self._use(estimated=False)
        if not self.estimated_now:
            got = await self.nse.chain_for_window(symbol, spot, min_dte, max_dte)
            if got or self.estimated is None:
                return got
            if self.nse.options_error and "no " in self.nse.options_error \
                    and "expiry between" in self.nse.options_error:
                return got                              # NSE answered; nothing in window
            log.warning("NSE chain for %s failed (%s) — estimating", symbol,
                        self.nse.options_error)
            self._use(estimated=True)
        return await self.estimated.chain_for_window(symbol, spot, min_dte, max_dte)

    async def expiries(self, symbol: str) -> list[int]:
        src = self.estimated if self.estimated_now and self.estimated else self.nse
        return await src.expiries(symbol)

    async def option_chain(self, symbol: str, expiry_epoch: int, spot: float):
        src = self.estimated if self.estimated_now and self.estimated else self.nse
        return await src.option_chain(symbol, expiry_epoch, spot)


def make_india_feed(cfg: Any) -> Any:
    """Yahoo charts under NSE names + NSE chains (estimated when NSE refuses)."""
    from panaoptions.data.feed import YahooFeed
    from panaoptions.data.hybrid import HybridFeed

    charts = YahooNames(YahooFeed(check_options=False), cfg.get("data.yahoo_symbols") or {},
                        str(cfg.get("data.yahoo_suffix", ".NS")))
    nse = NseChains(cfg.get("data.lot_sizes") or {}, default_lot=1)
    fallback = bool(cfg.get("data.estimated.enabled", True))
    chains = IndiaChains(nse, EstimatedChains(charts, cfg) if fallback else None)
    log.info("market data (India): Yahoo charts + NSE option chains%s",
             ", estimated prices if NSE refuses" if fallback else "")
    return HybridFeed(charts=charts, chains=chains, chains_name="NSE")
