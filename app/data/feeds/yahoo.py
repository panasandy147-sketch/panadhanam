"""Yahoo Finance data feed — real market data, no API key, both markets.

This is the default data source. It provides genuine last-traded prices and
historical candles for NSE equities/indices (via the `.NS` / `^` conventions)
and for US equities and ETFs.

It is a DATA feed, not a broker: it cannot place orders. The paper broker wraps
it so you get real prices with simulated fills — which is exactly what you want
for checking trends and setups without risking money.

Endpoints used (all public, all keyless):
    /v8/finance/chart/{symbol}     quotes + OHLCV candles
    /v7/finance/options/{symbol}   US option chain with OI and IV

When the market is closed these return the LAST TRADED values, which is the
correct thing to show — it is what the instrument actually did.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from app.brokers.base import BrokerAdapter, OrderResult
from app.core.config import get_config
from app.core.logging import get_logger
from app.core.models import (Candle, Instrument, OptionChain, OptionLeg, Quote,
                             Side)

log = get_logger("feed.yahoo")

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
OPTIONS = "https://query2.finance.yahoo.com/v7/finance/options/{sym}"

# Yahoo's interval + range vocabulary.
_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
             "60m": "60m", "1h": "60m", "1d": "1d"}
# Intraday history on Yahoo is capped; ask for the most it will give.
_RANGE = {"1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d",
          "60m": "730d", "1h": "730d", "1d": "2y"}

# Indian index tickers on Yahoo.
_INDEX_TICKERS = {
    "NIFTY": "^NSEI", "NIFTY 50": "^NSEI",
    "BANKNIFTY": "^NSEBANK", "NIFTY BANK": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
}

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json",
}


def yahoo_ticker(symbol: str) -> str:
    """Map a watchlist symbol to its Yahoo ticker for the active market."""
    key = symbol.strip().upper()
    if key in _INDEX_TICKERS:
        return _INDEX_TICKERS[key]
    suffix = get_config().market.yahoo_suffix       # ".NS" for India, "" for US
    return f"{symbol.strip().upper()}{suffix}"


class YahooFeed(BrokerAdapter):
    """Read-only market data. Order methods deliberately refuse."""

    name = "yahoo"
    supports_options = True
    supports_live_orders = False

    def __init__(self, credentials: dict[str, str] | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, config)
        self._client: httpx.AsyncClient | None = None
        self._quote_cache: dict[str, tuple[float, Quote]] = {}

    async def connect(self) -> bool:
        self._client = httpx.AsyncClient(
            headers=_HEADERS, timeout=15.0, follow_redirects=True)
        # Prove the endpoint is actually reachable before claiming success —
        # a silent failure here would look like "no data" everywhere downstream.
        try:
            probe = "^NSEI" if get_config().active_market == "IN" else "SPY"
            r = await self._client.get(CHART.format(sym=probe),
                                       params={"range": "1d", "interval": "1d"})
            if r.status_code != 200:
                log.error("Yahoo Finance unreachable (HTTP %s). Check your "
                          "internet connection or a firewall/proxy.", r.status_code)
                return False
            self._connected = True
            log.info("Yahoo Finance feed connected — real market data")
            return True
        except Exception as exc:
            log.error("Yahoo Finance connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
        self._connected = False

    # ------------------------------------------------------------------ #
    async def get_quote(self, symbol: str) -> Quote | None:
        """Last traded price. When the market is shut this is the closing
        print, which is the honest thing to display."""
        if not self._client:
            return None
        ticker = yahoo_ticker(symbol)
        try:
            r = await self._client.get(CHART.format(sym=ticker),
                                       params={"range": "1d", "interval": "1m"})
            if r.status_code != 200:
                log.debug("yahoo quote %s → HTTP %s", ticker, r.status_code)
                return None
            result = (r.json().get("chart", {}).get("result") or [None])[0]
            if not result:
                return None
            meta = result.get("meta", {})

            last = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if last is None:
                return None
            change = ((last - prev) / prev * 100) if prev else 0.0

            volume = meta.get("regularMarketVolume") or 0
            ts = meta.get("regularMarketTime")
            return Quote(
                symbol=symbol, last_price=float(last),
                change_pct=round(change, 3), volume=float(volume),
                ts=(datetime.fromtimestamp(ts, tz=timezone.utc)
                    if ts else datetime.now(timezone.utc)),
            )
        except Exception as exc:
            log.debug("yahoo quote failed %s: %s", symbol, exc)
            return None

    async def get_candles(self, symbol: str, timeframe: str,
                          count: int = 200) -> list[Candle]:
        if not self._client:
            return []
        ticker = yahoo_ticker(symbol)
        interval = _INTERVAL.get(timeframe, "5m")
        rng = _RANGE.get(timeframe, "60d")
        try:
            r = await self._client.get(CHART.format(sym=ticker),
                                       params={"range": rng, "interval": interval})
            if r.status_code != 200:
                return []
            result = (r.json().get("chart", {}).get("result") or [None])[0]
            if not result:
                return []
            return self._parse_candles(result)[-count:]
        except Exception as exc:
            log.debug("yahoo candles failed %s: %s", symbol, exc)
            return []

    @staticmethod
    def _parse_candles(result: dict[str, Any]) -> list[Candle]:
        """Yahoo returns parallel arrays with nulls for untraded slots."""
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators", {}).get("quote") or [{}])[0]) or {}
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        out: list[Candle] = []
        for i, ts in enumerate(stamps):
            try:
                o, h, low, c = opens[i], highs[i], lows[i], closes[i]
            except IndexError:
                continue
            # A null in any OHLC slot means no trade in that bucket — skip it
            # rather than inventing a price.
            if None in (o, h, low, c):
                continue
            vol = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
            out.append(Candle(
                ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(o), high=float(h), low=float(low),
                close=float(c), volume=float(vol)))
        return out

    # ------------------------------------------------------------------ #
    async def get_expiries(self, underlying: str) -> list[str]:
        """US option expiries. Yahoo does not serve NSE chains — the NSE feed
        handles India."""
        if not self._client or get_config().active_market != "US":
            return []
        try:
            r = await self._client.get(OPTIONS.format(sym=yahoo_ticker(underlying)))
            if r.status_code != 200:
                return []
            result = (r.json().get("optionChain", {}).get("result") or [None])[0]
            if not result:
                return []
            return [datetime.fromtimestamp(e, tz=timezone.utc).date().isoformat()
                    for e in (result.get("expirationDates") or [])][:6]
        except Exception:
            return []

    async def get_option_chain(self, underlying: str,
                               expiry: str | None = None) -> OptionChain | None:
        if not self._client or get_config().active_market != "US":
            return None
        ticker = yahoo_ticker(underlying)
        params: dict[str, Any] = {}
        if expiry:
            try:
                stamp = int(datetime.fromisoformat(expiry)
                            .replace(tzinfo=timezone.utc).timestamp())
                params["date"] = stamp
            except ValueError:
                pass
        try:
            r = await self._client.get(OPTIONS.format(sym=ticker), params=params)
            if r.status_code != 200:
                return None
            result = (r.json().get("optionChain", {}).get("result") or [None])[0]
            if not result:
                return None
            return self._parse_chain(underlying, result)
        except Exception as exc:
            log.debug("yahoo chain failed %s: %s", underlying, exc)
            return None

    @staticmethod
    def _parse_chain(underlying: str, result: dict[str, Any]) -> OptionChain | None:
        options = (result.get("options") or [None])[0]
        if not options:
            return None
        spot = (result.get("quote", {}) or {}).get("regularMarketPrice") or 0.0
        exp_stamp = options.get("expirationDate")
        expiry = (datetime.fromtimestamp(exp_stamp, tz=timezone.utc).date().isoformat()
                  if exp_stamp else "")

        legs: list[OptionLeg] = []
        for key, opt_type in (("calls", "CE"), ("puts", "PE")):
            for row in options.get(key) or []:
                last = row.get("lastPrice") or 0.0
                bid, ask = row.get("bid"), row.get("ask")
                # Mid is a fairer mark than a stale last print on a thin strike.
                if bid and ask:
                    last = (bid + ask) / 2
                legs.append(OptionLeg(
                    strike=float(row.get("strike", 0)),
                    option_type=opt_type,
                    ltp=float(last or 0.0),
                    oi=float(row.get("openInterest") or 0),
                    oi_change=0.0,        # Yahoo exposes no OI delta
                    volume=float(row.get("volume") or 0),
                    iv=float(row.get("impliedVolatility") or 0.0),
                ))
        if not legs:
            return None
        return OptionChain(underlying=underlying, spot=float(spot),
                           expiry=expiry, legs=legs)

    # ------------------------------------------------------------------ #
    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        return OrderResult(
            False,
            message=("Yahoo Finance is a market-data feed and cannot place "
                     "orders. Connect a broker (zerodha / upstox / angelone / "
                     "alpaca) to trade."))
