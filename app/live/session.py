"""Trading Day — an explicit, per-session arm/disarm for live order placement.

Leaving `auto_place_orders: true` in a config file means the desk will trade on
any day the app happens to be open, including days you only meant to watch. A
trading day is a decision you make each morning, so it belongs behind a button
rather than a setting you last touched a week ago.

Arming is deliberately narrow:
  * it applies to ONE session date and expires with it
  * it survives neither a restart nor midnight
  * it disarms itself at square-off time

The desk still runs and still produces signals when disarmed — you simply get
alerts instead of orders.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core import clock
from app.core.bus import bus
from app.core.config import Config, get_config
from app.core.logging import get_logger

log = get_logger("live.session")


class TradingDay:
    """Tracks, and gates, one live session."""

    def __init__(self, engine: Any, cfg: Config | None = None) -> None:
        self.engine = engine
        self.cfg = cfg or get_config()
        self._armed_for: str | None = None     # ISO date this arming covers
        self._armed_at: datetime | None = None
        self._disarm_reason: str = ""

    # ------------------------------------------------------------------ #
    @property
    def timezone(self) -> str:
        return str(self.cfg.get("system.timezone", "Asia/Kolkata"))

    @property
    def today(self) -> str:
        return clock.market_now(self.timezone).date().isoformat()

    @property
    def armed(self) -> bool:
        """Armed only for today's session, and only before square-off."""
        if self._armed_for != self.today:
            return False
        if clock.past(self.timezone,
                      str(self.cfg.get("system.square_off_time", "15:15"))):
            if self._armed_for:
                self._auto_disarm("square-off time reached")
            return False
        return True

    def _auto_disarm(self, reason: str) -> None:
        log.info("trading day disarmed: %s", reason)
        self._armed_for = None
        self._disarm_reason = reason

    # ------------------------------------------------------------------ #
    async def start(self) -> dict[str, Any]:
        phase = self.engine.session_phase()

        if self.engine.risk.state.halted:
            return {"armed": False,
                    "reason": (f"The desk is halted: {self.engine.risk.state.halt_reason}. "
                               f"Clear it deliberately before arming.")}

        if clock.past(self.timezone,
                      str(self.cfg.get("system.square_off_time", "15:15"))):
            return {"armed": False,
                    "reason": ("Past square-off time for this session — there is "
                               "no day left to trade. Try again tomorrow.")}

        if not getattr(self.engine.broker, "is_paper_account", True) \
                and not self.cfg.live_orders_enabled:
            return {"armed": False,
                    "reason": (f"'{self.engine.broker.name}' is a REAL-MONEY account "
                               f"and the live-order switches are off. Set "
                               f"TRADING_MODE=live and ENABLE_LIVE_ORDERS=true "
                               f"only when you mean it.")}

        self._armed_for = self.today
        self._armed_at = datetime.now()
        self._disarm_reason = ""
        self.engine.paused = False

        paper = getattr(self.engine.broker, "is_paper_account", True)
        log.info("TRADING DAY ARMED for %s — %s account via %s%s",
                 self.today, "paper" if paper else "REAL MONEY",
                 self.engine.broker.name,
                 "" if phase == "open" else f" (market is {phase})")

        await bus.publish("trading_day.state", self.status())
        return {"armed": True, "status": self.status()}

    async def stop(self) -> dict[str, Any]:
        self._auto_disarm("stopped by you")
        await bus.publish("trading_day.state", self.status())
        return self.status()

    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        risk = self.engine.risk.snapshot()
        phase = self.engine.session_phase()
        paper = getattr(self.engine.broker, "is_paper_account", True)

        return {
            "armed": self.armed,
            "armed_for": self._armed_for,
            "armed_at": self._armed_at.isoformat() if self._armed_at else None,
            "disarm_reason": self._disarm_reason,
            "today": self.today,
            "phase": phase,
            "market_time": clock.market_now(self.timezone).strftime("%H:%M"),
            "broker": self.engine.broker.name,
            "is_paper_account": paper,
            "orders_will_be_placed": self.armed and bool(
                self.cfg.get("execution.auto_place_orders", False)),
            "capital": risk["capital"],
            "currency": risk.get("currency", ""),
            "day_pnl": risk["daily_pnl"],
            "trades_today": risk["trades_today"],
            "wins": risk["wins_today"],
            "losses": risk["losses_today"],
            "open_positions": risk["open_positions"],
            "room_before_halt": risk["remaining_loss_budget"],
            "halted": risk["halted"],
        }

    # ------------------------------------------------------------------ #
    def report(self) -> dict[str, Any]:
        """Everything that happened this session, for the end-of-day read."""
        from app.journal.analytics import compute_analytics
        from app.storage import db

        today = self.today
        signals = [s for s in db.recent_signals(limit=300)
                   if (s.get("ts") or "").startswith(today)]

        taken = [s for s in signals if s["status"] != "REJECTED"]
        closed = [s for s in taken if s.get("r_multiple") is not None]
        rejected = [s for s in signals if s["status"] == "REJECTED"]

        # Why the desk stayed out, bucketed so the tally stays readable.
        reasons: dict[str, int] = {}
        for s in rejected:
            try:
                import json
                first = (json.loads(s.get("rejection_reasons") or "[]") or [""])[0]
            except (ValueError, TypeError):
                first = ""
            key = self._bucket(first)
            reasons[key] = reasons.get(key, 0) + 1

        total_r = sum(s["r_multiple"] or 0.0 for s in closed)
        wins = sum(1 for s in closed if (s["r_multiple"] or 0) > 0)

        return {
            "date": today,
            "market": self.cfg.active_market,
            "phase": self.engine.session_phase(),
            "broker": self.engine.broker.name,
            "is_paper_account": getattr(self.engine.broker, "is_paper_account", True),
            "data_source": self.engine.data_provenance(),
            "signals_generated": len(signals),
            "trades_taken": len(taken),
            "trades_closed": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else 0.0,
            "total_r": round(total_r, 2),
            "pnl": round(sum(s["pnl"] or 0.0 for s in closed), 2),
            "still_open": len(taken) - len(closed),
            "rejected": len(rejected),
            "top_rejections": sorted(
                ({"reason": k, "count": v} for k, v in reasons.items()),
                key=lambda x: x["count"], reverse=True)[:6],
            "trades": [
                {
                    "time": (s.get("ts") or "")[11:16],
                    "symbol": s["symbol"],
                    "instrument": s.get("tradingsymbol"),
                    "side": s["side"],
                    "entry": s["entry"],
                    "stop": s["stop_loss"],
                    "target": s["target"],
                    "exit": s.get("exit_price"),
                    "quantity": s["quantity"],
                    "status": s["status"],
                    "r_multiple": s.get("r_multiple"),
                    "pnl": s.get("pnl"),
                }
                for s in taken
            ],
            "journal": compute_analytics(),
            "risk": self.engine.risk.snapshot(),
        }

    @staticmethod
    def _bucket(reason: str) -> str:
        low = (reason or "").lower()
        if "confirmation" in low:
            return "Not enough confirmations"
        if "neutral band" in low or "conviction" in low:
            return "Conviction below threshold"
        if "lot" in low or "share" in low or "sizes to" in low:
            return "Position sizes to zero (capital too small)"
        if "r:r" in low or "reward" in low:
            return "Risk:reward below minimum"
        if "stop" in low:
            return "Stop too tight or too wide"
        if "cutoff" in low or "halt" in low or "open positions" in low:
            return "Desk closed (cutoff, halt, or full)"
        if "exposure" in low:
            return "Exposure limit reached"
        return reason[:60] or "Unspecified"
