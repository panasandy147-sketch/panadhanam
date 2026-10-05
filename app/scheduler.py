"""The engine loop: market-hours scheduling, cycle execution, learning.

One cycle =
    refresh shared data (news + macro, fetched once for all symbols)
      → for each symbol: build context → run the agent graph
      → persist everything
      → poll open positions → grade closed ones → re-weight agents
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from app.agents.graph import TradingDesk
from app.agents.risk import RiskManager
from app.analysis.focus import FocusList
from app.analysis.opportunities import OpportunityScanner
from app.analysis.replay import WeeklyReplay
from app.brokers.base import BrokerAdapter
from app.core import clock
from app.core.bus import Topic, bus
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import Fundamentals
from app.data.macro import MacroCollector
from app.data.market import MarketDataService, fetch_fundamentals
from app.data.news import NewsCollector
from app.learning.feedback import FeedbackLoop
from app.learning.outcomes import OutcomeTracker
from app.live.session import TradingDay
from app.storage import db

log = get_logger("scheduler")


class TradingEngine:
    def __init__(self, broker: BrokerAdapter, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self.broker = broker
        self.risk = RiskManager(self.cfg)
        self.desk = TradingDesk(broker, self.cfg, risk_manager=self.risk)
        self.data = MarketDataService(broker, self.cfg)
        self.news = NewsCollector(self.cfg)
        self.macro = MacroCollector(self.cfg)
        self.feedback = FeedbackLoop(self.cfg)
        self.outcomes = OutcomeTracker(broker, self.cfg, risk_manager=self.risk)
        self.scanner = OpportunityScanner(self, self.cfg)
        self.focus = FocusList(self, self.cfg)
        self.replay = WeeklyReplay(self, self.cfg)
        self.trading_day = TradingDay(self, self.cfg)
        # The RSI(2) swing book: its own paper ledger, run once a day.
        from app.strategies.rsi2_swing import Rsi2Book
        self.rsi2 = Rsi2Book(broker, self.cfg)
        # The Williams-Crabel swing book: its own ledger, every 5 min.
        from app.strategies.williams_swing import WilliamsBook
        self.williams = WilliamsBook(broker, self.cfg)
        # The hourly audit email (app/core/email_report.py; .env SMTP_*).
        from app.core.email_report import AuditEmailer
        self.mailer = AuditEmailer(self.cfg, self.rsi2, self.williams)
        self.desk.dispatcher.trading_day = self.trading_day
        self.outcomes.trading_day = self.trading_day
        self.outcomes.dispatcher = self.desk.dispatcher

        self.running = False
        self.paused = False
        self._task: asyncio.Task | None = None
        self._fundamentals: dict[str, Fundamentals] = {}
        self._premarket_done_on: str | None = None
        # ISO date of the week-end whose review is already written.
        self._weekly_written_for: str | None = None
        # ISO date whose end-of-day summary has already been published.
        self._day_summary_on: str | None = None
        self._last_day_summary: dict[str, Any] | None = None
        self.last_cycle: dict[str, Any] = {}
        self.cycle_count = 0
        # For the dashboard's live candidate: the names this cycle is going
        # through, and the newest trade the risk desk judged (taken or not).
        self.scanning: list[str] = []
        self.candidate: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # Market hours
    # ------------------------------------------------------------------ #
    @property
    def timezone(self) -> str:
        """Always the MARKET's timezone, never the server's."""
        return str(self.cfg.get("system.timezone", "Asia/Kolkata"))

    def market_open_now(self) -> bool:
        return clock.is_open(
            self.timezone,
            self.cfg.get("system.market_open", "09:15"),
            self.cfg.get("system.market_close", "15:30"),
            self.cfg.get("system.trading_days"),
        )

    def session_phase(self) -> str:
        return clock.session_phase(
            self.timezone,
            self.cfg.get("system.premarket_scan_time", "08:45"),
            self.cfg.get("system.market_open", "09:15"),
            self.cfg.get("system.market_close", "15:30"),
            self.cfg.get("system.trading_days"),
        )

    # ------------------------------------------------------------------ #
    # Market switching
    # ------------------------------------------------------------------ #
    async def switch_market(self, code: str) -> dict[str, Any]:
        """Point the whole desk at a different market.

        Everything that was derived from the old market has to go: the broker
        (Zerodha cannot quote AAPL), cached fundamentals, the synthetic price
        seeds, and any cached scan. Open positions are NOT closed — switching
        the view must never silently abandon a live trade — so the switch is
        refused while positions are open.
        """
        previous = self.cfg.active_market
        code = code.upper()
        if code == previous:
            return {"switched": False, "market": previous, "reason": "already active"}

        if self.risk.state.open_positions > 0:
            return {
                "switched": False, "market": previous,
                "reason": (f"{self.risk.state.open_positions} position(s) still open. "
                           f"Close or square off before switching markets — the new "
                           f"market's broker cannot manage them."),
            }

        active = self.cfg.switch_market(code)
        if active != code:
            return {"switched": False, "market": active,
                    "reason": f"unknown market '{code}'"}

        # A broker bound to the old market can't serve the new one.
        from app.brokers.factory import build_broker, reset_broker
        await reset_broker()
        self.broker = await build_broker(self.cfg)

        self.risk = RiskManager(self.cfg)
        # The new market's day so far: its trade count, P&L and any lockout.
        try:
            self.risk.restore_day()
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not restore %s's day: %s", code, exc)
        self.desk = TradingDesk(self.broker, self.cfg, risk_manager=self.risk)
        self.data = MarketDataService(self.broker, self.cfg)
        self.news = NewsCollector(self.cfg)
        self.macro = MacroCollector(self.cfg)
        self.outcomes = OutcomeTracker(self.broker, self.cfg, risk_manager=self.risk)
        self.scanner = OpportunityScanner(self, self.cfg)
        self.focus = FocusList(self, self.cfg)
        self.replay = WeeklyReplay(self, self.cfg)
        self.trading_day = TradingDay(self, self.cfg)
        self.desk.dispatcher.trading_day = self.trading_day
        self.outcomes.trading_day = self.trading_day
        self.outcomes.dispatcher = self.desk.dispatcher
        self._fundamentals.clear()
        self._premarket_done_on = None

        log.info("desk switched to %s (%s) — broker=%s, %d symbols",
                 self.cfg.market.name, active, self.broker.name,
                 len(self.cfg.watchlist()))
        await bus.publish("market.switched", {
            "market": self.cfg.market.describe(),
            "broker": self.broker.name,
            "symbols": [w["symbol"] for w in self.cfg.watchlist()],
        })
        await bus.publish(Topic.RISK_STATE, self.risk.snapshot())
        return {"switched": True, "market": active,
                "profile": self.cfg.market.describe(),
                "broker": self.broker.name}

    # ------------------------------------------------------------------ #
    # Pre-market
    # ------------------------------------------------------------------ #
    async def run_premarket_scan(self) -> dict[str, Any]:
        """Fundamental screen + macro read, once a day before the open."""
        log.info("running pre-market scan")
        watch = self.cfg.watchlist()
        stocks = [w["symbol"] for w in watch if not w.get("is_index")]

        results = await asyncio.gather(
            *[fetch_fundamentals(s) for s in stocks], return_exceptions=True)
        eligible, screened_out = [], []
        for sym, res in zip(stocks, results, strict=False):
            if isinstance(res, Exception) or res is None:
                screened_out.append({"symbol": sym, "reason": "no fundamental data"})
                continue
            self._fundamentals[sym] = res

        macro = await self.macro.fetch()
        await bus.publish(Topic.MACRO, macro)

        # Grade eligibility using the same agent the desk uses intraday.
        from app.agents.fundamental import FundamentalAgent
        from app.core.models import MarketContext
        agent = FundamentalAgent(self.cfg)
        for sym in stocks:
            ctx = MarketContext(symbol=sym, cycle_id="premarket",
                                fundamentals=self._fundamentals.get(sym))
            report = agent.analyse_rules(ctx)
            if report.extra.get("eligible"):
                eligible.append({"symbol": sym, "quality": report.extra.get("quality_score")})
            else:
                screened_out.append({"symbol": sym,
                                     "reason": ", ".join(report.extra.get("fails", []))
                                     or report.rationale})

        self._premarket_done_on = clock.market_now(self.timezone).date().isoformat()
        summary = {
            "ts": datetime.now().isoformat(),
            "eligible": eligible,
            "screened_out": screened_out,
            "macro_notes": macro.notes,
        }
        log.info("pre-market: %d eligible, %d screened out",
                 len(eligible), len(screened_out))
        await bus.publish("premarket.scan", summary)
        return summary

    # ------------------------------------------------------------------ #
    # The pre-market screener (app/analysis/pre_market_screener.py)
    # ------------------------------------------------------------------ #
    def _screened_only(self) -> bool:
        return bool(self.cfg.get("screener.enabled", False))

    def _screened_targets(self) -> list[str]:
        from app.analysis import pre_market_screener as scr
        max_daily = int(self.cfg.get("risk.max_daily_trades", 0) or 0)
        if max_daily and self.risk.state.trades_today >= max_daily:
            log.info("daily trade cap reached (%d/%d) — no new signals this session",
                     self.risk.state.trades_today, max_daily)
            return []
        entry = scr.todays(self.cfg, clock.market_now(self.timezone).date())
        targets = list((entry or {}).get("symbols") or [])
        # The gold desk's instruments (swing: true) are not screened — gold's
        # ~1% daily range never passes the 2% ATR floor — and always looked at.
        for item in self.cfg.watchlist():
            if item.get("swing") and item["symbol"] not in targets:
                targets.append(item["symbol"])
        return targets

    async def maybe_run_screener(self, force: bool = False) -> dict[str, Any] | None:
        """Once a day at screener.run_at on the market clock (09:00 IST), from
        yesterday's end-of-day bars. `force` runs it now (the API / --screen)."""
        from app.analysis import pre_market_screener as scr
        if not self._screened_only() and not force:
            return None
        now = clock.market_now(self.timezone)
        if not force:
            if now.weekday() >= 5 or scr.todays(self.cfg, now.date()) is not None:
                return None
            run_at = str(self.cfg.get("screener.run_at", "09:00"))
            h, _, m = run_at.partition(":")
            if now.hour * 60 + now.minute < int(h) * 60 + int(m or 0):
                return None
        try:
            entry = await scr.run(self.cfg, self.broker, now.date())
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("the pre-market screener failed: %s", exc)
            return None
        await bus.publish("screener.done", entry)
        return entry

    # ------------------------------------------------------------------ #
    # One full cycle
    # ------------------------------------------------------------------ #
    async def run_cycle(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        cycle_id = f"CY-{datetime.now():%H%M%S}-{uuid.uuid4().hex[:4]}"
        self.cycle_count += 1

        # Shared data fetched once per cycle, not once per symbol.
        news_items, macro_snap = await asyncio.gather(
            self.news.fetch(), self.macro.fetch(), return_exceptions=True)
        if isinstance(news_items, Exception):
            log.warning("news fetch failed: %s", news_items)
            news_items = []
        if isinstance(macro_snap, Exception):
            log.warning("macro fetch failed: %s", macro_snap)
            macro_snap = None

        if news_items:
            for item in news_items[:10]:
                await bus.publish(Topic.NEWS, item)
        if macro_snap:
            await bus.publish(Topic.MACRO, macro_snap)

        # A macro release in the window (scheduled, or just in the headlines)
        # pauses every new entry this cycle; open trades are unaffected.
        from app.core import blackout
        previous = self.risk.blackout_reason
        self.risk.blackout_reason = blackout.reason(self.cfg, news_items)
        if self.risk.blackout_reason != previous:
            await bus.publish("news.blackout", {
                "active": bool(self.risk.blackout_reason),
                "reason": self.risk.blackout_reason or "news blackout over"})

        # Keep the CMIO's weights current with what the learning loop has found.
        self.feedback.apply_learned_weights()

        # The focus list: the top names of each band, re-ranked on its own
        # clock. An explicit symbol list (a test, a manual cycle) bypasses it.
        ready: dict[str, Any] = {}
        if symbols:
            targets = list(symbols)
        elif self._screened_only():
            # The pre-market screener replaces full-universe scanning: only
            # today's Band A/B names are cycled (and the risk desk refuses
            # anything else); none at all once the day's trade cap is reached.
            targets = self._screened_targets()
        else:
            targets, ready = await self.focus.targets(cycle_id, news_items, macro_snap)
        outcomes: list[dict[str, Any]] = []

        # A symbol already held is MANAGED, not re-scanned: no fresh entry
        # evaluation for it (the risk desk would only refuse it as "Already
        # holding"). Its stop, target and Standard Pyramid adds (+50% at +1R,
        # +25% at +2R) belong to the position manager — outcomes.poll() below.
        if bool(self.cfg.get("system.skip_held_symbols", True)):
            held = {str(r["symbol"]).upper() for r in db.open_signals()}
            for symbol in [t for t in targets if t.upper() in held]:
                outcomes.append({
                    "symbol": symbol, "held": True, "signal": None, "signal_id": None,
                    "rejected": ["held — managed by the position manager (stop, "
                                 "target, pyramid adds); no new entry scan"]})
            targets = [t for t in targets if t.upper() not in held]

        # Fetch every symbol's data concurrently, then decide one at a time.
        # Fetching one after another made a 60-name watchlist take longer
        # than the cycle; deciding one at a time is still required, because
        # each approved trade must count against the limits before the next
        # symbol is judged.
        missing = [t for t in targets if t not in ready]
        contexts = {**ready,
                    **await self._prefetch(missing, cycle_id, news_items, macro_snap)}

        self.scanning = list(targets)
        for symbol in targets:
            try:
                result = await self._cycle_for_symbol(
                    symbol, cycle_id, news_items, macro_snap,
                    ctx=contexts.get(symbol))
                outcomes.append(result)
            except Exception as exc:
                log.exception("cycle failed for %s: %s", symbol, exc)
                await bus.publish(Topic.ERROR, {"symbol": symbol, "error": str(exc)})
        self.scanning = []

        # Grade what resolved, then learn from it.
        try:
            closed = await self.outcomes.poll()
            if closed:
                await self.feedback.update_from_closed(closed)
        except Exception as exc:
            log.warning("outcome polling failed: %s", exc)

        self.last_cycle = {
            "cycle_id": cycle_id, "ts": datetime.now().isoformat(),
            "symbols": len(targets), "results": outcomes,
        }
        return outcomes

    async def _build_context(self, symbol: str, cycle_id: str,
                             news_items: list, macro_snap: Any):
        symbol_news = self.news.for_symbol(news_items, symbol) if news_items else []
        return await self.data.build_context(
            symbol=symbol, cycle_id=cycle_id, news=symbol_news, macro=macro_snap,
            fundamentals=self._fundamentals.get(symbol),
            recall=self.feedback.recall_for(symbol),
        )

    async def _prefetch(self, symbols: list[str], cycle_id: str,
                        news_items: list, macro_snap: Any) -> dict[str, Any]:
        limit = asyncio.Semaphore(int(self.cfg.get("system.fetch_concurrency", 8)))

        async def one(symbol: str):
            async with limit:
                try:
                    return await self._build_context(symbol, cycle_id,
                                                     news_items, macro_snap)
                except Exception as exc:
                    log.warning("data fetch failed for %s: %s", symbol, exc)
                    return None

        built = await asyncio.gather(*(one(s) for s in symbols))
        return {s: c for s, c in zip(symbols, built, strict=True) if c is not None}

    async def _cycle_for_symbol(self, symbol: str, cycle_id: str,
                                news_items: list, macro_snap: Any,
                                ctx: Any = None) -> dict[str, Any]:
        if ctx is None:
            ctx = await self._build_context(symbol, cycle_id, news_items, macro_snap)
        if ctx.quote:
            await bus.publish(Topic.QUOTE, ctx.quote)

        result = await self.desk.run_cycle(ctx, cycle_id=f"{cycle_id}-{symbol}")
        self._remember_candidate(symbol, result)

        # Persist
        try:
            db.save_cycle(result, proceeded=result.signal is not None)
            signal_id = result.signal.id if result.signal else None
            db.save_reports(result.reports, symbol, result.cycle_id, signal_id)
            if result.signal:
                from app.core.version import code_version
                result.signal.code_version = code_version()
                db.save_signal(result.signal)
                self.risk.register_open(result.signal)
        except Exception as exc:
            log.warning("persistence failed for %s: %s", symbol, exc)

        return {
            "symbol": symbol,
            "bias": result.bias.value,
            "composite_score": result.composite_score,
            "signal": result.signal.alert_line() if result.signal else None,
            "signal_id": result.signal.id if result.signal else None,
            "rejected": result.rejected,
            "duration_ms": result.duration_ms,
        }

    def _remember_candidate(self, symbol: str, result: Any) -> None:
        """Keep the case for the newest trade the risk desk judged — taken or
        refused — for the dashboard's live candidate: the chart of the
        symbol, the entry, stop and target, and why. A symbol with no setup
        leaves the last candidate in place."""
        proposal = result.signal or getattr(result, "proposal", None)
        if proposal is None:
            return
        from app.core.models import InstrumentType, SignalStatus
        inst = proposal.instrument
        option = inst.instrument_type in (InstrumentType.CALL, InstrumentType.PUT)
        taken = proposal.status in (SignalStatus.APPROVED, SignalStatus.OPEN)
        long = proposal.side.value == "BUY"
        self.candidate = {
            "market": self.cfg.active_market,
            "symbol": symbol,
            "ts": proposal.ts.isoformat(),
            "action": (f"BUY {'CALL' if inst.instrument_type is InstrumentType.CALL else 'PUT'}"
                       if option else ("BUY" if long else "SELL")),
            "direction": "LONG" if (long if not option else
                                    inst.instrument_type is InstrumentType.CALL) else "SHORT",
            "instrument": inst.tradingsymbol,
            "option": option,
            "setup": proposal.setup or "",
            "entry": proposal.entry,
            "stop": proposal.stop_loss,
            "target": proposal.target,
            # On the UNDERLYING, for the chart: an option's entry, stop and
            # target are premiums and do not belong on the stock's price axis.
            "spot": proposal.entry_spot if option else proposal.entry,
            "underlying_stop": (proposal.underlying_stop if option
                                else proposal.stop_loss),
            "underlying_target": None if option else proposal.target,
            "stop_note": proposal.underlying_stop_note,
            "risk_reward": proposal.risk_reward,
            "quantity": proposal.quantity,
            "unit_label": proposal.unit_label,
            "bias": proposal.bias.value,
            "score": proposal.composite_score,
            "rationale": proposal.rationale,
            "counter": proposal.counter_argument,
            "confirmations": list(proposal.confirmations)[:8],
            "taken": taken,
            "refused": "" if taken else "; ".join(proposal.rejection_reasons[:2]),
            "hold_overnight": bool(getattr(proposal, "hold_overnight", False)),
        }

    # ------------------------------------------------------------------ #
    # The loop
    # ------------------------------------------------------------------ #
    async def _loop(self) -> None:
        interval = int(self.cfg.get("system.cycle_seconds", 60))
        log.info("engine started — cycling every %ds", interval)

        while self.running:
            try:
                # Follow whichever market is trading before reading the
                # phase, or the first cycle after a session opens is wasted.
                await self.maybe_follow_session()

                phase = self.session_phase()
                today = clock.market_now(self.timezone).date().isoformat()
                # Today's Band A/B watchlist, from yesterday's close, at 09:00.
                await self.maybe_run_screener()

                if phase == "premarket" and self._premarket_done_on != today:
                    await self.run_premarket_scan()

                elif phase == "open" and not self.paused:
                    if self._premarket_done_on != today:
                        await self.run_premarket_scan()
                    # Arm by itself at the open, so the desk can act on what it
                    # finds without somebody being at the screen. Paper only.
                    await self.trading_day.maybe_auto_arm()
                    await self.run_cycle()
                    await self._maybe_run_rsi2()

                elif phase in {"closed", "weekend", "postmarket"}:
                    # Outside hours we still mark and grade open positions.
                    closed = await self.outcomes.poll()
                    if closed:
                        await self.feedback.update_from_closed(closed)
                    await self._maybe_publish_day_summary()
                    await self._maybe_write_weekly_review()

                await self.mailer.maybe_send(clock.market_now(self.timezone),
                                             phase == "open")
                await bus.publish(Topic.RISK_STATE, self.risk.snapshot())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("engine loop error: %s", exc)
                await bus.publish(Topic.ERROR, {"error": str(exc)})

            sleep_for = interval if self.session_phase() == "open" else max(interval, 120)
            await asyncio.sleep(sleep_for)

    async def _maybe_run_rsi2(self) -> None:
        """The RSI(2) swing book's daily run (rsi2_swing.at to the close)."""
        try:
            await self.rsi2.maybe_run()
        except Exception as exc:                          # noqa: BLE001
            log.warning("RSI(2) swing book run failed: %s", exc)
        try:
            await self.williams.maybe_run()
        except Exception as exc:                          # noqa: BLE001
            log.warning("Williams-Crabel swing book run failed: %s", exc)

    async def maybe_follow_session(self) -> str | None:
        """Move to whichever market is trading, so one app covers both.

        India runs 03:45-10:00 UTC and the US 13:30-20:00 UTC, so the two
        sessions never overlap and a single desk can serve both by following
        the clock. It is not parallel trading: at any moment exactly one
        market is active, which is the only arrangement where a single risk
        budget and a single daily loss limit mean anything.

        Three conditions, all required:
          * the active market is NOT in session (never interrupt a live one);
          * some other configured market IS;
          * nothing is open (switching rebuilds the broker, and the new one
            cannot manage the old market's positions).

        Returns the code switched to, or None.
        """
        if not bool(self.cfg.get("markets.auto_follow_session", False)):
            return None
        if self.cfg.market.is_in_session():
            return None
        if self.risk.state.open_positions:
            log.debug("not following the session: %d position(s) still open",
                      self.risk.state.open_positions)
            return None

        for profile in self.cfg.profiles.values():
            if profile.code == self.cfg.active_market or not profile.is_in_session():
                continue
            log.info("following the session: %s has opened, switching from %s",
                     profile.code, self.cfg.active_market)
            result = await self.switch_market(profile.code)
            return profile.code if result.get("switched") else None
        return None

    async def _maybe_publish_day_summary(self) -> None:
        """Put the day's result on screen once the session is over.

        Written once per day, shortly after square-off, so the answer to "what
        happened today" is waiting rather than something you have to go and
        ask for. A day with no trades still publishes — "nothing today, and
        here is what the desk was waiting for" is the more common outcome and
        the more useful one to read.
        """
        # Keyed by market as well as date: on Auto, India closes and then the
        # US trades the same date, and each needs its own day record.
        today = clock.market_now(self.timezone).date().isoformat()
        marker = f"{self.cfg.active_market}:{today}"
        if self._day_summary_on == marker:
            return

        delay = int(self.cfg.get("trading_day.summary_after_square_off_minutes", 5))
        square_off = str(self.cfg.get("system.square_off_time", "15:15"))
        hour, _, minute = square_off.partition(":")
        after = (int(hour) * 60 + int(minute or 0) + delay)
        now = clock.market_now(self.timezone)
        if (now.hour * 60 + now.minute) < after:
            return
        if now.weekday() >= 5:
            self._day_summary_on = marker
            return

        try:
            summary = self.trading_day.report()
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not build the day summary: %s", exc)
            self._day_summary_on = marker
            return

        self._day_summary_on = marker
        # Keep the day for the weekly review: its record and its audit.
        try:
            from app.core.record import save_day
            save_day(self.cfg, now.date())
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not save the day's record: %s", exc)
        # And the week so far: journal/weekly/ holds Monday-to-today after
        # every session, not only after Friday.
        await self._save_week_so_far()
        summary["published_at"] = now.isoformat()
        self._last_day_summary = summary
        await bus.publish("trading_day.summary", summary)

        cur = self.cfg.market.currency_symbol
        log.info("DAY SUMMARY %s — %d signals, %d trades, %d closed, "
                 "%+.2fR, %s%+.0f", today, summary.get("signals_generated", 0),
                 summary.get("trades_taken", 0), summary.get("trades_closed", 0),
                 summary.get("total_r", 0.0), cur, summary.get("pnl", 0.0))

    async def _save_week_so_far(self) -> None:
        """The provisional weekly review, rewritten after each session.

        No coach (that waits for Friday); just the numbers and the audit log
        day by day, so a Wednesday look at journal/weekly/ has Mon-Wed in it.
        """
        from app.journal import weekly
        try:
            start, end = weekly.current_week(self.cfg)
            review = await weekly.build(start, end, with_coach=False, cfg=self.cfg)
            if review.trades or review.audit_days:
                weekly.save(review)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not save the week so far: %s", exc)

    async def _maybe_write_weekly_review(self) -> None:
        """Have the week's review waiting once Friday has closed.

        Written once per week, after the last session ends, so it is on disk
        before you go looking for it. Building it on demand from the dashboard
        still works — this only means you do not have to remember to.
        """
        from app.journal import weekly

        try:
            start, end = weekly.current_week(self.cfg)
            marker = f"{self.cfg.active_market}:{end.isoformat()}"
            if self._weekly_written_for == marker:
                return
            if not weekly.is_complete(end, self.cfg):
                return

            review = await weekly.build(start, end, cfg=self.cfg)
            self._weekly_written_for = marker
            # Friday's self-reflection runs whether or not a review is saved;
            # with too few trades it records that and changes nothing.
            await self._friday_feedback(start, end)
            if not review.trades:
                log.info("no trades in the week to %s — no review written", end)
                return
            weekly.save(review)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not write the weekly review: %s", exc)
            # Do not retry every two minutes for the rest of the weekend.
            self._weekly_written_for = (f"{self.cfg.active_market}:"
                                        f"{weekly.current_week(self.cfg)[1].isoformat()}")

    async def _friday_feedback(self, start, end) -> None:
        """scripts/ollama_feedback.py: Ollama tunes the analysts' vote weights."""
        if not bool(self.cfg.get("feedback.enabled", True)):
            return
        try:
            from scripts import ollama_feedback

            done = await ollama_feedback.run(self.cfg, start, end)
            await bus.publish("feedback.weekly", {
                "week": done.week, "applied": done.applied, "note": done.note,
                "changes": done.changes, "weights": done.weights,
                "advice": done.advice})
        except asyncio.CancelledError:
            raise
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("Friday Ollama feedback failed: %s", exc)

    async def _catch_up_feedback(self) -> None:
        """A desk stopped before Friday's close still reflects on that week."""
        try:
            from scripts import ollama_feedback

            start, end = ollama_feedback.last_finished_week(self.cfg)
            week = f"{start.isoformat()}_to_{end.isoformat()}"
            if not ollama_feedback.record_path(week).exists():
                await self._friday_feedback(start, end)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not catch up the Friday feedback: %s", exc)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        db.init_db()
        self.risk.restore_open(db.open_signals())
        # Today's trade count, realised P&L and any lockout survive a restart.
        self.risk.restore_day()
        self.feedback.apply_learned_weights()
        self._feedback_task = asyncio.create_task(self._catch_up_feedback())
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("engine stopped")

    def data_provenance(self) -> dict[str, Any]:
        """Where the prices on screen come from. See feeds.stack."""
        from app.data.feeds.stack import describe_data_source
        return describe_data_source(self.broker)

    def status(self) -> dict[str, Any]:
        return {
            "market": {
                **self.cfg.market.describe(),
                **clock.describe(self.timezone),
                "active": self.cfg.active_market,
            },
            "available_markets": self.cfg.available_markets(),
            "data_source": self.data_provenance(),
            "trading_day": self.trading_day.status(),
            "running": self.running,
            "paused": self.paused,
            "phase": self.session_phase(),
            "market_open": self.market_open_now(),
            "cycle_count": self.cycle_count,
            "last_cycle": self.last_cycle,
            "premarket_done": self._premarket_done_on,
            "desk": self.desk.describe(),
            "risk": self.risk.snapshot(),
            "trading_mode": self.cfg.trading_mode,
            # Only this market's: after India's close the desk moves to the
            # US, and India's summary is not the US screen's "today".
            "day_summary": (self._last_day_summary
                            if (self._last_day_summary or {}).get("market")
                            in (None, self.cfg.active_market) else None),
            "live_orders": self.cfg.live_orders_enabled,
            "auto_place_orders": self.cfg.get("execution.auto_place_orders", False),
            # A paper broker with auto_place_orders on DOES place orders — they
            # are simulated. Without this the dashboard cannot tell that state
            # from true alert-only, and both read as "nothing will be sent".
            "is_paper_account": getattr(self.broker, "is_paper_account", True),
            "armed": self.trading_day.armed if self.trading_day else False,
        }
