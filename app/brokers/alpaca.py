"""Alpaca adapter — US equities and options (REST v2, via httpx, no SDK needed).

Alpaca is the most accessible US broker for this kind of system: sign up, get
keys, and you have a free paper-trading account with real market data.

    1. Create keys at https://app.alpaca.markets/paper/dashboard/overview
    2. Put them in .env:
           BROKER=alpaca
           ALPACA_API_KEY=...
           ALPACA_API_SECRET=...
           ALPACA_PAPER=true        # false only when you mean real money
    3. ACTIVE_MARKET=US

Free-tier market data comes from the IEX feed, which is a partial view of the
tape. Quotes and bars are real but thinner than the consolidated SIP feed; set
ALPACA_FEED=sip if your subscription includes it.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.brokers.base import BrokerAdapter, OrderResult
from app.core.logging import get_logger
from app.core.models import Candle, Instrument, OptionChain, OptionLeg, Quote, Side
from app.core.registry import register_broker

log = get_logger("broker.alpaca")

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"

_TF = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min",
       "60m": "1Hour", "1h": "1Hour", "1d": "1Day"}


@register_broker("alpaca")
class AlpacaBroker(BrokerAdapter):
    name = "alpaca"
    supports_options = True
    supports_live_orders = True

    def __init__(self, credentials: dict[str, str] | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, config)
        self._trade: httpx.AsyncClient | None = None
        self._data: httpx.AsyncClient | None = None
        # Defaults to the paper endpoint. You have to opt IN to real money.
        self._paper = str(self.credentials.get("paper", "true")).lower() != "false"
        self.is_paper_account = self._paper
        self._feed = self.credentials.get("feed") or os.getenv("ALPACA_FEED", "iex")

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.credentials.get("api_key", ""),
            "APCA-API-SECRET-KEY": self.credentials.get("api_secret", ""),
            "accept": "application/json",
        }

    # ----------------------- lifecycle -----------------------
    async def connect(self) -> bool:
        if not self.credentials.get("api_key") or not self.credentials.get("api_secret"):
            log.error("ALPACA_API_KEY / ALPACA_API_SECRET missing from .env")
            return False

        base = PAPER_BASE if self._paper else LIVE_BASE
        self._trade = httpx.AsyncClient(base_url=base, headers=self._headers(), timeout=20.0)
        self._data = httpx.AsyncClient(base_url=DATA_BASE, headers=self._headers(), timeout=20.0)

        try:
            r = await self._trade.get("/v2/account")
            if r.status_code != 200:
                log.error("alpaca auth failed: %s %s", r.status_code, r.text[:200])
                return False
            acct = r.json()
            log.info("alpaca connected (%s) — equity $%s, status %s",
                     "paper" if self._paper else "LIVE",
                     acct.get("equity"), acct.get("status"))
            if not self._paper:
                log.warning("alpaca is pointed at a LIVE account — real money")
            self._connected = True
            return True
        except Exception as exc:
            log.error("alpaca connect error: %s", exc)
            return False

    async def disconnect(self) -> None:
        for client in (self._trade, self._data):
            if client:
                await client.aclose()
        self._connected = False

    # ----------------------- market data -----------------------
    async def get_quote(self, symbol: str) -> Quote | None:
        if not self._data:
            return None
        try:
            r = await self._data.get(f"/v2/stocks/{symbol}/snapshot",
                                     params={"feed": self._feed})
            if r.status_code != 200:
                return None
            snap = r.json()
            trade = snap.get("latestTrade") or {}
            quote = snap.get("latestQuote") or {}
            daily = snap.get("dailyBar") or {}
            prev = snap.get("prevDailyBar") or {}

            last = trade.get("p") or daily.get("c") or 0.0
            prev_close = prev.get("c") or daily.get("o") or last
            change = (last - prev_close) / prev_close * 100 if prev_close else 0.0
            return Quote(symbol=symbol, last_price=float(last),
                         change_pct=round(change, 3),
                         volume=float(daily.get("v", 0) or 0),
                         bid=quote.get("bp"), ask=quote.get("ap"))
        except Exception as exc:
            log.warning("alpaca quote failed %s: %s", symbol, exc)
            return None

    async def get_candles(self, symbol: str, timeframe: str,
                          count: int = 200) -> list[Candle]:
        if not self._data:
            return []
        tf = _TF.get(timeframe, "5Min")
        minutes = {"1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30,
                   "1Hour": 60, "1Day": 1440}.get(tf, 5)
        # US session is 390 minutes; pad generously for weekends and holidays.
        days = max(2, int(count * minutes / 390) + 4)
        start = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        try:
            r = await self._data.get(f"/v2/stocks/{symbol}/bars", params={
                "timeframe": tf, "start": start, "limit": min(count * 2, 10000),
                "feed": self._feed, "adjustment": "raw",
            })
            if r.status_code != 200:
                log.warning("alpaca bars %s: %s %s", symbol, r.status_code, r.text[:160])
                return []
            bars = r.json().get("bars") or []
            out = [
                Candle(ts=datetime.fromisoformat(b["t"].replace("Z", "+00:00")),
                       open=b["o"], high=b["h"], low=b["l"], close=b["c"],
                       volume=b.get("v", 0))
                for b in bars
            ]
            return out[-count:]
        except Exception as exc:
            log.warning("alpaca candles failed %s: %s", symbol, exc)
            return []

    async def get_expiries(self, underlying: str) -> list[str]:
        if not self._trade:
            return []
        try:
            today = datetime.now().date()
            r = await self._trade.get("/v2/options/contracts", params={
                "underlying_symbols": underlying, "status": "active",
                "expiration_date_gte": today.isoformat(), "limit": 1000,
            })
            if r.status_code != 200:
                log.warning("alpaca expiries for %s → HTTP %s: %s",
                            underlying, r.status_code, r.text[:200])
                return []
            contracts = r.json().get("option_contracts") or []
            if not contracts:
                log.info("alpaca returned no option contracts for %s — options "
                         "may not be enabled on this account", underlying)
            return sorted({c["expiration_date"] for c in contracts})[:6]
        except Exception as exc:
            log.warning("alpaca expiries failed for %s: %s", underlying, exc)
            return []

    async def get_option_chain(self, underlying: str,
                               expiry: str | None = None) -> OptionChain | None:
        if not self._trade or not self._data:
            return None

        expiries = await self.get_expiries(underlying)
        exp = expiry or (expiries[0] if expiries else None)
        if not exp:
            log.info("alpaca: no option expiries available for %s", underlying)
            return None

        spot_q = await self.get_quote(underlying)
        spot = spot_q.last_price if spot_q else 0.0
        if not spot:
            log.warning("alpaca: no spot price for %s, cannot build a chain",
                        underlying)
            return None

        window = float(self.config.get("strikes_around_atm", 10))
        step = max(spot * 0.01, 1.0)
        lo, hi = spot - window * step, spot + window * step

        try:
            r = await self._trade.get("/v2/options/contracts", params={
                "underlying_symbols": underlying, "expiration_date": exp,
                "status": "active", "limit": 1000,
                "strike_price_gte": f"{max(lo, 0):.2f}",
                "strike_price_lte": f"{hi:.2f}",
            })
            if r.status_code != 200:
                log.warning("alpaca option contracts for %s → HTTP %s: %s",
                            underlying, r.status_code, r.text[:200])
                return None
            contracts = r.json().get("option_contracts") or []
            if not contracts:
                log.info("alpaca: no %s contracts near spot for expiry %s",
                         underlying, exp)
                return None

            symbols = [c["symbol"] for c in contracts][:400]
            snaps: dict[str, Any] = {}
            # The options snapshot endpoint caps the symbol list per request.
            for i in range(0, len(symbols), 100):
                sr = await self._data.get("/v1beta1/options/snapshots", params={
                    "symbols": ",".join(symbols[i:i + 100])})
                if sr.status_code == 200:
                    snaps.update(sr.json().get("snapshots") or {})

            legs: list[OptionLeg] = []
            for c in contracts:
                snap = snaps.get(c["symbol"]) or {}
                trade = snap.get("latestTrade") or {}
                quote = snap.get("latestQuote") or {}
                greeks = snap.get("greeks") or {}
                mid = None
                if quote.get("bp") and quote.get("ap"):
                    mid = (quote["bp"] + quote["ap"]) / 2
                legs.append(OptionLeg(
                    strike=float(c["strike_price"]),
                    option_type="CE" if c["type"] == "call" else "PE",
                    ltp=float(trade.get("p") or mid or 0.0),
                    oi=float(c.get("open_interest") or 0),
                    oi_change=0.0,          # Alpaca does not expose an OI delta
                    volume=float(snap.get("dailyBar", {}).get("v") or 0),
                    iv=float(snap.get("impliedVolatility") or 0.0),
                    delta=greeks.get("delta"), gamma=greeks.get("gamma"),
                    theta=greeks.get("theta"), vega=greeks.get("vega"),
                ))
            if not legs:
                return None
            return OptionChain(underlying=underlying, spot=spot, expiry=exp, legs=legs)
        except Exception as exc:
            log.warning("alpaca option chain failed: %s", exc)
            return None

    # ----------------------- account -----------------------
    async def get_funds(self) -> dict[str, float]:
        if not self._trade:
            return {}
        try:
            a = (await self._trade.get("/v2/account")).json()
            return {"available": float(a.get("buying_power", 0)),
                    "used": float(a.get("equity", 0)) - float(a.get("cash", 0)),
                    "equity": float(a.get("equity", 0))}
        except Exception:
            return {}

    async def get_positions(self) -> list[dict[str, Any]]:
        if not self._trade:
            return []
        try:
            r = await self._trade.get("/v2/positions")
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    # ----------------------- orders -----------------------
    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        if not self._trade:
            return OrderResult(False, message="not connected")

        body: dict[str, Any] = {
            "symbol": instrument.tradingsymbol,
            "qty": str(quantity),
            "side": "buy" if side == Side.BUY else "sell",
            "type": order_type.lower(),
            # MIS maps to a day order; US brokers have no intraday product code.
            "time_in_force": "day",
        }
        if order_type.upper() == "LIMIT":
            body["limit_price"] = f"{price:.2f}"
        if tag:
            body["client_order_id"] = tag[:48]

        try:
            r = await self._trade.post("/v2/orders", json=body)
            data = r.json()
            if r.status_code in (200, 201):
                log.info("alpaca order placed: %s", data.get("id"))
                return OrderResult(True, order_id=str(data.get("id")),
                                   message="placed", raw=data)
            return OrderResult(False, message=str(data)[:300], raw=data)
        except Exception as exc:
            return OrderResult(False, message=str(exc))

    async def cancel_order(self, order_id: str) -> OrderResult:
        if not self._trade:
            return OrderResult(False, message="not connected")
        try:
            r = await self._trade.delete(f"/v2/orders/{order_id}")
            ok = r.status_code in (200, 204)
            return OrderResult(ok, order_id=order_id,
                               message="cancelled" if ok else r.text[:200])
        except Exception as exc:
            return OrderResult(False, message=str(exc))

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        if not self._trade:
            return {}
        try:
            r = await self._trade.get(f"/v2/orders/{order_id}")
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}
