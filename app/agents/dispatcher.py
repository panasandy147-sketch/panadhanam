"""Order & Signal Dispatcher.

Broadcasts approved signals to the dashboard and, only when explicitly enabled,
to the broker. Three independent switches must ALL be on before a real order is
sent — that is intentional friction:

    execution.auto_place_orders = true      (config/settings.yaml)
    TRADING_MODE=live                       (.env)
    ENABLE_LIVE_ORDERS=true                 (.env)
"""
from __future__ import annotations

from typing import Any

import httpx

from app.brokers.base import BrokerAdapter
from app.core.bus import Topic, bus
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import SignalStatus, TradeSignal
from app.core.registry import register_agent

log = get_logger("agent.dispatcher")


@register_agent("dispatcher")
class Dispatcher:
    agent_id = "dispatcher"

    def __init__(self, broker: BrokerAdapter, cfg: Config | None = None,
                 trading_day: Any = None) -> None:
        self.cfg = cfg or get_config()
        self.broker = broker
        # Set by the engine. When present, the session must be explicitly armed
        # for today before any order is sent.
        self.trading_day = trading_day

    async def dispatch(self, signal: TradeSignal) -> dict[str, Any]:
        if signal.status == SignalStatus.REJECTED:
            await bus.publish(Topic.SIGNAL_REJECTED, signal)
            return {"dispatched": False, "reason": "rejected by risk"}

        await bus.publish(Topic.SIGNAL_APPROVED, signal)
        log.info("ALERT %s", signal.alert_line())

        result: dict[str, Any] = {"dispatched": True, "alert": signal.alert_line(),
                                  "order": None}

        await self._external_alert(signal)

        if self._live_allowed():
            order = await self.broker.place_signal(
                signal,
                order_type=self.cfg.get("execution.order_type", "LIMIT"),
                product=self.cfg.get("execution.product", "MIS"),
            )
            result["order"] = order.dict()
            if order.ok:
                signal.status = SignalStatus.OPEN
                log.info("order placed for %s → %s", signal.id, order.order_id)
            else:
                log.error("order FAILED for %s: %s", signal.id, order.message)
            await bus.publish(Topic.POSITION_UPDATE,
                              {"signal_id": signal.id, "order": order.dict()})
        else:
            log.info("alert-only mode — no order placed for %s", signal.id)
            result["order"] = {"ok": False, "message": "alert-only mode (no order placed)"}

        return result

    def _live_allowed(self) -> bool:
        """May this signal become an actual order?

        A simulator account and a real-money account are different questions.
        Sending an order to Alpaca's paper endpoint IS the practice — gating it
        behind the real-money switches would make practising impossible. Only a
        broker pointed at real money needs the full three-switch guard.
        """
        auto = bool(self.cfg.get("execution.auto_place_orders", False))
        if not auto:
            return False

        # A config flag left on from last week must not trade today's market.
        # Arming is a decision taken each morning and expires with the session.
        if self.trading_day is not None and not self.trading_day.armed:
            log.info("signal alert-only — the trading day is not armed. "
                     "Press Start trading day to place orders.")
            return False

        if getattr(self.broker, "is_paper_account", True):
            log.info("placing SIMULATED order via %s (paper account — no real money)",
                     self.broker.name)
            return True

        if not self.cfg.live_orders_enabled:
            log.warning("auto_place_orders is on and '%s' is a REAL-MONEY account, "
                        "but TRADING_MODE/ENABLE_LIVE_ORDERS are not set to live "
                        "— staying in alert-only mode", self.broker.name)
            return False
        return True

    async def _external_alert(self, signal: TradeSignal) -> None:
        """Optional Telegram / webhook relay. Never allowed to break a cycle."""
        alerts = self.cfg.get("alerts", {}) or {}
        text = (f"🔔 {signal.alert_line()}\n"
                f"Qty {signal.quantity} ({signal.lots} lot(s)) | "
                f"Risk ₹{signal.total_risk:,.0f} ({signal.capital_at_risk_pct:.2f}%)\n"
                f"{signal.rationale[:300]}")

        token = alerts.get("telegram_bot_token")
        chat_id = alerts.get("telegram_chat_id")
        webhook = alerts.get("webhook_url")

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                if token and chat_id:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": text})
                if webhook:
                    await client.post(webhook, json={
                        "text": text,
                        "signal": signal.model_dump(mode="json"),
                    })
        except Exception as exc:
            log.debug("external alert failed: %s", exc)
