"""Plugin registry.

The extension seam of the whole system. Adding a broker or an analyst is a
decorator and a YAML line — nothing else in the codebase has to know about it.

    @register_agent("twitter_sentiment")
    class TwitterAgent(BaseAgent): ...

    @register_broker("fyers")
    class FyersBroker(BrokerAdapter): ...
"""
from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from typing import Any, TypeVar

from app.core.logging import get_logger

log = get_logger("registry")

T = TypeVar("T")

_AGENTS: dict[str, type] = {}
_BROKERS: dict[str, type] = {}
_FEEDS: dict[str, type] = {}


def register_agent(agent_id: str) -> Callable[[type[T]], type[T]]:
    def deco(cls: type[T]) -> type[T]:
        _AGENTS[agent_id] = cls
        return cls
    return deco


def register_broker(name: str) -> Callable[[type[T]], type[T]]:
    def deco(cls: type[T]) -> type[T]:
        _BROKERS[name] = cls
        return cls
    return deco


def register_feed(name: str) -> Callable[[type[T]], type[T]]:
    def deco(cls: type[T]) -> type[T]:
        _FEEDS[name] = cls
        return cls
    return deco


def get_agent_class(agent_id: str) -> type | None:
    return _AGENTS.get(agent_id)


def get_broker_class(name: str) -> type | None:
    return _BROKERS.get(name)


def get_feed_class(name: str) -> type | None:
    return _FEEDS.get(name)


def registered_agents() -> dict[str, type]:
    return dict(_AGENTS)


def registered_brokers() -> dict[str, type]:
    return dict(_BROKERS)


def autodiscover(packages: tuple[str, ...] = ("app.agents", "app.brokers", "app.data")) -> None:
    """Import every module in these packages so decorators run.

    A broker whose SDK isn't installed simply fails to import and is skipped —
    that's why you can run the whole system with zero broker packages present.
    """
    for pkg_name in packages:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError as exc:  # pragma: no cover
            log.warning("cannot import package %s: %s", pkg_name, exc)
            continue
        for mod in pkgutil.iter_modules(pkg.__path__):
            if mod.name.startswith("_"):
                continue
            full = f"{pkg_name}.{mod.name}"
            try:
                importlib.import_module(full)
            except ImportError as exc:
                log.info("skipping %s (optional dependency missing: %s)", full, exc)
            except Exception as exc:  # pragma: no cover
                log.error("failed importing %s: %s", full, exc)


def load_from_path(dotted: str) -> Any:
    """Import 'app.agents.candlestick' style paths on demand."""
    return importlib.import_module(dotted)
