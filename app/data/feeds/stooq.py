"""Stooq data feed — a third keyless source, used when Yahoo is unavailable.

Stooq serves plain CSV over HTTPS with no key, no cookies and no rate-limit
games. It is the most reliable fallback when Yahoo is blocked by a corporate
network or is rate-limiting, and it covers both Indian and US equities.

    https://stooq.com/q/d/l/?s=aapl.us&i=d     daily OHLCV as CSV

Limits, stated plainly:
  * Daily bars are solid. Intraday coverage is patchy and not guaranteed.
  * No option chains at all — NSE and Yahoo handle those.
  * End-of-day data can lag the close by a few hours.

So it is a safety net for price history and last close, not a primary feed.
"""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

import httpx

from app.brokers.base import BrokerAdapter, OrderResult
from app.core.config import get_config
from app.core.logging import get_logger
from app.core.models import Candle, Instrument, Quote, Side

log = get_logger("feed.stooq")

CSV_URL = "https://stooq.com/q/d/l/"

# Stooq's own suffixes; the Indian ones differ from Yahoo's.
_INDEX = {
    "NIFTY": "^nsei", "NIFTY 50": "^nsei",
    "BANKNIFTY": "^nsebank", "NIFTY BANK": "^nsebank",
    "SENSEX": "^bses",
    "SPY": "spy.us", "QQQ": "qqq.us", "IWM": "iwm.us",
}


def stooq_ticker(symbol: str) -> str:
    key = symbol.strip().upper()
    if key in _INDEX:
        return _INDEX[key]
    market = get_config().active_market
    # Stooq marks Indian listings ".in" and US ones ".us".
    return f"{key.lower()}.{'in' if market == 'IN' else 'us'}"


class StooqFeed(BrokerAdapter):
    """Read-only daily price history. Cannot place orders."""

    name = "stooq"
    supports_options = False
    supports_live_orders = False

    def __init__(self, credentials: dict[str, str] | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, config)
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> bool:
        self._client = httpx.AsyncClient(
            timeout=15.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"})
        try:
            probe = "spy.us" if get_config().active_market == "US" else "^nsei"
            r = await self._client.get(CSV_URL, params={"s": probe, "i": "d"})
            # Stooq answers 200 with the text "No data" rather than a 404.
            if r.status_code != 200 or "Date" not in r.text[:200]:
                log.debug("stooq probe returned no usable data")
                return False
            self._connected = True
            log.info("Stooq feed connected (daily history fallback)")
            return True
        except Exception as exc:
            log.debug("stooq connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
        self._connected = False

    async def _fetch(self, symbol: str, interval: str) -> list[Candle]:
        if not self._client:
            return []
        try:
            r = await self._client.get(
                CSV_URL, params={"s": stooq_ticker(symbol), "i": interval})
            if r.status_code != 200 or "Date" not in r.text[:200]:
                return []
            return self.parse_csv(r.text)
        except Exception as exc:
            log.debug("stooq fetch failed %s: %s", symbol, exc)
            return []

    @staticmethod
    def parse_csv(text: str) -> list[Candle]:
        """Parse Stooq's CSV. Pure and static so it is unit-testable offline."""
        out: list[Candle] = []
        for row in csv.DictReader(io.StringIO(text)):
            try:
                ts = datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=UTC)
                out.append(Candle(
                    ts=ts,
                    open=float(row["Open"]), high=float(row["High"]),
                    low=float(row["Low"]), close=float(row["Close"]),
                    volume=float(row.get("Volume") or 0),
                ))
            except (KeyError, ValueError, TypeError):
                # A malformed row is skipped, never guessed at.
                continue
        return out

    async def get_candles(self, symbol: str, timeframe: str,
                          count: int = 200) -> list[Candle]:
        # Only daily is dependable here; intraday requests fall through to
        # another feed rather than returning something misleading.
        if timeframe != "1d":
            return []
        return (await self._fetch(symbol, "d"))[-count:]

    async def get_quote(self, symbol: str) -> Quote | None:
        candles = await self._fetch(symbol, "d")
        if not candles:
            return None
        last = candles[-1]
        prev = candles[-2] if len(candles) > 1 else last
        change = ((last.close - prev.close) / prev.close * 100) if prev.close else 0.0
        return Quote(symbol=symbol, last_price=last.close,
                     change_pct=round(change, 3), volume=last.volume, ts=last.ts)

    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        return OrderResult(False, message="Stooq is a data feed and cannot place orders")
