"""Market profiles — everything that differs between India and the US.

A profile is a YAML file in `config/markets/`. Loading one overlays its
session times, universe, news sources, macro tickers and derivatives
conventions onto the base settings, so the rest of the system keeps reading
`cfg.get("system.market_open")` and never learns which market it is on.

Adding a third market is a new YAML file and nothing else.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from app.core.logging import get_logger

log = get_logger("markets")

MARKETS_DIR = Path(__file__).resolve().parents[2] / "config" / "markets"


class MarketProfile:
    """One market's conventions."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data or {}

    # ---------------- identity ----------------
    @property
    def code(self) -> str:
        return str(self.data.get("market", {}).get("code", "??")).upper()

    @property
    def name(self) -> str:
        return str(self.data.get("market", {}).get("name", self.code))

    @property
    def flag(self) -> str:
        return str(self.data.get("market", {}).get("flag", ""))

    @property
    def exchange(self) -> str:
        return str(self.data.get("market", {}).get("exchange", ""))

    @property
    def timezone(self) -> str:
        return str(self.data.get("market", {}).get("timezone", "UTC"))

    @property
    def accent(self) -> str:
        return str(self.data.get("market", {}).get("accent", "#3987e5"))

    # ---------------- money ----------------
    @property
    def currency_symbol(self) -> str:
        return str(self.data.get("currency", {}).get("symbol", "$"))

    @property
    def currency_code(self) -> str:
        return str(self.data.get("currency", {}).get("code", "USD"))

    @property
    def locale(self) -> str:
        return str(self.data.get("currency", {}).get("locale", "en-US"))

    # ---------------- derivatives ----------------
    @property
    def lot_based(self) -> bool:
        """India sizes options in exchange lots; the US uses a 100x contract
        multiplier on single contracts. The risk manager needs to know which."""
        return bool(self.data.get("derivatives", {}).get("lot_based", True))

    @property
    def contract_multiplier(self) -> int:
        return int(self.data.get("derivatives", {}).get("contract_multiplier", 1))

    @property
    def weekly_expiry_weekday(self) -> int:
        return int(self.data.get("derivatives", {}).get("weekly_expiry_weekday", 3))

    def strike_step(self, symbol: str, spot: float) -> float:
        steps = self.data.get("derivatives", {}).get("strike_steps", {}) or {}
        explicit = steps.get(symbol.upper())
        if explicit:
            return float(explicit)
        pct = float(steps.get("default_pct", 1.0)) / 100.0
        raw = max(spot * pct, 0.01)
        # Snap to a sane increment so strikes look like real strikes.
        for candidate in (0.5, 1, 2.5, 5, 10, 25, 50, 100, 250, 500):
            if raw <= candidate:
                return float(candidate)
        return float(round(raw / 100) * 100)

    @property
    def yahoo_suffix(self) -> str:
        return str(self.data.get("fundamentals", {}).get("yahoo_suffix", ""))

    # ---------------- brokers ----------------
    @property
    def supported_brokers(self) -> list[str]:
        return list(self.data.get("brokers", {}).get("supported", ["paper"]))

    @property
    def default_broker(self) -> str:
        return str(self.data.get("brokers", {}).get("default", "paper"))

    # ---------------- session ----------------
    @property
    def session(self) -> dict[str, Any]:
        return self.data.get("session", {}) or {}

    def is_in_session(self) -> bool:
        """Is THIS market trading right now?

        Answered from the profile alone, without activating it — the engine has
        to ask about a market it is not currently running in order to decide
        whether to follow it.
        """
        from app.core import clock

        session = self.session
        if not clock.is_trading_day(self.timezone,
                                    session.get("trading_days", [0, 1, 2, 3, 4])):
            return False
        return clock.is_open(self.timezone,
                             str(session.get("market_open", "09:15")),
                             str(session.get("market_close", "15:30")))

    def minutes_until_close(self) -> int:
        """How much session is left, in minutes. Negative once it has shut."""
        from app.core import clock

        now = clock.market_now(self.timezone)
        close = clock.parse_time(str(self.session.get("market_close", "15:30")),
                                 __import__("datetime").time(15, 30))
        return (close.hour * 60 + close.minute) - (now.hour * 60 + now.minute)

    # ---------------- overlay ----------------
    def apply_to(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of `settings` with this market's values merged in."""
        out = copy.deepcopy(settings)

        session = self.data.get("session", {}) or {}
        system = out.setdefault("system", {})
        for key in ("market_open", "market_close", "premarket_scan_time",
                    "no_new_entry_after", "square_off_time"):
            if key in session:
                system[key] = session[key]
        system["timezone"] = self.timezone
        system["trading_days"] = session.get("trading_days", [0, 1, 2, 3, 4])
        system["market_code"] = self.code

        # Derivatives: keep the base thresholds (IV limits, PCR bands) and
        # layer only the structural conventions on top.
        deriv = out.setdefault("derivatives", {})
        deriv.update(self.data.get("derivatives", {}) or {})

        macro_profile = self.data.get("macro", {}) or {}
        macro = out.setdefault("macro", {})
        macro.update(macro_profile)

        news_profile = self.data.get("news", {}) or {}
        if news_profile.get("sources"):
            out.setdefault("news", {})["sources"] = news_profile["sources"]

        out["currency"] = self.data.get("currency", {})
        out["market"] = self.data.get("market", {})
        return out

    def universe(self) -> dict[str, Any]:
        return self.data.get("universe", {}) or {}

    def describe(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "flag": self.flag,
            "exchange": self.exchange,
            "timezone": self.timezone,
            "accent": self.accent,
            "currency": {"symbol": self.currency_symbol,
                         "code": self.currency_code,
                         "locale": self.locale,
                         "grouping": self.data.get("currency", {}).get("grouping", "western")},
            "session": self.data.get("session", {}),
            "lot_based": self.lot_based,
            "contract_multiplier": self.contract_multiplier,
            "brokers": self.supported_brokers,
            "default_broker": self.default_broker,
            "symbols": len(self.universe().get("indices", []) or [])
                       + len(self.universe().get("stocks", []) or []),
        }


def load_profiles() -> dict[str, MarketProfile]:
    """Load every `config/markets/*.yaml`."""
    profiles: dict[str, MarketProfile] = {}
    if not MARKETS_DIR.exists():
        log.warning("no config/markets directory found")
        return profiles

    for path in sorted(MARKETS_DIR.glob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            profile = MarketProfile(data)
            if profile.code == "??":
                log.warning("%s has no market.code — skipping", path.name)
                continue
            profiles[profile.code] = profile
        except Exception as exc:
            log.error("failed to load market profile %s: %s", path.name, exc)
    return profiles
