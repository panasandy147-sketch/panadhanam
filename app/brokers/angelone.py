"""Angel One SmartAPI adapter.

Install with:  pip install smartapi-python pyotp
Auth is fully automatic — SmartAPI uses password + TOTP, so unlike Kite/Upstox
there is no daily manual login step. Put these in .env:
    ANGELONE_API_KEY, ANGELONE_CLIENT_ID, ANGELONE_PASSWORD, ANGELONE_TOTP_SECRET
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.brokers.base import BrokerAdapter, OrderResult
from app.core.logging import get_logger
from app.core.models import Candle, Instrument, Quote, Side
from app.core.registry import register_broker

log = get_logger("broker.angelone")

_TF = {"1m": "ONE_MINUTE", "3m": "THREE_MINUTE", "5m": "FIVE_MINUTE",
       "15m": "FIFTEEN_MINUTE", "30m": "THIRTY_MINUTE", "60m": "ONE_HOUR", "1d": "ONE_DAY"}

SCRIP_MASTER = ("https://margincalculator.angelbroking.com/"
                "OpenAPI_File/files/OpenAPIScripMaster.json")


@register_broker("angelone")
class AngelOneBroker(BrokerAdapter):
    name = "angelone"
    supports_options = True
    supports_live_orders = True
    is_paper_account = False  # real money

    def __init__(self, credentials: dict[str, str] | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, config)
        self._api: Any = None
        self._scrips: list[dict[str, Any]] = []
        self._token_cache: dict[str, str] = {}

    async def connect(self) -> bool:
        try:
            import pyotp
            from SmartApi import SmartConnect
        except ImportError:
            log.error("smartapi-python not installed — run: pip install smartapi-python pyotp")
            return False

        creds = self.credentials
        required = ("api_key", "client_id", "password", "totp_secret")
        if not all(creds.get(k) for k in required):
            log.error("angelone credentials incomplete; need %s", ", ".join(required))
            return False

        try:
            self._api = SmartConnect(api_key=creds["api_key"])
            totp = pyotp.TOTP(creds["totp_secret"]).now()
            session = await asyncio.to_thread(
                self._api.generateSession, creds["client_id"], creds["password"], totp)
            if not session.get("status"):
                log.error("angelone login failed: %s", session.get("message"))
                return False
            log.info("angelone connected as %s", creds["client_id"])
            await self._load_scrips()
            self._connected = True
            return True
        except Exception as exc:
            log.error("angelone connect error: %s", exc)
            return False

    async def _load_scrips(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.get(SCRIP_MASTER)
                self._scrips = r.json()
            log.info("loaded %d angelone scrips", len(self._scrips))
        except Exception as exc:
            log.warning("scrip master download failed: %s", exc)

    def _token(self, symbol: str, exchange: str = "NSE") -> tuple[str, str] | None:
        key = f"{exchange}:{symbol}"
        if key in self._token_cache:
            return self._token_cache[key], f"{symbol}-EQ"
        wanted = f"{symbol.upper()}-EQ"
        for s in self._scrips:
            if s.get("symbol") == wanted and s.get("exch_seg") == exchange:
                self._token_cache[key] = s["token"]
                return s["token"], s["symbol"]
        return None

    async def get_quote(self, symbol: str) -> Quote | None:
        found = self._token(symbol)
        if not found or not self._api:
            return None
        token, tsym = found
        try:
            r = await asyncio.to_thread(self._api.ltpData, "NSE", tsym, token)
            d = r.get("data") or {}
            ltp, close = d.get("ltp", 0.0), d.get("close", 0.0)
            change = (ltp - close) / close * 100 if close else 0.0
            return Quote(symbol=symbol, last_price=ltp, change_pct=round(change, 3))
        except Exception as exc:
            log.warning("angelone quote failed %s: %s", symbol, exc)
            return None

    async def get_candles(self, symbol: str, timeframe: str, count: int = 200) -> list[Candle]:
        found = self._token(symbol)
        if not found or not self._api:
            return []
        token, _ = found
        interval = _TF.get(timeframe, "FIVE_MINUTE")
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=10 if interval != "ONE_DAY" else 250)
        try:
            r = await asyncio.to_thread(self._api.getCandleData, {
                "exchange": "NSE", "symboltoken": token, "interval": interval,
                "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
            })
            rows = r.get("data") or []
            out = [Candle(ts=datetime.fromisoformat(row[0].replace("+05:30", "")),
                          open=row[1], high=row[2], low=row[3], close=row[4], volume=row[5])
                   for row in rows]
            return out[-count:]
        except Exception as exc:
            log.warning("angelone candles failed %s: %s", symbol, exc)
            return []

    async def get_funds(self) -> dict[str, float]:
        try:
            r = await asyncio.to_thread(self._api.rmsLimit)
            d = r.get("data") or {}
            return {"available": float(d.get("availablecash", 0) or 0),
                    "used": float(d.get("utiliseddebits", 0) or 0)}
        except Exception:
            return {}

    async def get_positions(self) -> list[dict[str, Any]]:
        try:
            r = await asyncio.to_thread(self._api.position)
            return r.get("data") or []
        except Exception:
            return []

    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        if not self._api:
            return OrderResult(False, message="not connected")
        found = self._token(instrument.tradingsymbol, instrument.exchange)
        if not found:
            return OrderResult(False, message=f"token not found for {instrument.tradingsymbol}")
        token, tsym = found
        params = {
            "variety": "NORMAL",
            "tradingsymbol": tsym,
            "symboltoken": token,
            "transactiontype": side.value,
            "exchange": instrument.exchange,
            "ordertype": order_type,
            "producttype": "INTRADAY" if product == "MIS" else "DELIVERY",
            "duration": "DAY",
            "price": str(round(price, 2)) if order_type == "LIMIT" else "0",
            "quantity": str(quantity),
        }
        try:
            r = await asyncio.to_thread(self._api.placeOrder, params)
            oid = r if isinstance(r, str) else (r.get("data") or {}).get("orderid", "")
            log.info("angelone order placed: %s", oid)
            return OrderResult(bool(oid), order_id=str(oid), message="placed")
        except Exception as exc:
            return OrderResult(False, message=str(exc))
