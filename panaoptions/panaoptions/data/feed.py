"""Market data from Yahoo Finance's public endpoints. No API key, no package.

Deliberately plain httpx rather than yfinance: one less dependency to install
on a Windows machine, and the two endpoints used here are stable and simple.

    /v8/finance/chart/{symbol}      quotes, OHLCV candles, pre/post market
    /v7/finance/options/{symbol}    expiries, strikes, bid/ask, IV, OI

Delta is not served by Yahoo and is computed from the implied volatility — see
data/greeks.py.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from panaoptions.data.greeks import delta as bs_delta
from panaoptions.logging import get_logger
from panaoptions.models import Candle, OptionContract, OptionRight

log = get_logger("feed")

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
# The same chart API on Yahoo's second host: the backup when query1 fails.
CHART_BACKUP = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
OPTIONS = "https://query2.finance.yahoo.com/v7/finance/options/{sym}"
NEWS = "https://query2.finance.yahoo.com/v1/finance/search"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json",
}

_INTERVAL_RANGE = {"1m": "5d", "5m": "60d", "15m": "60d", "1d": "1y"}


class YahooFeed:
    # Why the last request to each endpoint family failed, or "".
    options_error: str = ""
    chart_error: str = ""
    # Whether the options endpoint answered at connect time. None = not probed.
    options_available: bool | None = None

    def __init__(self, timeout: float = 15.0, check_options: bool = True) -> None:
        self.timeout = timeout
        # False when this feed only draws charts and another source prices
        # the options (India: NSE); then Yahoo's option endpoint refusing is
        # not news and must not be logged as an error.
        self.check_options = check_options
        self._client: httpx.AsyncClient | None = None
        self.connected = False
        # Which source answered each symbol's last candles request.
        self.candle_source: dict[str, str] = {}

    async def __aenter__(self) -> YahooFeed:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> bool:
        self._client = httpx.AsyncClient(headers=HEADERS, timeout=self.timeout,
                                         follow_redirects=True)
        try:
            r = await self._client.get(CHART.format(sym="SPY"),
                                       params={"range": "1d", "interval": "1d"})
            if r.status_code != 200:
                log.error("Yahoo unreachable (HTTP %s) — check your connection, "
                          "a VPN, or a corporate proxy", r.status_code)
                return False
            self.connected = True

            # Charts are not the product. This app buys options, so an
            # options endpoint that refuses the request means the desk can
            # screen, chart and fire setups all day and never place one paper
            # trade — which is exactly what it looks like from the outside.
            self.options_error = ""
            if not self.check_options:
                self.options_available = False
                return True
            probe = await self._get(OPTIONS.format(sym="SPY"))
            self.options_available = bool(
                (probe or {}).get("optionChain", {}).get("result"))
            if not self.options_available:
                log.error(
                    "Yahoo option chains are NOT available (%s). Charts work, "
                    "so the desk will screen and fire setups — and every one "
                    "of them will report 'no contract'. Nothing can trade "
                    "until this endpoint answers.",
                    self.options_error or "empty response")
            return True
        except Exception as exc:
            log.error("Yahoo connect failed: %s", exc)
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self.connected = False

    # ------------------------------------------------------------------ #
    async def _get(self, url: str, **params: Any) -> dict[str, Any] | None:
        if not self._client:
            raise RuntimeError("feed used before connect()")
        try:
            r = await self._client.get(url, params=params)
            if r.status_code != 200:
                self._note_failure(url, f"HTTP {r.status_code}")
                log.debug("%s -> HTTP %s", url, r.status_code)
                return None
            return r.json()
        except Exception as exc:
            self._note_failure(url, f"{type(exc).__name__}: {exc}")
            log.debug("%s failed: %s", url, exc)
            return None

    def _note_failure(self, url: str, reason: str) -> None:
        """Keep the last reason a request failed, per endpoint family.

        Returning a bare empty list for a rejected request is how "the market
        has no puts today" and "Yahoo refused the request" end up reading the
        same on screen. The options endpoint is a DIFFERENT host and API path
        from the chart one and fails independently of it, so a desk whose
        charts work can still be unable to price a single contract.
        """
        if url.startswith(OPTIONS.split("{")[0]):
            self.options_error = reason
        else:
            self.chart_error = reason

    # ------------------------------------------------------------------ #
    async def candles(self, symbol: str, interval: str = "5m",
                      include_prepost: bool = False) -> list[Candle]:
        """Candles, with two backups when Yahoo's chart host fails:
          1. the same chart API on Yahoo's second host (query2);
          2. for 5m/15m/30m, the 1-minute bars rebuilt into the interval.
        Which one answered is kept in `candle_source[symbol]`."""
        prepost = "true" if include_prepost else "false"
        for label, url in (("yahoo query1", CHART), ("yahoo query2", CHART_BACKUP)):
            bars = parse_candles(await self._get(
                url.format(sym=symbol), interval=interval,
                range=_INTERVAL_RANGE.get(interval, "60d"), includePrePost=prepost))
            if bars:
                self.candle_source[symbol] = label
                self.chart_error = ""
                return bars
        minutes = {"5m": 5, "15m": 15, "30m": 30}.get(interval)
        if minutes:
            for url in (CHART, CHART_BACKUP):
                ones = parse_candles(await self._get(
                    url.format(sym=symbol), interval="1m", range="5d",
                    includePrePost=prepost))
                if ones:
                    self.candle_source[symbol] = f"rebuilt from 1m ({interval})"
                    self.chart_error = ""
                    return resample_candles(ones, minutes)
        self.candle_source[symbol] = "none — every source failed"
        return []

    async def quote(self, symbol: str) -> dict[str, Any] | None:
        """Last price, previous close and the pre-market print if there is one."""
        payload = await self._get(CHART.format(sym=symbol), interval="1m",
                                  range="1d", includePrePost="true")
        try:
            meta = payload["chart"]["result"][0]["meta"]
        except (KeyError, IndexError, TypeError):
            return None
        return {
            "symbol": symbol,
            "last_price": meta.get("regularMarketPrice"),
            "previous_close": (meta.get("chartPreviousClose")
                               or meta.get("previousClose")),
            "pre_market_price": meta.get("preMarketPrice"),
            "volume": meta.get("regularMarketVolume") or 0,
        }

    async def news(self, symbol: str, count: int = 10) -> list[dict[str, Any]]:
        """Recent headlines for `symbol`: title, publisher, published (UTC).

        Read by the Macro & Sentiment agent. A failure returns [] and is NOT
        recorded as a chart or options error — no headlines is not a broken
        data feed, and must not turn the dashboard's data banner red.
        """
        if not self._client:
            return []
        try:
            r = await self._client.get(NEWS, params={
                "q": symbol, "newsCount": count, "quotesCount": 0})
            if r.status_code != 200:
                return []
            return parse_news(r.json())
        except Exception as exc:                     # noqa: BLE001
            log.debug("news for %s failed: %s", symbol, exc)
            return []

    async def futures_change(self, symbol: str) -> float | None:
        """An index future's move since the prior settle, in percent.

        ES=F and NQ=F through the same chart endpoint as everything else.
        """
        q = await self.quote(symbol)
        try:
            last, prev = float(q["last_price"]), float(q["previous_close"])
        except (TypeError, KeyError, ValueError):
            return None
        return round((last - prev) / prev * 100, 2) if prev else None

    async def expiries(self, symbol: str) -> list[int]:
        payload = await self._get(OPTIONS.format(sym=symbol))
        try:
            return list(payload["optionChain"]["result"][0]["expirationDates"])
        except (KeyError, IndexError, TypeError):
            return []

    async def option_chain(self, symbol: str, expiry_epoch: int,
                           spot: float) -> list[OptionContract]:
        payload = await self._get(OPTIONS.format(sym=symbol), date=expiry_epoch)
        return parse_chain(payload, symbol, spot)

    async def chain_for_window(self, symbol: str, spot: float,
                               min_dte: int, max_dte: int) -> list[OptionContract]:
        """Every contract across the expiries inside the DTE window."""
        self.options_error = ""
        stamps = await self.expiries(symbol)
        today = datetime.now(UTC).date()

        if not stamps:
            # No expiry list at all is a broken endpoint, not a quiet market.
            self.options_error = (self.options_error
                                  or "the options endpoint returned nothing")
            log.warning("%s: no expiry list — %s", symbol, self.options_error)
            return []

        wanted = []
        for epoch in stamps:
            dte = (datetime.fromtimestamp(epoch, tz=UTC).date() - today).days
            if min_dte <= dte <= max_dte:
                wanted.append(epoch)
        if not wanted:
            nearest = sorted(
                (datetime.fromtimestamp(e, tz=UTC).date() - today).days
                for e in stamps)
            self.options_error = (
                f"no expiry between {min_dte} and {max_dte} days out; the "
                f"listed ones are {', '.join(str(d) for d in nearest[:6])} "
                f"days")
            log.debug("%s: %s", symbol, self.options_error)
            return []

        chains = await asyncio.gather(
            *[self.option_chain(symbol, e, spot) for e in wanted],
            return_exceptions=True)
        out: list[OptionContract] = []
        for chain in chains:
            if isinstance(chain, Exception):
                continue
            out.extend(chain)
        return out


# --------------------------------------------------------------------------- #
# Parsing, kept as free functions so they can be tested without a network.
# --------------------------------------------------------------------------- #
def parse_news(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Yahoo's search response as [{title, publisher, published}], newest first."""
    out: list[dict[str, Any]] = []
    for item in (payload or {}).get("news") or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        stamp = item.get("providerPublishTime")
        try:
            published = datetime.fromtimestamp(int(stamp), tz=UTC) if stamp else None
        except (TypeError, ValueError, OSError):
            published = None
        out.append({"title": title, "publisher": item.get("publisher") or "",
                    "published": published})
    out.sort(key=lambda n: n["published"] or datetime.min.replace(tzinfo=UTC),
             reverse=True)
    return out


def resample_candles(bars: list[Candle], minutes: int) -> list[Candle]:
    """1-minute bars into `minutes` buckets on the clock (5m: :00, :05, ...;
    US and NSE session opens both sit on those boundaries). Open of the first,
    high/low of all, close of the last, the volumes summed."""
    out: list[Candle] = []
    step = minutes * 60
    for b in sorted(bars, key=lambda x: x.ts):
        epoch = int(b.ts.timestamp())
        start = datetime.fromtimestamp(epoch - epoch % step, tz=b.ts.tzinfo or UTC)
        if out and out[-1].ts == start:
            last = out[-1]
            out[-1] = last.model_copy(update={
                "high": max(last.high, b.high), "low": min(last.low, b.low),
                "close": b.close, "volume": (last.volume or 0) + (b.volume or 0)})
        else:
            out.append(Candle(ts=start, open=b.open, high=b.high, low=b.low,
                              close=b.close, volume=b.volume or 0))
    return out


def parse_candles(payload: dict[str, Any] | None) -> list[Candle]:
    """OHLCV from a chart response.

    A bucket with a null price is a gap in the tape. It is DROPPED, never
    forward-filled: an invented candle would print a body and a wick that
    nobody traded, and the pattern engine would read it as a signal.
    """
    try:
        result = payload["chart"]["result"][0]
        stamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return []

    out: list[Candle] = []
    for i, epoch in enumerate(stamps):
        try:
            o, h, low, c = (quote["open"][i], quote["high"][i],
                            quote["low"][i], quote["close"][i])
        except (IndexError, KeyError):
            continue
        if None in (o, h, low, c):
            continue
        volume = (quote.get("volume") or [None] * len(stamps))[i] or 0
        out.append(Candle(ts=datetime.fromtimestamp(epoch, tz=UTC),
                          open=float(o), high=float(h), low=float(low),
                          close=float(c), volume=float(volume)))
    return sorted(out, key=lambda c: c.ts)


def _days_to_close(expiry_date, now: datetime | None = None) -> float:
    """Fractional days until 16:00 ET on the expiry date, never below 5 min."""
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now = (now or datetime.now(UTC)).astimezone(et)
    close = datetime.combine(expiry_date, dtime(16, 0), tzinfo=et)
    return max((close - now).total_seconds() / 86400.0, 5 / 1440)


def parse_chain(payload: dict[str, Any] | None, symbol: str,
                spot: float) -> list[OptionContract]:
    try:
        result = payload["optionChain"]["result"][0]
        options = result["options"][0]
        expiry_epoch = options.get("expirationDate")
    except (KeyError, IndexError, TypeError):
        return []

    expiry_date = datetime.fromtimestamp(expiry_epoch, tz=UTC).date()
    dte = max((expiry_date - datetime.now(UTC).date()).days, 0)
    # Time left to the 16:00 ET expiry, in days, for the delta estimate. A
    # whole-day count is 0 on expiry day, where Black-Scholes has no answer and
    # returned 0 delta — every same-day contract then failed the delta filter.
    years_days = _days_to_close(expiry_date)

    out: list[OptionContract] = []
    for right, key in ((OptionRight.CALL, "calls"), (OptionRight.PUT, "puts")):
        for row in options.get(key) or []:
            strike = row.get("strike")
            if strike is None:
                continue
            iv = float(row.get("impliedVolatility") or 0.0)
            out.append(OptionContract(
                symbol=symbol, right=right, strike=float(strike),
                expiry=expiry_date.isoformat(), dte=dte,
                bid=float(row.get("bid") or 0.0),
                ask=float(row.get("ask") or 0.0),
                implied_volatility=iv,
                open_interest=int(row.get("openInterest") or 0),
                volume=int(row.get("volume") or 0),
                delta=bs_delta(spot, float(strike), years_days, iv,
                               right is OptionRight.CALL),
            ))
    return out
