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

from datetime import UTC, datetime
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
        self._armed_by: str = ""               # "you" or "auto"
        # Dates the auto-arm has already tried, so a refusal is not retried
        # every cycle for the rest of the day.
        self._auto_attempted: set[str] = set()

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

    async def maybe_auto_arm(self) -> dict[str, Any] | None:
        """Arm by itself when the session opens, on a PAPER account only.

        Real money is deliberately excluded and there is no setting to permit
        it. Committing real capital is a decision a person takes each morning,
        not one a config file takes while they are asleep — the button is
        thirty seconds of work and it is the right thirty seconds.

        Returns the arming result, or None when there was nothing to do.
        """
        if not bool(self.cfg.get("trading_day.auto_arm_on_open", False)):
            return None
        if self.armed or self.today in self._auto_attempted:
            return None
        if self.engine.session_phase() != "open":
            return None

        if not getattr(self.engine.broker, "is_paper_account", True):
            self._auto_attempted.add(self.today)
            log.warning("auto-arm refused: '%s' is a REAL-MONEY account. Arming "
                        "it is a decision you take at the screen — press Start "
                        "trading day.", self.engine.broker.name)
            return None

        self._auto_attempted.add(self.today)
        result = await self.start(by="auto")
        if result.get("armed"):
            log.info("auto-armed for %s at the open — paper account, simulated "
                     "fills only", self.today)
        else:
            log.info("auto-arm declined: %s", result.get("reason"))
        return result

    # ------------------------------------------------------------------ #
    async def start(self, by: str = "you") -> dict[str, Any]:
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
        self._armed_by = by
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
            "armed_by": self._armed_by if self.armed else "",
            "auto_arm": bool(self.cfg.get("trading_day.auto_arm_on_open", False)),
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
        from app.core.explain import why_bought, why_sold
        from app.journal.analytics import compute_analytics
        from app.storage import db

        today = self.today
        signals = [s for s in db.recent_signals(limit=300)
                   if self._local(s.get("ts"))[:10] == today]

        taken = [s for s in signals if s["status"] != "REJECTED"]
        closed = [s for s in taken if s.get("r_multiple") is not None]
        rejected = [s for s in signals if s["status"] == "REJECTED"]

        # Why the desk stayed out, bucketed so the tally stays readable. Two
        # places say no: the risk manager (a REJECTED signal) and, far more
        # often, the CMIO's vote — which never becomes a signal at all, so a
        # day of "not enough confirmations" used to leave this list empty.
        passes = [c for c in db.recent_cycles()
                  if self._local(c.get("ts"))[:10] == today]
        firsts: list[str] = []
        for s in rejected:
            try:
                import json
                firsts.append((json.loads(s.get("rejection_reasons") or "[]") or [""])[0])
            except (ValueError, TypeError):
                firsts.append("")
        firsts += [self._cmio_reason((c.get("rejected") or [""])[0])
                   for c in passes if not c.get("proceeded")]
        reasons: dict[str, int] = {}
        for first in firsts:
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
            "symbols_judged": len(passes),
            "signals_generated": len(signals),
            "trades_taken": len(taken),
            "trades_closed": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": round(wins / len(closed) * 100, 1) if closed else 0.0,
            "total_r": round(total_r, 2),
            "pnl": round(sum(s["pnl"] or 0.0 for s in closed), 2),
            "still_open": len(taken) - len(closed),
            "rejected": len(firsts),
            "top_rejections": sorted(
                ({"reason": k, "count": v} for k, v in reasons.items()),
                key=lambda x: x["count"], reverse=True)[:6],
            "trades": [
                {
                    "id": s["id"],
                    "time": self._local(s.get("ts"))[11:16],
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
                    "why": why_bought(s),
                    "exit_reason": why_sold(s),
                }
                for s in taken
            ],
            "journal": compute_analytics(),
            "risk": self.engine.risk.snapshot(),
        }

    def _local(self, ts: str | None) -> str:
        """A stored UTC timestamp in the market's own zone, as ISO text.

        Rows are written in UTC. Matching them against the market's date by
        the text prefix dropped every trade from the review once the UTC and
        market dates differed — past midnight IST, that was all of them.
        """
        if not ts:
            return ""
        try:
            moment = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return str(ts)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(clock.zone(self.timezone)).isoformat()

    @staticmethod
    def _cmio_reason(rationale: str) -> str:
        """The CMIO's rationale is the whole vote; the reason is the tail."""
        _, found, tail = (rationale or "").partition("Not proceeding: ")
        return tail if found else rationale

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
