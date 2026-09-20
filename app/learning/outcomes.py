"""Outcome tracking — the feedback signal the whole learning loop runs on.

Every approved signal is monitored until it hits its stop, its target, or the
square-off time. That realised R-multiple is what grades each agent's call.
Without this the system would never learn anything; a signal with no recorded
outcome is just an opinion.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.brokers.base import BrokerAdapter
from app.core.bus import Topic, bus
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import SignalStatus
from app.storage import db

log = get_logger("learning.outcomes")


class OutcomeTracker:
    def __init__(self, broker: BrokerAdapter, cfg: Config | None = None,
                 risk_manager: Any = None) -> None:
        self.cfg = cfg or get_config()
        self.broker = broker
        self.risk = risk_manager

    async def poll(self) -> list[dict[str, Any]]:
        """Mark open signals to market and close the ones that resolved."""
        open_rows = db.open_signals()
        if not open_rows:
            return []

        closed: list[dict[str, Any]] = []
        unrealised = 0.0

        for row in open_rows:
            price = await self._current_price(row)
            if price is None:
                continue

            entry = row["entry"]
            stop = row["stop_loss"]
            target = row["target"]
            qty = row["quantity"] or 0
            is_long = row["side"] == "BUY"
            risk_per_unit = abs(entry - stop) or 1.0

            hit_stop = price <= stop if is_long else price >= stop
            hit_target = price >= target if is_long else price <= target
            timed_out = self._past_squareoff()
            time_stop = self._time_stop_hit(row, price)

            if hit_stop or hit_target or timed_out or time_stop:
                # A time-stop exit leaves at MARKET, not at the price stop: the
                # thesis has expired, so there is nothing left to wait for.
                exit_price = (stop if hit_stop
                              else target if hit_target
                              else price)
                direction = 1 if is_long else -1
                pnl = (exit_price - entry) * qty * direction
                r_multiple = (exit_price - entry) * direction / risk_per_unit

                status = (SignalStatus.CLOSED_STOP if hit_stop else
                          SignalStatus.CLOSED_TARGET if hit_target else
                          SignalStatus.CLOSED_TIME)
                if time_stop and not (hit_stop or hit_target):
                    log.info("TIME STOP %s — no bounce within %s min, exiting at market",
                             row["symbol"],
                             self.cfg.get("risk.time_stop_minutes", 30))

                db.update_outcome(row["id"], round(exit_price, 2), round(pnl, 2),
                                  round(r_multiple, 3), status.value)
                closed.append({"signal_id": row["id"], "symbol": row["symbol"],
                               "status": status.value, "pnl": round(pnl, 2),
                               "r_multiple": round(r_multiple, 3),
                               "exit_price": round(exit_price, 2)})

                if self.risk:
                    from app.core.models import TradeSignal
                    try:
                        import json
                        sig = TradeSignal.model_validate(json.loads(row["payload"]))
                        self.risk.register_close(sig, pnl)
                    except Exception as exc:
                        log.debug("risk close bookkeeping failed: %s", exc)

                log.info("CLOSED %s %s @ %.2f → %s (%.2fR, ₹%.0f)",
                         row["symbol"], row["side"], exit_price, status.value,
                         r_multiple, pnl)
                await bus.publish(Topic.POSITION_UPDATE, closed[-1])
            else:
                direction = 1 if is_long else -1
                unrealised += (price - entry) * qty * direction

        if self.risk:
            self.risk.set_unrealised(round(unrealised, 2))
            await bus.publish(Topic.RISK_STATE, self.risk.snapshot())

        return closed

    async def _current_price(self, row: dict[str, Any]) -> float | None:
        """Mark to market.

        An option is marked by moving its premium with the underlying, scaled by
        the delta recorded at entry. That is an approximation — it ignores theta
        and IV shifts — but it is applied consistently to every trade, so the
        R-multiples the learning loop grades on stay comparable. In live mode the
        broker's own position P&L supersedes this.
        """
        quote = await self.broker.get_quote(row["symbol"])
        if not quote:
            return None

        if row["instrument_type"] not in {"CE", "PE"}:
            return quote.last_price

        entry_spot = row.get("entry_spot")
        if not entry_spot:
            return None      # cannot mark it honestly — leave the position open
        delta = abs(row.get("entry_delta") or 0.5)
        sign = 1 if row["instrument_type"] == "CE" else -1
        move = (quote.last_price - entry_spot) * delta * sign
        return max(row["entry"] + move, 0.05)

    def _time_stop_hit(self, row: dict[str, Any], price: float) -> bool:
        """Has a mean-reversion entry failed to bounce in the allotted time?

        A reversion trade is a bet that buyers are absorbing supply. If the
        price has not moved in your favour within the window, the bet is wrong
        — a dead bounce means supply is still in control. Waiting for the price
        stop just pays more for the same information.
        """
        minutes = float(self.cfg.get("risk.time_stop_minutes", 0) or 0)
        if minutes <= 0:
            return False

        setups = self.cfg.get("risk.time_stop_setups") or ["Mean Reversion"]
        payload = row.get("payload")
        setup = ""
        if payload:
            try:
                import json
                setup = (json.loads(payload).get("setup") or "")
            except Exception:
                setup = ""
        # Only applies to the setups the timer is configured for.
        if setup and setup not in setups:
            return False

        entry_ts = row.get("ts")
        if not entry_ts:
            return False
        try:
            opened = datetime.fromisoformat(str(entry_ts))
        except ValueError:
            return False
        if opened.tzinfo is not None:
            opened = opened.replace(tzinfo=None)

        held = (datetime.now() - opened).total_seconds() / 60.0
        if held < minutes:
            return False

        # Only exit if it has NOT moved in your favour. A trade that is working
        # is left alone — the time stop kills dead trades, not winners.
        is_long = row["side"] == "BUY"
        entry = row["entry"]
        in_favour = price > entry if is_long else price < entry
        return not in_favour

    def _past_squareoff(self) -> bool:
        cutoff = str(self.cfg.get("system.square_off_time", "15:15"))
        try:
            h, m = (int(x) for x in cutoff.split(":"))
        except ValueError:
            return False
        now = datetime.now()
        return (now.hour, now.minute) >= (h, m)
