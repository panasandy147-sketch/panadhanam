"""Outcome tracking — the feedback signal the whole learning loop runs on.

Every approved signal is monitored until it hits its stop, its target, or the
square-off time. That realised R-multiple is what grades each agent's call.
Without this the system would never learn anything; a signal with no recorded
outcome is just an opinion.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.brokers.base import BrokerAdapter
from app.core.bus import Topic, bus
from app.core.config import Config, get_config
from app.core.explain import why_sold
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
                closed.append({"event": "closed",
                               "signal_id": row["id"], "symbol": row["symbol"],
                               "side": row["side"],
                               "status": status.value, "pnl": round(pnl, 2),
                               "r_multiple": round(r_multiple, 3),
                               "exit_price": round(exit_price, 2),
                               "exit_reason": why_sold({
                                   **row, "status": status.value,
                                   "exit_detail": ("square_off" if timed_out
                                                   else "time_stop"),
                                   "exit_price": round(exit_price, 2),
                                   "r_multiple": round(r_multiple, 3)})})

                if self.risk:
                    from app.core.models import TradeSignal
                    try:
                        import json
                        sig = TradeSignal.model_validate(json.loads(row["payload"]))
                        self.risk.register_close(sig, pnl)
                    except Exception as exc:
                        log.debug("risk close bookkeeping failed: %s", exc)

                cur = self.cfg.market.currency_symbol
                log.info("CLOSED %s %s @ %.2f → %s (%.2fR, %s%.0f)",
                         row["symbol"], row["side"], exit_price, status.value,
                         r_multiple, cur, pnl)
                await bus.publish(Topic.POSITION_UPDATE, closed[-1])

                # Every completed trade gets graded, automatically. A P&L
                # number alone teaches nothing; the card is the learning.
                await self._journal(row, exit_price, status.value)
            else:
                direction = 1 if is_long else -1
                unrealised += (price - entry) * qty * direction

        if self.risk:
            self.risk.set_unrealised(round(unrealised, 2))
            await bus.publish(Topic.RISK_STATE, self.risk.snapshot())

        return closed

    async def _journal(self, row: dict[str, Any], exit_price: float,
                       outcome: str) -> None:
        """Write a closed trade into the journal and grade it.

        Never allowed to break the polling loop: a journalling failure must not
        stop positions being marked or closed.
        """
        if not bool(self.cfg.get("journal.auto_log_live_trades", True)):
            return
        try:
            from datetime import datetime as _dt

            from app.journal import store
            from app.journal.models import JournalEntry
            from app.journal.postmortem import PostMortemEngine

            store.init_journal()

            def _ts(value: Any) -> _dt | None:
                if not value:
                    return None
                try:
                    parsed = _dt.fromisoformat(str(value))
                    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
                except ValueError:
                    return None

            entry = JournalEntry(
                id=f"LIVE-{row['id']}",
                market=self.cfg.active_market,
                symbol=row["symbol"],
                instrument=row.get("tradingsymbol") or row["symbol"],
                setup=self._infer_setup(row),
                side=row["side"],
                planned_entry=row["entry"],
                planned_stop=row["stop_loss"],
                planned_target=row["target"],
                planned_quantity=row["quantity"] or 0,
                actual_entry=row["entry"],
                actual_exit=round(exit_price, 2),
                actual_quantity=row["quantity"] or 0,
                entry_ts=_ts(row.get("ts")),
                exit_ts=_dt.now(),
                notes=(f"Auto-logged from a live signal. Exit: {outcome}. "
                       f"{(row.get('rationale') or '')[:200]}"),
                context={
                    "capital": self.risk.state.capital if self.risk else 0.0,
                    "regime": row.get("regime"),
                    "auto_logged": True,
                    # The desk always honours its own stop, so a time-based or
                    # square-off exit is correct behaviour, not an early exit.
                    "time_stop_hit": outcome in {"CLOSED_TIME", "SQUARE_OFF"},
                },
                signal_id=row["id"],
            )

            card = await PostMortemEngine(self.cfg).build(entry)
            entry.execution_score = card.execution_score
            store.save_entry(entry)
            store.save_card(card)
            store.export_summary()
            log.info("journalled %s → %s (%d/10)", row["symbol"],
                     card.verdict.value, card.execution_score)
        except Exception as exc:
            log.warning("could not journal %s: %s", row.get("id"), exc)

    @staticmethod
    def _infer_setup(row: dict[str, Any]) -> Any:
        """Best-effort setup tag from what the desk recorded at entry."""
        from app.journal.models import SetupType
        blob = f"{row.get('confirmations') or ''} {row.get('rationale') or ''}".lower()
        if "breakout" in blob or "flag" in blob:
            return SetupType.BREAKOUT
        if "vwap" in blob or "reversion" in blob or "oversold" in blob:
            return SetupType.MEAN_REVERSION
        if "news" in blob or "sentiment" in blob:
            return SetupType.NEWS_MOMENTUM
        return SetupType.OTHER

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
        # Rows are written in UTC. Measured against the machine's local clock
        # instead, a trade on a PC in New York looked four hours younger than
        # it was, and the time stop never fired.
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        held = (datetime.now(UTC) - opened).total_seconds() / 60.0
        if held < minutes:
            return False

        # Only exit if it has NOT moved in your favour. A trade that is working
        # is left alone — the time stop kills dead trades, not winners.
        is_long = row["side"] == "BUY"
        entry = row["entry"]
        in_favour = price > entry if is_long else price < entry
        return not in_favour

    def _past_squareoff(self) -> bool:
        """Is it past square-off on the MARKET's clock?

        This used the computer's own clock. On a UTC machine every US trade
        was squared off the moment it opened; on a PC in New York the Indian
        15:15 square-off (05:45 ET) never came at all.
        """
        from app.core import clock

        cutoff = str(self.cfg.get("system.square_off_time", "15:15"))
        timezone = str(self.cfg.get("system.timezone", "Asia/Kolkata"))
        return clock.past(timezone, cutoff)
