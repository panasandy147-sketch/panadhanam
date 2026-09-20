"""Broker selection with a safety net.

If the configured broker can't authenticate, the system does NOT crash and does
NOT silently place orders somewhere unexpected — it falls back to the paper
broker and says so loudly. A failed login should never become a live trade.
"""
from __future__ import annotations

from app.brokers.base import BrokerAdapter
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.registry import autodiscover, get_broker_class, registered_brokers

log = get_logger("broker.factory")

_instance: BrokerAdapter | None = None


async def build_broker(cfg: Config | None = None, force: str | None = None) -> BrokerAdapter:
    cfg = cfg or get_config()
    autodiscover(("app.brokers",))

    name = force or cfg.broker_name
    cls = get_broker_class(name)
    if cls is None:
        log.error("unknown broker '%s' (available: %s) — using paper",
                  name, ", ".join(registered_brokers()))
        cls = get_broker_class("paper")
        name = "paper"

    broker: BrokerAdapter = cls(
        credentials=cfg.broker_credentials(name),
        config={**(cfg.get("derivatives") or {}), "total_capital": cfg.get("risk.total_capital", 100_000)},
    )
    ok = await broker.connect()

    if not ok and name != "paper":
        log.warning("=" * 68)
        log.warning("broker '%s' failed to connect — FALLING BACK TO PAPER MODE.", name)
        log.warning("No live orders will be placed. Check your .env credentials.")
        log.warning("=" * 68)
        paper_cls = get_broker_class("paper")
        broker = paper_cls(config={"total_capital": cfg.get("risk.total_capital", 100_000)})
        await broker.connect()

    return broker


async def get_broker(cfg: Config | None = None) -> BrokerAdapter:
    global _instance
    if _instance is None:
        _instance = await build_broker(cfg)
    return _instance


async def reset_broker() -> None:
    global _instance
    if _instance:
        await _instance.disconnect()
    _instance = None
