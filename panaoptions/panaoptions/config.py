"""Configuration: one YAML file, with .env allowed to override per machine."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from panaoptions import envfile

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "settings.yaml"
PROFILE_DIR = ROOT / "config" / "profiles"
# A second desk on the same machine (the swing desk beside the 0DTE one):
# PANAOPTIONS_DESK_DIR=swing keeps its book, journal and learned weights in
# data/swing, journal/swing and config/learned-swing.yaml, so two desks never
# share a ledger. Unset: the one desk, as before.
DESK = (os.getenv("PANAOPTIONS_DESK_DIR") or "").strip().strip("/\\")
DATA_DIR = ROOT / "data" / DESK if DESK else ROOT / "data"
ENV_PATH = ROOT / ".env"
# What the Friday reflection learned: strategy weights, merged over the
# settings and the profile. Git-ignored, so `git pull` never conflicts with it.
LEARNED_PATH = ROOT / "config" / (f"learned-{DESK}.yaml" if DESK else "learned.yaml")
MARKET_DIR = ROOT / "config" / "markets"
MARKETS = ("US", "IN")


def market_choice_path() -> Path:
    """Where the dashboard's US / India / Auto toggle is remembered."""
    return DATA_DIR / "market.json"


def saved_market_mode() -> str:
    """"US", "IN" or "AUTO": the env var wins, then the saved toggle."""
    import json

    env = (os.getenv("PANAOPTIONS_MARKET") or "").strip().upper()
    if env in (*MARKETS, "AUTO"):
        return env
    try:
        mode = str(json.loads(market_choice_path().read_text(encoding="utf-8"))
                   .get("mode", "US")).upper()
    except (OSError, ValueError, AttributeError):
        mode = "US"
    return mode if mode in (*MARKETS, "AUTO") else "US"


def save_market_mode(mode: str) -> None:
    import json

    path = market_choice_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": mode.upper()}), encoding="utf-8")


def learned_path(market: str = "US") -> Path:
    """Each market learns its own strategy weights."""
    return LEARNED_PATH if market == "US" else \
        LEARNED_PATH.with_name(f"learned-{market.lower()}.yaml")

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


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge `overlay` onto `base`, returning a new dict.

    Deep rather than shallow so a profile can change one strategy's window
    without having to restate every other setting under `strategies:` — and
    so a setting a profile does not mention keeps its default rather than
    silently disappearing.
    """
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def available_profiles() -> list[str]:
    if not PROFILE_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))


class Config:
    def __init__(self, path: Path | None = None,
                 profile: str | None = None, market: str | None = None) -> None:
        self.path = path or CONFIG_PATH
        envfile.load(ENV_PATH)
        self.profile = profile or os.getenv("PANAOPTIONS_PROFILE") or ""
        # Which market's session, symbols, contracts and account are in force.
        # "AUTO" in the saved toggle starts on the US and follows the clock.
        mode = (market or saved_market_mode()).upper()
        self.market = mode if mode in MARKETS else "US"
        self.data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        envfile.load(ENV_PATH)
        with open(self.path, encoding="utf-8") as fh:
            self.data = yaml.safe_load(fh) or {}
        if self.profile:
            self._apply_profile(self.profile)
        # After the profile: a market's session and windows are in its own
        # clock, and must not be left at the profile's New York times.
        if self.market != "US":
            self._apply_market(self.market)
        self._apply_learned()
        self._apply_env()

    def _apply_market(self, code: str) -> None:
        path = MARKET_DIR / f"{code.lower()}.yaml"
        if not path.is_file():
            raise FileNotFoundError(f"no market overlay for {code!r} at {path}")
        with open(path, encoding="utf-8") as fh:
            overlay = yaml.safe_load(fh) or {}
        # A market's universe and lot table replace the US ones outright.
        for key in ("universe",):
            if key in overlay:
                self.data[key] = overlay.pop(key)
        self.data = _merge(self.data, overlay)

    def _apply_learned(self) -> None:
        """Merge config/learned.yaml — only the keys reflection may write."""
        path = learned_path(getattr(self, "market", "US"))
        if not path.is_file():
            return
        try:
            with open(path, encoding="utf-8") as fh:
                learned = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            return
        weights = learned.get("strategy_weights") if isinstance(learned, dict) else None
        if isinstance(weights, dict):
            self.data = _merge(self.data, {"strategy_weights": {
                str(k): float(v) for k, v in weights.items()
                if isinstance(v, int | float)}})

    def _apply_profile(self, name: str) -> None:
        """Layer a profile over the defaults.

        A profile is a different desk, not a tweak — `scalp` reads 1-minute
        bars and buys same-day contracts where the default reads 5m/15m and
        buys 7-45 day ones. Loading one silently would be the worst outcome
        of all, so an unknown name is an error rather than a shrug, and every
        surface that shows the configuration names the profile in force.
        """
        path = PROFILE_DIR / f"{name}.yaml"
        if not path.is_file():
            known = ", ".join(available_profiles()) or "none installed"
            raise FileNotFoundError(
                f"no such profile: {name!r}. Available: {known}. "
                f"Profiles live in {PROFILE_DIR}.")
        with open(path, encoding="utf-8") as fh:
            overlay = yaml.safe_load(fh) or {}
        self.data = _merge(self.data, overlay)

    def _apply_env(self) -> None:
        """Per-machine overrides. Secrets and account size stay out of git."""
        account = self.data.setdefault("account", {})
        # PANAOPTIONS_CAPITAL is the US account (in dollars); India has its
        # own, PANAOPTIONS_CAPITAL_IN, in rupees.
        market = getattr(self, "market", "US")
        capital = _env_float("PANAOPTIONS_CAPITAL" if market == "US"
                             else f"PANAOPTIONS_CAPITAL_{market}")
        if capital is not None:
            account["starting_capital"] = capital

        # Which market data source, per machine. Yahoo's chain endpoint works
        # for some people and returns 401 for others, so this is exactly the
        # kind of setting that belongs beside the machine rather than in a
        # file everyone shares.
        provider = (os.getenv("PANAOPTIONS_PROVIDER") or "").strip().lower()
        if provider and market == "US":
            self.data.setdefault("data", {})["provider"] = provider

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
        """The default contract size. India trades exchange lots, which differ
        by symbol — see lot_size()."""
        return int(self.get("contracts.contract_multiplier", 100))

    def lot_size(self, symbol: str) -> int:
        """Units one contract controls: an NSE lot in India, 100 in the US."""
        lots = self.get("data.lot_sizes") or {}
        return int(lots.get(symbol.upper(), 0) or self.multiplier)

    @property
    def currency(self) -> str:
        return str(self.get("account.currency", "$"))

    @property
    def market_name(self) -> str:
        return str(self.get("market.name", "United States" if self.market == "US"
                            else self.market))

    @property
    def profile_label(self) -> str:
        """What to print wherever the configuration is shown.

        Running the scalp desk while reading a screen that says "default" is
        the kind of confusion that costs money, so this is never blank.
        """
        return self.profile or "default"

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


def get_config(profile: str | None = None) -> Config:
    global _config
    if _config is None:
        _config = Config(profile=profile)
    elif profile is not None and profile != _config.profile:
        _config = Config(profile=profile, market=_config.market)
    return _config
