"""Configuration: YAML files + .env, hot-reloadable.

Design rule: nothing tunable is hardcoded in Python. If you want to change a
threshold, a prompt, a watchlist or a weight, you edit `config/*.yaml` and hit
`POST /api/config/reload`.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data" / "runtime"
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env", override=False)

_lock = threading.RLock()


def _env(key: str, default: Any = None) -> Any:
    v = os.getenv(key)
    return default if v is None or v == "" else v


def _env_float(key: str, default: float | None) -> float | None:
    v = os.getenv(key)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Merged view of settings.yaml / agents.yaml / universe.yaml + env overrides."""

    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}
        self.agents: dict[str, Any] = {}
        self.universe: dict[str, Any] = {}
        self.reload()

    # ---------------- loading ----------------
    def reload(self) -> None:
        with _lock:
            self.settings = self._load("settings.yaml")
            self.agents = self._load("agents.yaml")
            self.universe = self._load("universe.yaml")
            self._apply_env_overrides()

    @staticmethod
    def _load(name: str) -> dict[str, Any]:
        path = CONFIG_DIR / name
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def _apply_env_overrides(self) -> None:
        """Env wins over YAML — so secrets and per-machine tuning stay out of git."""
        risk = self.settings.setdefault("risk", {})
        for env_key, cfg_key in (
            ("TOTAL_CAPITAL", "total_capital"),
            ("RISK_PER_TRADE_PCT", "risk_per_trade_pct"),
            ("MAX_DAILY_LOSS_PCT", "max_daily_loss_pct"),
            ("MIN_RISK_REWARD", "min_risk_reward"),
        ):
            val = _env_float(env_key, None)
            if val is not None:
                risk[cfg_key] = val

        execu = self.settings.setdefault("execution", {})
        broker = _env("BROKER")
        if broker:
            execu["broker"] = broker

    # ---------------- accessors ----------------
    def get(self, path: str, default: Any = None) -> Any:
        """Dotted lookup: cfg.get('risk.min_risk_reward', 2.0)."""
        node: Any = self.settings
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def agent(self, agent_id: str) -> dict[str, Any]:
        return self.agents.get(agent_id, {})

    def enabled_analysts(self) -> list[str]:
        """Analyst agents (not cmio/risk/dispatcher) that are switched on."""
        out = []
        for aid, spec in self.agents.items():
            if aid in {"cmio", "risk", "dispatcher"}:
                continue
            if spec.get("enabled", True):
                out.append(aid)
        return out

    def watchlist(self) -> list[dict[str, Any]]:
        black = set(self.universe.get("blacklist") or [])
        items: list[dict[str, Any]] = []
        for idx in self.universe.get("indices") or []:
            d = dict(idx)
            d.setdefault("trading_symbol", d["symbol"])
            d["is_index"] = True
            items.append(d)
        for st in self.universe.get("stocks") or []:
            d = dict(st)
            d.setdefault("trading_symbol", d["symbol"])
            d["is_index"] = False
            items.append(d)
        return [i for i in items if i["symbol"] not in black]

    def instrument_meta(self, symbol: str) -> dict[str, Any]:
        for item in self.watchlist():
            if item["symbol"] == symbol or item.get("trading_symbol") == symbol:
                return item
        return {"symbol": symbol, "trading_symbol": symbol, "lot_size": 1,
                "exchange": "NSE", "tick_size": 0.05, "is_index": False}

    # ---------------- secrets / env ----------------
    @property
    def anthropic_key(self) -> str | None:
        return _env("ANTHROPIC_API_KEY")

    @property
    def llm_model(self) -> str:
        return _env("LLM_MODEL", "claude-opus-5")

    @property
    def llm_effort(self) -> str:
        return _env("LLM_EFFORT", "medium")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_key)

    @property
    def broker_name(self) -> str:
        return str(self.get("execution.broker", "paper"))

    @property
    def trading_mode(self) -> str:
        return _env("TRADING_MODE", "paper")

    @property
    def live_orders_enabled(self) -> bool:
        return _env_bool("ENABLE_LIVE_ORDERS", False) and self.trading_mode == "live"

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "panadhanam.db"

    def broker_credentials(self, broker: str) -> dict[str, str]:
        prefix = {
            "zerodha": "KITE",
            "upstox": "UPSTOX",
            "angelone": "ANGELONE",
        }.get(broker, broker.upper())
        return {
            k.removeprefix(f"{prefix}_").lower(): v
            for k, v in os.environ.items()
            if k.startswith(f"{prefix}_") and v
        }


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        with _lock:
            if _config is None:
                _config = Config()
    return _config


def reload_config() -> Config:
    cfg = get_config()
    cfg.reload()
    return cfg
