"""Zerodha Kite Connect adapter.

Install with:  pip install kiteconnect
Auth flow:
  1. Get your api_key/api_secret from https://kite.trade
  2. Open  https://kite.trade/connect/login?api_key=<KEY>&v=3
  3. Login, copy `request_token` from the redirect URL
  4. Run:   python -m scripts.kite_login <request_token>
     which prints an access_token → paste into .env as KITE_ACCESS_TOKEN
Access tokens expire daily; re-run step 2-4 each morning.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from app.brokers.base import BrokerAdapter, OrderResult
from app.core.logging import get_logger
from app.core.models import Candle, Instrument, OptionChain, OptionLeg, Quote, Side
from app.core.registry import register_broker

log = get_logger("broker.zerodha")

_TF = {"1m": "minute", "3m": "3minute", "5m": "5minute", "15m": "15minute",
       "30m": "30minute", "60m": "60minute", "1h": "60minute", "1d": "day"}


@register_broker("zerodha")
class ZerodhaBroker(BrokerAdapter):
    name = "zerodha"
    supports_options = True
    supports_live_orders = True

    def __init__(self, credentials: dict[str, str] | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, config)
        self._kite: Any = None
        self._instruments: list[dict[str, Any]] = []
        self._token_cache: dict[str, int] = {}

    async def connect(self) -> bool:
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            log.error("kiteconnect not installed — run: pip install kiteconnect")
            return False

        api_key = self.credentials.get("api_key")
        access_token = self.credentials.get("access_token")
        if not api_key or not access_token:
            log.error("KITE_API_KEY / KITE_ACCESS_TOKEN missing from .env")
            return False

        try:
            self._kite = KiteConnect(api_key=api_key)
            self._kite.set_access_token(access_token)
            profile = await asyncio.to_thread(self._kite.profile)
            log.info("zerodha connected as %s", profile.get("user_name", "?"))
            await self._load_instruments()
            self._connected = True
            return True
        except Exception as exc:
            log.error("zerodha connect failed: %s", exc)
            return False

    async def _load_instruments(self) -> None:
        try:
            nfo = await asyncio.to_thread(self._kite.instruments, "NFO")
            nse = await asyncio.to_thread(self._kite.instruments, "NSE")
            self._instruments = list(nfo) + list(nse)
            log.info("loaded %d instruments", len(self._instruments))
        except Exception as exc:
            log.warning("instrument dump failed: %s", exc)

    def _token(self, tradingsymbol: str, exchange: str = "NSE") -> int | None:
        key = f"{exchange}:{tradingsymbol}"
        if key in self._token_cache:
            return self._token_cache[key]
        for inst in self._instruments:
            if inst.get("tradingsymbol") == tradingsymbol and inst.get("exchange") == exchange:
                self._token_cache[key] = inst["instrument_token"]
                return inst["instrument_token"]
        return None

    async def get_quote(self, symbol: str) -> Quote | None:
        if not self._kite:
            return None
        key = f"NSE:{symbol}"
        try:
            data = await asyncio.to_thread(self._kite.quote, [key])
            q = data.get(key)
            if not q:
                return None
            ohlc = q.get("ohlc", {})
            close = ohlc.get("close") or q["last_price"]
            change = (q["last_price"] - close) / close * 100 if close else 0.0
            depth = q.get("depth", {})
            return Quote(
                symbol=symbol, last_price=q["last_price"], change_pct=round(change, 3),
                volume=q.get("volume", 0), oi=q.get("oi"),
                bid=(depth.get("buy") or [{}])[0].get("price"),
                ask=(depth.get("sell") or [{}])[0].get("price"),
            )
        except Exception as exc:
            log.warning("quote failed for %s: %s", symbol, exc)
            return None

    async def get_candles(self, symbol: str, timeframe: str, count: int = 200) -> list[Candle]:
        token = self._token(symbol)
        if not token or not self._kite:
            return []
        interval = _TF.get(timeframe, "5minute")
        minutes = {"minute": 1, "3minute": 3, "5minute": 5, "15minute": 15,
                   "30minute": 30, "60minute": 60, "day": 1440}.get(interval, 5)
        days_needed = max(1, int(count * minutes / 375) + 2)
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=min(days_needed, 60))
        try:
            raw = await asyncio.to_thread(
                self._kite.historical_data, token, from_dt, to_dt, interval)
            return [Candle(ts=r["date"], open=r["open"], high=r["high"],
                           low=r["low"], close=r["close"], volume=r.get("volume", 0))
                    for r in raw][-count:]
        except Exception as exc:
            log.warning("historical_data failed for %s: %s", symbol, exc)
            return []

    async def get_expiries(self, underlying: str) -> list[str]:
        exps = sorted({
            inst["expiry"].isoformat()
            for inst in self._instruments
            if inst.get("name") == underlying.upper() and inst.get("expiry")
            and inst.get("segment") == "NFO-OPT"
        })
        today = datetime.now().date().isoformat()
        return [e for e in exps if e >= today][:6]

    async def get_option_chain(self, underlying: str, expiry: str | None = None) -> OptionChain | None:
        if not self._kite:
            return None
        expiries = await self.get_expiries(underlying)
        if not expiries:
            return None
        exp = expiry or expiries[0]

        legs_meta = [
            inst for inst in self._instruments
            if inst.get("name") == underlying.upper()
            and inst.get("expiry") and inst["expiry"].isoformat() == exp
            and inst.get("instrument_type") in {"CE", "PE"}
        ]
        if not legs_meta:
            return None

        spot_q = await self.get_quote(underlying)
        spot = spot_q.last_price if spot_q else 0.0
        strikes = sorted({m["strike"] for m in legs_meta})
        atm = min(strikes, key=lambda s: abs(s - spot)) if strikes else 0.0
        window = self.config.get("strikes_around_atm", 10)
        step = min((b - a for a, b in zip(strikes, strikes[1:], strict=False)), default=50.0) or 50.0
        keep = {s for s in strikes if abs(s - atm) <= window * step}
        legs_meta = [m for m in legs_meta if m["strike"] in keep]

        keys = [f"NFO:{m['tradingsymbol']}" for m in legs_meta]
        legs: list[OptionLeg] = []
        try:
            # Kite caps quote() at ~500 instruments per call.
            for i in range(0, len(keys), 200):
                data = await asyncio.to_thread(self._kite.quote, keys[i:i + 200])
                for meta in legs_meta:
                    q = data.get(f"NFO:{meta['tradingsymbol']}")
                    if not q:
                        continue
                    legs.append(OptionLeg(
                        strike=meta["strike"], option_type=meta["instrument_type"],
                        ltp=q.get("last_price", 0.0), oi=q.get("oi", 0.0),
                        oi_change=q.get("oi", 0.0) - q.get("oi_day_low", q.get("oi", 0.0)),
                        volume=q.get("volume", 0.0),
                    ))
        except Exception as exc:
            log.warning("option chain fetch failed: %s", exc)
            return None
        return OptionChain(underlying=underlying, spot=spot, expiry=exp, legs=legs)

    async def get_funds(self) -> dict[str, float]:
        try:
            m = await asyncio.to_thread(self._kite.margins, "equity")
            return {"available": m["available"]["live_balance"], "used": m["utilised"]["debits"]}
        except Exception:
            return {}

    async def get_positions(self) -> list[dict[str, Any]]:
        try:
            pos = await asyncio.to_thread(self._kite.positions)
            return pos.get("net", [])
        except Exception:
            return []

    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        if not self._kite:
            return OrderResult(False, message="not connected")
        try:
            order_id = await asyncio.to_thread(
                self._kite.place_order,
                variety=self._kite.VARIETY_REGULAR,
                exchange=instrument.exchange if instrument.instrument_type.value == "EQUITY" else "NFO",
                tradingsymbol=instrument.tradingsymbol,
                transaction_type=side.value,
                quantity=quantity,
                product=product,
                order_type=order_type,
                price=round(price, 2) if order_type == "LIMIT" else None,
                tag=tag[:20] or None,
            )
            log.info("zerodha order placed: %s", order_id)
            return OrderResult(True, order_id=str(order_id), message="placed")
        except Exception as exc:
            log.error("order failed: %s", exc)
            return OrderResult(False, message=str(exc))

    async def cancel_order(self, order_id: str) -> OrderResult:
        try:
            await asyncio.to_thread(self._kite.cancel_order,
                                    variety=self._kite.VARIETY_REGULAR, order_id=order_id)
            return OrderResult(True, order_id=order_id, message="cancelled")
        except Exception as exc:
            return OrderResult(False, message=str(exc))

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        try:
            history = await asyncio.to_thread(self._kite.order_history, order_id)
            return history[-1] if history else {}
        except Exception:
            return {}
