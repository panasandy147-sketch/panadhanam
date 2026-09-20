"""Upstox adapter (REST v2, via httpx — no SDK dependency required).

Auth:
  1. Create an app at https://account.upstox.com/developer/apps
  2. Visit https://api.upstox.com/v2/login/authorization/dialog
        ?client_id=<KEY>&redirect_uri=<URI>&response_type=code
  3. Exchange the `code` for an access token (scripts/upstox_login.py)
  4. Put the token in .env as UPSTOX_ACCESS_TOKEN (valid till ~3:30am next day)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from app.brokers.base import BrokerAdapter, OrderResult
from app.core.logging import get_logger
from app.core.models import Candle, Instrument, OptionChain, OptionLeg, Quote, Side
from app.core.registry import register_broker

log = get_logger("broker.upstox")

BASE = "https://api.upstox.com/v2"
_TF = {"1m": "1minute", "5m": "30minute", "15m": "30minute", "30m": "30minute", "1d": "day"}


@register_broker("upstox")
class UpstoxBroker(BrokerAdapter):
    name = "upstox"
    supports_options = True
    supports_live_orders = True
    is_paper_account = False  # real money

    def __init__(self, credentials: dict[str, str] | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, config)
        self._client: httpx.AsyncClient | None = None
        self._keys: dict[str, str] = {}   # symbol -> instrument_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.get('access_token', '')}",
            "Accept": "application/json",
        }

    async def connect(self) -> bool:
        token = self.credentials.get("access_token")
        if not token:
            log.error("UPSTOX_ACCESS_TOKEN missing from .env")
            return False
        self._client = httpx.AsyncClient(base_url=BASE, headers=self._headers(), timeout=20.0)
        try:
            r = await self._client.get("/user/profile")
            if r.status_code != 200:
                log.error("upstox auth failed: %s %s", r.status_code, r.text[:200])
                return False
            log.info("upstox connected as %s",
                     r.json().get("data", {}).get("user_name", "?"))
            self._connected = True
            return True
        except Exception as exc:
            log.error("upstox connect error: %s", exc)
            return False

    def _instrument_key(self, symbol: str) -> str:
        """Upstox addresses instruments by key, e.g. NSE_EQ|INE002A01018.
        Override the mapping in config/universe.yaml via `upstox_key:` per symbol."""
        if symbol in self._keys:
            return self._keys[symbol]
        index_map = {
            "NIFTY": "NSE_INDEX|Nifty 50",
            "NIFTY 50": "NSE_INDEX|Nifty 50",
            "BANKNIFTY": "NSE_INDEX|Nifty Bank",
            "NIFTY BANK": "NSE_INDEX|Nifty Bank",
            "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
        }
        return index_map.get(symbol.upper(), f"NSE_EQ|{symbol.upper()}")

    async def get_quote(self, symbol: str) -> Quote | None:
        if not self._client:
            return None
        key = self._instrument_key(symbol)
        try:
            r = await self._client.get("/market-quote/quotes", params={"instrument_key": key})
            if r.status_code != 200:
                return None
            payload = next(iter(r.json().get("data", {}).values()), None)
            if not payload:
                return None
            ohlc = payload.get("ohlc", {})
            close = ohlc.get("close") or payload.get("last_price", 0)
            ltp = payload.get("last_price", 0)
            change = (ltp - close) / close * 100 if close else 0.0
            return Quote(symbol=symbol, last_price=ltp, change_pct=round(change, 3),
                         volume=payload.get("volume", 0), oi=payload.get("oi"))
        except Exception as exc:
            log.warning("upstox quote failed %s: %s", symbol, exc)
            return None

    async def get_candles(self, symbol: str, timeframe: str, count: int = 200) -> list[Candle]:
        if not self._client:
            return []
        key = self._instrument_key(symbol)
        interval = _TF.get(timeframe, "30minute")
        to_d = datetime.now().date()
        from_d = to_d - timedelta(days=30 if interval != "day" else 200)
        try:
            r = await self._client.get(
                f"/historical-candle/{key}/{interval}/{to_d}/{from_d}")
            if r.status_code != 200:
                return []
            rows = r.json().get("data", {}).get("candles", [])
            out = [Candle(ts=datetime.fromisoformat(row[0]), open=row[1], high=row[2],
                          low=row[3], close=row[4], volume=row[5]) for row in rows]
            out.sort(key=lambda c: c.ts)
            return out[-count:]
        except Exception as exc:
            log.warning("upstox candles failed %s: %s", symbol, exc)
            return []

    async def get_expiries(self, underlying: str) -> list[str]:
        if not self._client:
            return []
        try:
            r = await self._client.get("/option/contract",
                                       params={"instrument_key": self._instrument_key(underlying)})
            if r.status_code != 200:
                return []
            exps = sorted({row.get("expiry") for row in r.json().get("data", []) if row.get("expiry")})
            today = datetime.now().date().isoformat()
            return [e for e in exps if e >= today][:6]
        except Exception:
            return []

    async def get_option_chain(self, underlying: str, expiry: str | None = None) -> OptionChain | None:
        if not self._client:
            return None
        expiries = await self.get_expiries(underlying)
        exp = expiry or (expiries[0] if expiries else None)
        if not exp:
            return None
        try:
            r = await self._client.get("/option/chain", params={
                "instrument_key": self._instrument_key(underlying), "expiry_date": exp})
            if r.status_code != 200:
                return None
            data = r.json().get("data", [])
            if not data:
                return None
            spot = data[0].get("underlying_spot_price", 0.0)
            legs: list[OptionLeg] = []
            for row in data:
                strike = row.get("strike_price", 0.0)
                for side_key, opt in (("call_options", "CE"), ("put_options", "PE")):
                    node = row.get(side_key) or {}
                    md = node.get("market_data") or {}
                    greeks = node.get("option_greeks") or {}
                    legs.append(OptionLeg(
                        strike=strike, option_type=opt,
                        ltp=md.get("ltp", 0.0), oi=md.get("oi", 0.0),
                        oi_change=md.get("oi", 0.0) - md.get("prev_oi", md.get("oi", 0.0)),
                        volume=md.get("volume", 0.0),
                        iv=(greeks.get("iv") or 0.0) / 100.0,
                        delta=greeks.get("delta"), gamma=greeks.get("gamma"),
                        theta=greeks.get("theta"), vega=greeks.get("vega"),
                    ))
            return OptionChain(underlying=underlying, spot=spot, expiry=exp, legs=legs)
        except Exception as exc:
            log.warning("upstox chain failed: %s", exc)
            return None

    async def get_funds(self) -> dict[str, float]:
        if not self._client:
            return {}
        try:
            r = await self._client.get("/user/get-funds-and-margin", params={"segment": "SEC"})
            d = r.json().get("data", {}).get("equity", {})
            return {"available": d.get("available_margin", 0.0), "used": d.get("used_margin", 0.0)}
        except Exception:
            return {}

    async def get_positions(self) -> list[dict[str, Any]]:
        if not self._client:
            return []
        try:
            r = await self._client.get("/portfolio/short-term-positions")
            return r.json().get("data", [])
        except Exception:
            return []

    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        if not self._client:
            return OrderResult(False, message="not connected")
        body = {
            "quantity": quantity,
            "product": "I" if product == "MIS" else "D",
            "validity": "DAY",
            "price": round(price, 2) if order_type == "LIMIT" else 0,
            "tag": tag[:20],
            "instrument_token": self._instrument_key(instrument.tradingsymbol),
            "order_type": order_type,
            "transaction_type": side.value,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        try:
            r = await self._client.post("/order/place", json=body)
            data = r.json()
            if r.status_code == 200 and data.get("status") == "success":
                oid = data["data"]["order_id"]
                log.info("upstox order placed: %s", oid)
                return OrderResult(True, order_id=str(oid), message="placed", raw=data)
            return OrderResult(False, message=str(data)[:300], raw=data)
        except Exception as exc:
            return OrderResult(False, message=str(exc))

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
        self._connected = False
