"""Configuration: one YAML file, with .env allowed to override per machine."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from panaoptions import envfile

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "settings.yaml"
DATA_DIR = ROOT / "data"
ENV_PATH = ROOT / ".env"

# Load .env before anything reads an environment variable. A value exported in
# a shell lasts only for that shell; the file is what survives a restart.
envfile.load(ENV_PATH)


def _env_float(key: str) -> float | None:
    raw = os.getenv(key)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _hhmm(value: str, default: int | None = 0) -> int | None:
    """"HH:MM" as minutes past midnight, so times compare as numbers."""
    hour, sep, minute = value.partition(":")
    if not sep and not hour.strip():
        return default
    try:
        return int(hour) * 60 + int(minute or 0)
    except ValueError:
        return default


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG_PATH
        self.data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        envfile.load(ENV_PATH)
        with open(self.path, encoding="utf-8") as fh:
            self.data = yaml.safe_load(fh) or {}
        self._apply_env()

    def _apply_env(self) -> None:
        """Per-machine overrides. Secrets and account size stay out of git."""
        account = self.data.setdefault("account", {})
        capital = _env_float("PANAOPTIONS_CAPITAL")
        if capital is not None:
            account["starting_capital"] = capital

        notify = self.data.setdefault("notify", {})
        for env_key, cfg_key in (
            ("DISCORD_WEBHOOK_URL", "discord_webhook_url"),
            ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
            ("TELEGRAM_CHAT_ID", "telegram_chat_id"),
        ):
            value = os.getenv(env_key)
            if value:
                notify[cfg_key] = value

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted lookup: cfg.get('risk.stop_loss_pct', 20.0)."""
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # Shorthands used all over the code, so a typo fails at import not at 09:35.
    @property
    def symbols(self) -> list[str]:
        return list(self.get("universe.symbols", []))

    @property
    def timezone(self) -> str:
        return str(self.get("session.timezone", "America/New_York"))

    @property
    def capital(self) -> float:
        return float(self.get("account.starting_capital", 500.0))

    @property
    def multiplier(self) -> int:
        return int(self.get("contracts.contract_multiplier", 100))

    @property
    def last_entry_hhmm(self) -> str:
        """The last moment of the day the desk may open a trade.

        `session.entry_close` is the floor, not the answer. Every strategy also
        carries its own window, and a strategy whose window runs past the
        desk-wide close is dead config: enabled, in window by its own
        reckoning, and never once asked. That is how a pullback strategy
        configured 10:00-13:30 ends up with thirty live minutes.

        So the desk hunts until the last enabled strategy shuts — and never
        past the force-exit time, because a trade opened then has nowhere to
        go but straight back out.
        """
        floor = _hhmm(str(self.get("session.entry_close", "10:30")))
        latest = floor
        for block in (self.get("strategies", {}) or {}).values():
            if not isinstance(block, dict) or not block.get("enabled", True):
                continue
            end = _hhmm(str(block.get("to", "") or ""), default=None)
            if end is not None and end > latest:
                latest = end
        ceiling = _hhmm(str(self.get("session.force_exit_at", "15:45")))
        latest = min(latest, ceiling)
        return f"{latest // 60:02d}:{latest % 60:02d}"


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
