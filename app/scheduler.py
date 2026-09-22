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
        self.replay = WeeklyReplay(self, self.cfg)
        self.trading_day = TradingDay(self, self.cfg)
        self.desk.dispatcher.trading_day = self.trading_day

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
        self.desk = TradingDesk(self.broker, self.cfg, risk_manager=self.risk)
        self.data = MarketDataService(self.broker, self.cfg)
        self.news = NewsCollector(self.cfg)
        self.macro = MacroCollector(self.cfg)
        self.outcomes = OutcomeTracker(self.broker, self.cfg, risk_manager=self.risk)
        self.scanner = OpportunityScanner(self, self.cfg)
        self.replay = WeeklyReplay(self, self.cfg)
        self.trading_day = TradingDay(self, self.cfg)
        self.desk.dispatcher.trading_day = self.trading_day
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

        # Keep the CMIO's weights current with what the learning loop has found.
        self.feedback.apply_learned_weights()

        targets = symbols or [w["symbol"] for w in self.cfg.watchlist()]
        outcomes: list[dict[str, Any]] = []

        for symbol in targets:
            try:
                result = await self._cycle_for_symbol(
                    symbol, cycle_id, news_items, macro_snap)
                outcomes.append(result)
            except Exception as exc:
                log.exception("cycle failed for %s: %s", symbol, exc)
                await bus.publish(Topic.ERROR, {"symbol": symbol, "error": str(exc)})

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

    async def _cycle_for_symbol(self, symbol: str, cycle_id: str,
                                news_items: list, macro_snap: Any) -> dict[str, Any]:
        symbol_news = self.news.for_symbol(news_items, symbol) if news_items else []
        recall = self.feedback.recall_for(symbol)

        ctx = await self.data.build_context(
            symbol=symbol, cycle_id=cycle_id, news=symbol_news, macro=macro_snap,
            fundamentals=self._fundamentals.get(symbol), recall=recall,
        )
        if ctx.quote:
            await bus.publish(Topic.QUOTE, ctx.quote)

        result = await self.desk.run_cycle(ctx, cycle_id=f"{cycle_id}-{symbol}")

        # Persist
        try:
            db.save_cycle(result, proceeded=result.signal is not None)
            signal_id = result.signal.id if result.signal else None
            db.save_reports(result.reports, symbol, result.cycle_id, signal_id)
            if result.signal:
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

                if phase == "premarket" and self._premarket_done_on != today:
                    await self.run_premarket_scan()

                elif phase == "open" and not self.paused:
                    if self._premarket_done_on != today:
                        await self.run_premarket_scan()
                    # Arm by itself at the open, so the desk can act on what it
                    # finds without somebody being at the screen. Paper only.
                    await self.trading_day.maybe_auto_arm()
                    await self.run_cycle()

                elif phase in {"closed", "weekend", "postmarket"}:
                    # Outside hours we still mark and grade open positions.
                    closed = await self.outcomes.poll()
                    if closed:
                        await self.feedback.update_from_closed(closed)
                    await self._maybe_publish_day_summary()
                    await self._maybe_write_weekly_review()

                await bus.publish(Topic.RISK_STATE, self.risk.snapshot())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("engine loop error: %s", exc)
                await bus.publish(Topic.ERROR, {"error": str(exc)})

            sleep_for = interval if self.session_phase() == "open" else max(interval, 120)
            await asyncio.sleep(sleep_for)

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
        today = clock.market_now(self.timezone).date().isoformat()
        if self._day_summary_on == today:
            return

        delay = int(self.cfg.get("trading_day.summary_after_square_off_minutes", 5))
        square_off = str(self.cfg.get("system.square_off_time", "15:15"))
        hour, _, minute = square_off.partition(":")
        after = (int(hour) * 60 + int(minute or 0) + delay)
        now = clock.market_now(self.timezone)
        if (now.hour * 60 + now.minute) < after:
            return
        if now.weekday() >= 5:
            self._day_summary_on = today
            return

        try:
            summary = self.trading_day.report()
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not build the day summary: %s", exc)
            self._day_summary_on = today
            return

        self._day_summary_on = today
        summary["published_at"] = now.isoformat()
        self._last_day_summary = summary
        await bus.publish("trading_day.summary", summary)

        cur = self.cfg.market.currency_symbol
        log.info("DAY SUMMARY %s — %d signals, %d trades, %d closed, "
                 "%+.2fR, %s%+.0f", today, summary.get("signals_generated", 0),
                 summary.get("trades_taken", 0), summary.get("trades_closed", 0),
                 summary.get("total_r", 0.0), cur, summary.get("pnl", 0.0))

    async def _maybe_write_weekly_review(self) -> None:
        """Have the week's review waiting once Friday has closed.

        Written once per week, after the last session ends, so it is on disk
        before you go looking for it. Building it on demand from the dashboard
        still works — this only means you do not have to remember to.
        """
        from app.journal import weekly

        try:
            start, end = weekly.current_week(self.cfg)
            if self._weekly_written_for == end.isoformat():
                return
            if not weekly.is_complete(end, self.cfg):
                return

            review = await weekly.build(start, end, cfg=self.cfg)
            self._weekly_written_for = end.isoformat()
            if not review.trades:
                log.info("no trades in the week to %s — no review written", end)
                return
            weekly.save(review)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not write the weekly review: %s", exc)
            # Do not retry every two minutes for the rest of the weekend.
            self._weekly_written_for = weekly.current_week(self.cfg)[1].isoformat()

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        db.init_db()
        self.feedback.apply_learned_weights()
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
            "day_summary": self._last_day_summary,
            "live_orders": self.cfg.live_orders_enabled,
            "auto_place_orders": self.cfg.get("execution.auto_place_orders", False),
            # A paper broker with auto_place_orders on DOES place orders — they
            # are simulated. Without this the dashboard cannot tell that state
            # from true alert-only, and both read as "nothing will be sent".
            "is_paper_account": getattr(self.broker, "is_paper_account", True),
            "armed": self.trading_day.armed if self.trading_day else False,
        }
