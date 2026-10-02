"""Outcome tracking — the feedback signal the whole learning loop runs on.

Every approved signal is monitored until it hits its stop, its target, or the
square-off time. That realised R-multiple is what grades each agent's call.
Without this the system would never learn anything; a signal with no recorded
outcome is just an opinion.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.agents import risk_manager as guard
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
        # Set by the engine: pyramid adds go to the broker through the same
        # gate as entries (armed trading day, paper or live switches).
        self.dispatcher: Any = None

    async def poll(self) -> list[dict[str, Any]]:
        """Mark open signals to market and close the ones that resolved."""
        open_rows = db.open_signals()
        if not open_rows:
            return []

        closed: list[dict[str, Any]] = []
        unrealised = 0.0
        still_open: list[tuple[dict[str, Any], float]] = []

        from app.agents import pyramid
        pyramiding = pyramid.enabled(self.cfg)
        for row in open_rows:
            marks = await self._marks(row)
            if marks is None:
                continue
            price, spot = marks
            if pyramiding:
                row = self._pyramid_start(row)

            entry = row["entry"]
            stop = row["stop_loss"]
            target = row["target"]
            qty = row["quantity"] or 0
            is_long = row["side"] == "BUY"

            # An option's stop is its UNDERLYING's structural level: the call
            # is out when the stock closes through it, whatever the premium
            # is doing. Rows from before the level was recorded keep the old
            # premium comparison.
            ustop = self._underlying_stop(row)
            if ustop is not None and spot is not None:
                is_call = row["instrument_type"] == "CE"
                hit_stop = guard.underlying_stop_hit(spot, ustop, is_call)
            else:
                hit_stop = price <= stop if is_long else price >= stop
            hit_target = price >= target if is_long else price <= target
            swing = self._is_swing(row)
            # The gold desk holds overnight: the square-off closes it only
            # once it has been held swing.max_hold_days sessions.
            timed_out = self._past_squareoff() and (
                not swing or self._held_sessions(row) >= int(
                    self.cfg.get("swing.max_hold_days", 4) or 0))
            time_stop = False if swing else self._time_stop_hit(row, price)
            if swing and not (hit_stop or hit_target) and self._first_profitable_open(
                    row, price, is_long):
                closed.append(await self._close(row, price, SignalStatus.CLOSED_TIME,
                                                "first_profitable_open"))
                continue

            if hit_stop or hit_target or timed_out or time_stop:
                # A time-stop exit leaves at MARKET, not at the price stop: the
                # thesis has expired, so there is nothing left to wait for.
                exit_price = (stop if hit_stop
                              else target if hit_target
                              else price)
                status = (SignalStatus.CLOSED_STOP if hit_stop else
                          SignalStatus.CLOSED_TARGET if hit_target else
                          SignalStatus.CLOSED_TIME)
                if time_stop and not (hit_stop or hit_target):
                    log.info("TIME STOP %s — no bounce within %s min, exiting at market",
                             row["symbol"],
                             self.cfg.get("risk.time_stop_minutes", 30))
                # What closed it, as recorded in the audit's exit_detail: a
                # stop or target hit was labelled "time_stop" (1 Oct, India).
                closed.append(await self._close(
                    row, exit_price, status,
                    "stop" if hit_stop else "target" if hit_target
                    else "square_off" if timed_out else "time_stop"))
            else:
                direction = 1 if is_long else -1
                if self.risk is not None:
                    self.risk.position_pnl[str(row["symbol"]).upper()] = round(
                        (price - entry) * qty * direction, 2)
                row = self._own_stop_step(row, price, spot)
                if pyramiding:
                    row = await self._pyramid_step(row, price, spot)
                    entry, qty = row["entry"], row["quantity"] or 0
                unrealised += (price - entry) * qty * direction
                still_open.append((row, price))

        if self.risk:
            self.risk.set_unrealised(round(unrealised, 2))
            closed += await self._maybe_trip_breaker(still_open)
            await bus.publish(Topic.RISK_STATE, self.risk.snapshot())

        return closed

    async def _maybe_trip_breaker(self, still_open: list[tuple[dict[str, Any], float]]
                                  ) -> list[dict[str, Any]]:
        """The daily circuit breaker: at the loss limit, stop everything.

        Realised plus open P&L at or below -`risk.max_daily_loss_pct`: every
        open position is closed at market, the desk is halted and the trading
        day disarmed for the rest of the session. It does not wait for the
        next entry to be refused — an open book can keep losing after the
        limit, and "no new trades" alone would let it.
        """
        state = self.risk.state
        if not bool(self.cfg.get("risk.circuit_breaker", True)):
            return []
        if state.daily_loss_limit <= 0 or state.daily_pnl > -state.daily_loss_limit:
            return []
        first = not state.halted
        self.risk.lock(f"Daily circuit breaker: P&L {state.daily_pnl:,.0f} hit the "
                       f"-{state.daily_loss_limit:,.0f} limit — flat and locked "
                       f"for the rest of the day")
        out = [await self._close(row, price, SignalStatus.CLOSED_TIME, "circuit_breaker")
               for row, price in still_open]
        self.risk.set_unrealised(0.0)
        if first:
            log.warning("CIRCUIT BREAKER — %s", state.halt_reason)
            day = getattr(self, "trading_day", None)
            if day is not None:
                day._auto_disarm(state.halt_reason)
                await bus.publish("trading_day.state", day.status())
        return out

    async def _close(self, row: dict[str, Any], exit_price: float,
                     status: SignalStatus, detail: str) -> dict[str, Any]:
        entry = row["entry"]
        qty = row["quantity"] or 0
        direction = 1 if row["side"] == "BUY" else -1
        risk_per_unit = abs(entry - row["stop_loss"]) or 1.0
        pnl = (exit_price - entry) * qty * direction
        r_multiple = (exit_price - entry) * direction / risk_per_unit
        # A pyramided position is measured against the BASE trade's risk:
        # after the stop moves, entry-to-stop is no longer what was risked.
        state = self._pyramid_state(row)
        if state is not None:
            from app.agents import pyramid
            r_multiple = pyramid.base_r_multiple(state, pnl)
        if self.risk is not None:
            self.risk.position_pnl.pop(str(row["symbol"]).upper(), None)

        db.update_outcome(row["id"], round(exit_price, 2), round(pnl, 2),
                          round(r_multiple, 3), status.value)
        event = {"event": "closed",
                 "signal_id": row["id"], "symbol": row["symbol"],
                 "side": row["side"],
                 "status": status.value, "pnl": round(pnl, 2),
                 "r_multiple": round(r_multiple, 3),
                 "exit_price": round(exit_price, 2), "exit_detail": detail,
                 "exit_reason": why_sold({
                     **row, "status": status.value, "exit_detail": detail,
                     "exit_price": round(exit_price, 2),
                     "r_multiple": round(r_multiple, 3)})}

        from app.core import audit
        audit.record_sell(self.cfg, row, exit_price=exit_price, pnl=pnl,
                          r_multiple=r_multiple, status=status.value, detail=detail,
                          exit_reason=event["exit_reason"])

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
        await bus.publish(Topic.POSITION_UPDATE, event)

        # Every completed trade gets graded, automatically. A P&L number
        # alone teaches nothing; the card is the learning.
        await self._journal(row, exit_price, status.value)
        return event

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

            from datetime import UTC as _UTC

            # Both ends in UTC, naive. The entry came back as UTC and the exit
            # was the PC's own local clock, so "Held" read -238 min.
            def _ts(value: Any) -> _dt | None:
                if not value:
                    return None
                try:
                    parsed = _dt.fromisoformat(str(value))
                    return (parsed.astimezone(_UTC).replace(tzinfo=None)
                            if parsed.tzinfo else parsed)
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
                exit_ts=_dt.now(_UTC).replace(tzinfo=None),
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

    # ------------------------------------------------------------------ #
    # The Standard Pyramid (agents/pyramid.py)
    # ------------------------------------------------------------------ #
    def _own_stop_step(self, row: dict[str, Any], price: float,
                       spot: float | None) -> dict[str, Any]:
        """SJK 1's optional stop management (sjk1.breakeven_r, sjk1.trail_r):
        the stop to breakeven once the trade is +breakeven_r on its own risk,
        then trailed trail_r behind the best R reached. Judged on the
        UNDERLYING for an option (its underlying stop moves), on the price for
        shares. Off (0) by default: the stop stays at the pullback's swing."""
        from app.strategies.sjk1 import SETUP_NAME
        payload = self._payload(row)
        be_r = float(self.cfg.get("sjk1.breakeven_r", 0) or 0)
        trail_r = float(self.cfg.get("sjk1.trail_r", 0) or 0)
        if payload.get("setup") != SETUP_NAME or (be_r <= 0 and trail_r <= 0):
            return row
        option = row.get("instrument_type") in {"CE", "PE"}
        state = payload.setdefault("sjk1_stop", {})
        if not state:                                      # the first look
            if option:
                u_entry = float(payload.get("entry_spot") or spot or 0)
                u_stop = float(payload.get("underlying_stop") or 0)
            else:
                u_entry, u_stop = float(row["entry"]), float(row["stop_loss"])
            state.update(entry=u_entry, stop=u_stop, risk=abs(u_entry - u_stop),
                         best=0.0, armed=False)
        now = float(spot if option and spot is not None else price)
        if not state.get("risk") or not now:
            return row
        long = (row["side"] == "BUY") if not option else row["instrument_type"] == "CE"
        sign = 1.0 if long else -1.0
        r = (now - state["entry"]) * sign / state["risk"]
        state["best"] = max(float(state.get("best", 0.0)), r)
        new = None
        if be_r and not state.get("armed") and state["best"] >= be_r:
            state["armed"] = True
            new = state["entry"]
        if trail_r and state.get("armed"):
            trail = state["entry"] + sign * (state["best"] - trail_r) * state["risk"]
            cur = new if new is not None else float(state.get("current", state["entry"]))
            if (trail - cur) * sign > 0:
                new = trail
        if new is None:
            return row
        state["current"] = round(new, 4)
        stop_loss = float(row["stop_loss"])
        if option:
            payload["underlying_stop"] = round(new, 4)
            stop_loss = max(stop_loss, float(row["entry"])) if state["armed"] else stop_loss
        else:
            stop_loss = round(new, 4)
        import json
        row = {**row, "stop_loss": stop_loss, "payload": json.dumps(payload, default=str)}
        try:
            db.update_position(row["id"], quantity=int(row["quantity"] or 0),
                               entry=float(row["entry"]), stop_loss=stop_loss,
                               target=float(row["target"]), payload=row["payload"],
                               notional=float(row.get("notional") or 0),
                               total_risk=float(row.get("total_risk") or 0))
        except Exception as exc:                           # noqa: BLE001
            log.warning("could not move the SJK 1 stop for %s: %s", row.get("symbol"), exc)
        log.info("SJK 1 %s at %+.2fR — stop moved to %.2f", row.get("symbol"), r, new)
        return row

    @staticmethod
    def _payload(row: dict[str, Any]) -> dict[str, Any]:
        import json
        try:
            return json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError):
            return {}

    def _pyramid_state(self, row: dict[str, Any]) -> Any:
        from app.agents import pyramid
        saved = self._payload(row).get("pyramid")
        if not saved:
            return None
        try:
            return pyramid.PyramidState(**saved)
        except TypeError:
            return None

    def _pyramid_start(self, row: dict[str, Any]) -> dict[str, Any]:
        """Level 0 for a position seen for the first time: remember the base,
        and set the single take-profit at +target_r (3R) from the base entry."""
        import json

        from app.agents import pyramid
        if self._pyramid_state(row) is not None or not row.get("quantity"):
            return row
        state = pyramid.start(self.cfg, row)
        payload = self._payload(row)
        payload["pyramid"] = state.to_dict()
        row = {**row, "target": state.target, "payload": json.dumps(payload, default=str)}
        db.update_position(row["id"], quantity=int(row["quantity"]), entry=float(row["entry"]),
                           stop_loss=float(row["stop_loss"]), target=state.target,
                           payload=row["payload"],
                           notional=float(row.get("notional") or 0.0),
                           total_risk=float(row.get("total_risk") or 0.0))
        return row

    async def _pyramid_step(self, row: dict[str, Any], price: float,
                            spot: float | None) -> dict[str, Any]:
        """Take the next level when the trade has earned it: add the size (the
        risk desk approving), and move the stop for the WHOLE position."""
        import json

        from app.agents import pyramid
        from app.core import audit
        state = self._pyramid_state(row)
        if state is None:
            return row
        payload = self._payload(row)
        is_long = row["side"] == "BUY"
        unit = int(payload.get("unit_size") or 1)
        add = pyramid.plan(self.cfg, state, price, spot, is_long, unit)
        if add is None:
            return row

        sign = 1 if is_long else -1
        open_pnl = (price - float(row["entry"])) * int(row["quantity"] or 0) * sign
        notional = add.quantity * price
        refused: list[str] = []
        if add.quantity:
            refused = (self.risk.approve_add(row["symbol"], open_pnl, notional)
                       if self.risk is not None
                       else ([] if open_pnl >= 0 else ["No averaging down"]))
        if refused:
            # The size is refused; the protective stop step is still taken.
            add = pyramid.Add(level=add.level, quantity=0, price=add.price, spot=add.spot,
                              new_stop=add.new_stop,
                              new_underlying_stop=add.new_underlying_stop,
                              avg_entry=state.avg_entry, total_qty=state.qty,
                              open_r=add.open_r,
                              note=f"Level {add.level} at {add.open_r:+.2f}R: add refused "
                                   f"({refused[0]}); stop moved to {add.new_stop:,.2f}")
        if add.quantity:
            await self._send_add(row, payload, add)

        risk_before = max(0.0, (state.avg_entry - float(row["stop_loss"])) * sign * state.qty)
        state = pyramid.apply(state, add)
        risk_after = max(0.0, (state.avg_entry - add.new_stop) * sign * state.qty)
        payload["pyramid"] = state.to_dict()
        if row.get("instrument_type") in {"CE", "PE"} and add.new_underlying_stop:
            payload["underlying_stop"] = add.new_underlying_stop
        row = {**row, "quantity": state.qty, "entry": state.avg_entry,
               "stop_loss": add.new_stop, "target": state.target,
               "payload": json.dumps(payload, default=str)}
        db.update_position(row["id"], quantity=state.qty, entry=state.avg_entry,
                           stop_loss=add.new_stop, target=state.target,
                           payload=row["payload"],
                           notional=round(state.avg_entry * state.qty, 2),
                           total_risk=round(risk_after, 2))
        if self.risk is not None:
            self.risk.register_add(notional if add.quantity else 0.0, risk_after - risk_before)
        audit.record_add(self.cfg, row, level=add.level, quantity=add.quantity,
                         price=add.price, new_stop=add.new_stop, avg_entry=state.avg_entry,
                         total_qty=state.qty, open_r=add.open_r, note=add.note,
                         refused=refused[0] if refused else "")
        log.info("PYRAMID %s — %s", row["symbol"], add.note)
        await bus.publish(Topic.POSITION_UPDATE, {
            "event": "pyramid_add", "order_tag": "PYRAMID_ADD",
            "signal_id": row["id"], "symbol": row["symbol"],
            "side": row["side"], "level": add.level, "quantity": add.quantity,
            "price": add.price, "stop_loss": add.new_stop, "avg_entry": state.avg_entry,
            "total_quantity": state.qty, "target": state.target, "note": add.note})
        return row

    async def _send_add(self, row: dict[str, Any], payload: dict[str, Any], add: Any) -> None:
        """The add goes to the broker through the dispatcher's own gate (an
        armed trading day; paper, or all three live switches). Alert-only
        desks track the add on the record, as they do the entry."""
        disp = self.dispatcher
        if disp is None or not disp._live_allowed():
            return
        try:
            from app.core.models import Side, TradeSignal
            sig = TradeSignal.model_validate({**payload, "order_tag": "PYRAMID_ADD"})
            side = Side.BUY if row["side"] == "BUY" else Side.SELL
            order = await self.broker.place_order(
                sig.instrument, side, int(add.quantity), float(add.price),
                order_type="MARKET", product=self.cfg.get("execution.product", "MIS"),
                stop_loss=add.new_stop, tag=f"PYRAMID_ADD-L{add.level}")
            log.info("pyramid add order %s: %s", "placed" if order.ok else "FAILED",
                     order.message or order.order_id)
        except Exception as exc:                        # noqa: BLE001 - never breaks polling
            log.warning("pyramid add order for %s failed: %s", row.get("symbol"), exc)

    @staticmethod
    def _infer_setup(row: dict[str, Any]) -> Any:
        """Best-effort setup tag from what the desk recorded at entry."""
        from app.journal.models import SetupType
        blob = f"{row.get('confirmations') or ''} {row.get('rationale') or ''}".lower()
        try:
            import json
            if json.loads(row.get("payload") or "{}").get("setup") == "PD Liquidity Sweep":
                return SetupType.LIQUIDITY_SWEEP
        except (TypeError, ValueError):
            pass
        if "breakout" in blob or "flag" in blob:
            return SetupType.BREAKOUT
        if "vwap" in blob or "reversion" in blob or "oversold" in blob:
            return SetupType.MEAN_REVERSION
        if "news" in blob or "sentiment" in blob:
            return SetupType.NEWS_MOMENTUM
        return SetupType.OTHER

    @staticmethod
    def _underlying_stop(row: dict[str, Any]) -> float | None:
        """The recorded underlying stop for an OPTION row, or None."""
        if row.get("instrument_type") not in {"CE", "PE"}:
            return None
        try:
            import json
            value = json.loads(row.get("payload") or "{}").get("underlying_stop")
            return float(value) if value else None
        except (TypeError, ValueError):
            return None

    async def _marks(self, row: dict[str, Any]) -> tuple[float, float | None] | None:
        """(position mark, underlying spot) — or None when it cannot be marked."""
        quote = await self.broker.get_quote(row["symbol"])
        if not quote:
            return None
        price = await self._current_price(row, quote)
        if price is None:
            return None
        return price, quote.last_price

    async def _current_price(self, row: dict[str, Any], quote: Any = None) -> float | None:
        """Mark to market.

        An option is marked by moving its premium with the underlying, scaled by
        the delta recorded at entry. That is an approximation — it ignores theta
        and IV shifts — but it is applied consistently to every trade, so the
        R-multiples the learning loop grades on stay comparable. In live mode the
        broker's own position P&L supersedes this.
        """
        quote = quote or await self.broker.get_quote(row["symbol"])
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
        # Only applies to the setups the timer is configured for. A signal
        # that names no setup is not one of them: the empty case used to fall
        # through, so the 30-minute Mean Reversion timer closed EVERY trade
        # that was not yet green — ten exits at -0.04R to -0.46R in a morning.
        if setup not in setups:
            return False
        # Never on a setup that must run to its target or its stop (the
        # Previous Day Liquidity Sweep): only the square-off ends it early.
        if setup in (self.cfg.get("risk.no_time_stop_setups") or ["PD Liquidity Sweep"]):
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

    @staticmethod
    def _is_swing(row: dict[str, Any]) -> bool:
        try:
            return bool(json.loads(row.get("payload") or "{}").get("hold_overnight"))
        except (TypeError, ValueError):
            return False

    def _opened_on(self, row: dict[str, Any]):
        from zoneinfo import ZoneInfo
        try:
            opened = datetime.fromisoformat(str(row.get("ts")))
        except (TypeError, ValueError):
            return None
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        return opened.astimezone(ZoneInfo(str(self.cfg.get("system.timezone",
                                                           "Asia/Kolkata")))).date()

    def _held_sessions(self, row: dict[str, Any]) -> int:
        """Weekday sessions since the entry day (the entry day is 0)."""
        import numpy as np

        from app.core import clock
        start = self._opened_on(row)
        if start is None:
            return 0
        today = clock.market_now(str(self.cfg.get("system.timezone",
                                                   "Asia/Kolkata"))).date()
        return int(np.busday_count(start, today))

    def _first_profitable_open(self, row: dict[str, Any], price: float,
                               is_long: bool) -> bool:
        """Larry Williams' bail-out: on each later session, judged once at the
        first mark after the open, sell if it is in profit; else hold."""
        if not bool(self.cfg.get("swing.first_profitable_open", True)):
            return False
        from app.core import clock
        tz = str(self.cfg.get("system.timezone", "Asia/Kolkata"))
        now = clock.market_now(tz)
        start = self._opened_on(row)
        if start is None or start >= now.date():
            return False
        if not clock.past(tz, str(self.cfg.get("system.market_open", "09:15"))):
            return False
        key = (row["id"], now.date().isoformat())
        seen = self.__dict__.setdefault("_fpo_judged", set())
        if key in seen:
            return False
        seen.add(key)
        entry = float(row["entry"])
        return price > entry if is_long else price < entry

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
