"""The broker contract.

Everything above this line in the stack (agents, risk, dashboard) only ever sees
`BrokerAdapter`. Supporting a new broker means implementing these ~8 methods —
nothing else in the system changes.
"""
from __future__ import annotations

import abc
from datetime import datetime
from typing import Any

from app.core.models import Candle, Instrument, OptionChain, Quote, Side, TradeSignal


class BrokerError(RuntimeError):
    pass


class NotAuthenticated(BrokerError):
    pass


class OrderResult:
    def __init__(self, ok: bool, order_id: str = "", message: str = "",
                 raw: dict[str, Any] | None = None) -> None:
        self.ok = ok
        self.order_id = order_id
        self.message = message
        self.raw = raw or {}

    def dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "order_id": self.order_id,
                "message": self.message, "raw": self.raw}


class BrokerAdapter(abc.ABC):
    """Base class for every broker integration."""

    name: str = "base"
    supports_options: bool = True
    supports_live_orders: bool = False

    def __init__(self, credentials: dict[str, str] | None = None,
                 config: dict[str, Any] | None = None) -> None:
        self.credentials = credentials or {}
        self.config = config or {}
        self._connected = False

    # ----------------------- lifecycle -----------------------
    @abc.abstractmethod
    async def connect(self) -> bool:
        """Authenticate. Must be idempotent and must not raise on bad creds —
        return False so the system can degrade to paper mode instead of dying."""

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ----------------------- market data -----------------------
    @abc.abstractmethod
    async def get_quote(self, symbol: str) -> Quote | None:
        ...

    @abc.abstractmethod
    async def get_candles(self, symbol: str, timeframe: str, count: int = 200) -> list[Candle]:
        """Most recent `count` candles, oldest first."""

    async def get_option_chain(self, underlying: str, expiry: str | None = None) -> OptionChain | None:
        """Optional — return None if the broker has no chain endpoint."""
        return None

    async def get_expiries(self, underlying: str) -> list[str]:
        return []

    # ----------------------- account -----------------------
    async def get_funds(self) -> dict[str, float]:
        return {}

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    # ----------------------- orders -----------------------
    @abc.abstractmethod
    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        ...

    async def cancel_order(self, order_id: str) -> OrderResult:
        return OrderResult(False, message="cancel not supported by this adapter")

    async def modify_order(self, order_id: str, **kwargs: Any) -> OrderResult:
        return OrderResult(False, message="modify not supported by this adapter")

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        return {}

    # ----------------------- helpers -----------------------
    async def place_signal(self, signal: TradeSignal, order_type: str = "LIMIT",
                           product: str = "MIS") -> OrderResult:
        """Convenience: turn an approved TradeSignal into a broker order."""
        return await self.place_order(
            instrument=signal.instrument,
            side=signal.side,
            quantity=signal.quantity,
            price=signal.entry,
            order_type=order_type,
            product=product,
            stop_loss=signal.stop_loss,
            tag=signal.id[:20],
        )

    def health(self) -> dict[str, Any]:
        return {
            "broker": self.name,
            "connected": self._connected,
            "supports_options": self.supports_options,
            "supports_live_orders": self.supports_live_orders,
            "checked_at": datetime.now().isoformat(),
        }
