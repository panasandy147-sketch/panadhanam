"""The desk. One loop, one decision at a time, all of it paper.

Each cycle:
    1. Roll the session date, resetting yesterday's counters.
    2. Mark and manage anything already open. This happens FIRST and in every
       phase — an open position must be managed after the entry window shuts,
       and on a day the breaker has tripped.
    3. Inside the entry window only, and only when flat, look for a setup.
    4. Force-exit everything at the square-off time.

The order is deliberate. Looking for new trades before managing open ones is
how a desk ends up doubling down while a loser runs.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from panaoptions import clock
from panaoptions.config import Config, get_config
from panaoptions.data.feed import YahooFeed
from panaoptions.data.premarket import screen
from panaoptions.engine import contracts as contract_filter
from panaoptions.engine import indicators as ta
from panaoptions.engine import levels as levels_mod
from panaoptions.engine import strategies
from panaoptions.ledger import store
from panaoptions.ledger.paper import PaperLedger
from panaoptions.logging import get_logger
from panaoptions.models import ExitReason, PreMarketRead
from panaoptions.notify.webhook import Notifier
from panaoptions.risk.guardrails import RiskManager

log = get_logger("app")


class OptionsDesk:
    def __init__(self, cfg: Config | None = None, feed: Any | None = None) -> None:
        self.cfg = cfg or get_config()
        self.feed = feed or YahooFeed()
        self.risk = RiskManager(self.cfg)
        self.ledger = PaperLedger(self.cfg, self.risk)
        self.notifier = Notifier(self.cfg)
        self.running = False
        self.screened: list[PreMarketRead] = []
        self._screened_on: str = ""
        self._screened_at: datetime | None = None
        # Opening range and pre-market extremes, per symbol, for today.
        self._levels: dict[str, Any] = {}
        self._levels_on: str = ""
        self._weekly_written_for: str | None = None
        self._daily_written_for: str | None = None
        self._predictor: Any = None

    # ------------------------------------------------------------------ #
    async def start(self, cycle_seconds: int = 60) -> None:
        store.init()
        if not await self.feed.connect():
            log.error("no market data — refusing to start. A desk that cannot "
                      "see prices must not pretend to trade.")
            return

        # Say up front if the rules cannot all hold. Discovering it by watching
        # the desk take nothing for a fortnight is the expensive way.
        from panaoptions import preflight
        preflight.report(self.cfg)

        self._load_predictor()
        self.running = True
        log.info("panaoptions desk started — paper only, capital $%.2f, "
                 "entries %s-%s %s", self.risk.capital,
                 self.cfg.get("session.entry_open"),
                 self.cfg.get("session.entry_close"), self.cfg.timezone)

        try:
            while self.running:
                try:
                    await self.cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("cycle failed: %s", exc)
                await asyncio.sleep(cycle_seconds)
        finally:
            await self.feed.close()

    async def stop(self) -> None:
        self.running = False

    def _load_predictor(self) -> None:
        if not bool(self.cfg.get("ml.enabled", False)):
            return
        try:
            from panaoptions.ml.predict import Predictor
            self._predictor = Predictor(self.cfg)
            log.info("ML filter active — signals need p > %.0f%%",
                     float(self.cfg.get("ml.min_probability", 0.65)) * 100)
        except Exception as exc:
            log.warning("ML is enabled but the model would not load (%s). "
                        "Carrying on with the rule engine alone.", exc)

    # ------------------------------------------------------------------ #
    async def cycle(self) -> dict[str, Any]:
        now = clock.now(self.cfg.timezone)
        today = now.date().isoformat()
        self.risk.roll_day(today)

        phase = clock.session_phase(self.cfg, now)
        result: dict[str, Any] = {"phase": phase, "ts": now.isoformat(),
                                  "actions": []}

        if phase == "weekend":
            return result

        # 1. Manage what is already open, always and first.
        managed = await self._manage(now)
        result["actions"].extend(managed)

        if phase == "closed":
            await self._square_off(now)
            await self._maybe_write_daily_review()
            await self._maybe_write_weekly_review()
            store.save_session(today, self.risk.state)
            return result

        # 2. The pre-market screen.
        if self._should_screen(phase, today, now):
            self.screened = await screen(self.feed, self.cfg, now)
            self._screened_on = today
            self._screened_at = now

        # 3. New entries: only in the window, only when flat.
        if phase == "entry_window":
            hunted = await self._hunt(now)
            result["actions"].extend(hunted)
        elif phase == "managing":
            await self._maybe_tighten(now)

        store.save_session(today, self.risk.state)
        return result

    # ------------------------------------------------------------------ #
    async def _hunt(self, now: datetime) -> list[str]:
        """Look for one trade among the symbols that passed the screen."""
        if self.risk.state.halted:
            return []
        if len(self.ledger.open_trades) >= int(self.cfg.get("risk.max_open_trades", 1)):
            return []

        candidates = [r.symbol for r in self.screened if r.passed]
        if not candidates:
            return []

        actions: list[str] = []
        for symbol in candidates:
            candles = await self.feed.candles(symbol, self.cfg.get("technical.timeframe", "5m"))
            if not candles:
                continue

            session_levels = await self._levels_for(symbol, now)
            setup, attempts = strategies.evaluate_all(
                symbol, candles, session_levels, self.cfg)
            signal_id = f"SIG-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4].upper()}"

            if setup is None:
                # Record every strategy that looked and passed, with its
                # reason. The rejections are the half that tells you whether
                # a rule is selective or simply impossible.
                for attempt in attempts:
                    store.save_signal_seen(
                        f"{signal_id}-{attempt.strategy.name}", now, symbol,
                        attempt.direction.value, False,
                        "; ".join(attempt.blockers) or "no setup",
                        {"strategy": attempt.strategy.value,
                         "confirmations": attempt.confirmations})
                continue

            # The ML filter, when it is switched on, is a veto — never a reason
            # to trade something the rules rejected.
            probability = None
            if self._predictor is not None:
                probability = self._predictor.probability(candles, self.cfg)
                floor = float(self.cfg.get("ml.min_probability", 0.65))
                if probability is not None and probability < floor:
                    store.save_signal_seen(
                        signal_id, now, symbol, setup.direction.value, False,
                        f"model probability {probability:.2f} below {floor:.2f}")
                    actions.append(f"{symbol}: model vetoed ({probability:.0%})")
                    continue

            search = await self._pick_contract(symbol, setup, now)
            if search.chosen is None:
                store.save_signal_seen(signal_id, now, symbol,
                                       setup.direction.value, False,
                                       search.note or "no contract qualified",
                                       {"rejected": search.rejected})
                actions.append(f"{symbol}: {search.note}")
                log.info("%s setup fired but no contract qualified. %s",
                         symbol, search.note)
                continue

            signal, refusal = self.risk.size(setup, search.chosen, signal_id,
                                             now, probability)
            if signal is None:
                store.save_signal_seen(signal_id, now, symbol,
                                       setup.direction.value, False, refusal)
                actions.append(f"{symbol}: {refusal}")
                continue

            trade = self.ledger.open(signal, now)
            store.save_signal_seen(signal_id, now, symbol,
                                   setup.direction.value, True, "taken",
                                   {"trade_id": trade.id,
                                    "strategy": setup.strategy.value})
            await self.notifier.entry(signal)
            actions.append(f"{symbol}: ENTERED {signal.alert_line()}")
            break            # one trade at a time; stop hunting

        return actions

    def _should_screen(self, phase: str, today: str, now: datetime) -> bool:
        """Is it worth running the pre-market screen right now?

        "Pre-market" covers everything from midnight to the entry window, so a
        desk left running overnight would screen at 00:01 — when there is no
        pre-market volume at all. RVOL comes back near zero, nothing passes,
        the screen is marked done for the day, and NOTHING can trade. Leaving
        the app on overnight would have been strictly worse than starting it
        in the morning, which is the opposite of the point.

        So: not before `premarket.screen_from`, and re-run while nothing has
        qualified, because the pre-market tape thickens as the open nears.
        """
        if phase not in {"premarket", "entry_window"}:
            return False

        earliest = str(self.cfg.get("premarket.screen_from", "09:00"))
        if not clock.at_or_after(self.cfg.timezone, earliest, now):
            return False

        if self._screened_on != today:
            return True

        # Already screened today. Only worth repeating while it found nothing.
        if any(r.passed for r in self.screened):
            return False
        gap = int(self.cfg.get("premarket.rescreen_minutes", 5))
        if self._screened_at is None:
            return True
        return (now - self._screened_at).total_seconds() >= gap * 60

    async def _levels_for(self, symbol: str, now: datetime):
        """Today's opening range and pre-market extremes, computed once.

        These need a pre/post-inclusive request — the regular-session tape
        does not contain pre-market bars at all — so they are fetched
        separately and cached for the day.
        """
        today = now.date().isoformat()
        if self._levels_on != today:
            self._levels.clear()
            self._levels_on = today

        cached = self._levels.get(symbol)
        # The opening range is not final until 09:45, so a partial one is
        # recomputed rather than kept.
        if cached is not None and cached.has_opening_range:
            return cached

        bars = await self.feed.candles(
            symbol, self.cfg.get("technical.timeframe", "5m"),
            include_prepost=True)
        computed = levels_mod.compute(bars, self.cfg.timezone, now.date())
        self._levels[symbol] = computed
        return computed

    async def _pick_contract(self, symbol: str, setup, now: datetime):
        spot = setup.indicators.close
        chain = await self.feed.chain_for_window(
            symbol, spot,
            int(self.cfg.get("contracts.min_dte", 7)),
            int(self.cfg.get("contracts.max_dte", 14)))
        return contract_filter.choose(symbol, chain, setup.direction, self.cfg)

    # ------------------------------------------------------------------ #
    async def _manage(self, now: datetime) -> list[str]:
        """Re-price every open trade and apply the exit rules."""
        if not self.ledger.open_trades:
            return []

        actions: list[str] = []
        for trade_id, trade in list(self.ledger.open_trades.items()):
            price = await self._contract_price(trade)
            candles = await self.feed.candles(trade.symbol,
                                              self.cfg.get("technical.timeframe", "5m"))
            underlying = candles[-1].close if candles else None
            ema_fast = None
            if candles:
                df = ta.to_frame(candles)
                ema_fast = float(ta.ema(df["close"],
                                        int(self.cfg.get("technical.fast_ema", 9))).iloc[-1])

            if price is None:
                continue
            fills = self.ledger.mark(trade_id, price, underlying, now, ema_fast)
            for fill in fills:
                actions.append(f"{trade.symbol}: {fill.reason} at {fill.price:.2f}")
            if not trade.is_open:
                store.save_trade(trade)
                await self._grade(trade)
                await self.notifier.exit(trade)
        return actions

    async def _contract_price(self, trade) -> float | None:
        """Re-price the exact contract being held."""
        chain = await self.feed.chain_for_window(
            trade.symbol, 0.0, 0, 60)
        for c in chain:
            if c.label == trade.contract_label:
                return c.mid
        log.debug("could not re-price %s this cycle", trade.contract_label)
        return None

    async def _maybe_tighten(self, now: datetime) -> None:
        """After the tighten time, pull stops to breakeven on anything green."""
        if not clock.at_or_after(self.cfg.timezone,
                                 str(self.cfg.get("session.tighten_stops_at", "10:45")),
                                 now):
            return
        for trade in self.ledger.open_trades.values():
            if not trade.breakeven_armed and trade.stop_price < trade.entry_price:
                trade.stop_price = trade.entry_price
                trade.breakeven_armed = True
                log.info("%s past the tighten time — stop moved to breakeven "
                         "%.2f", trade.contract_label, trade.stop_price)

    async def _square_off(self, now: datetime) -> None:
        if not self.ledger.open_trades:
            return
        prices: dict[str, float] = {}
        for trade in self.ledger.open_trades.values():
            price = await self._contract_price(trade)
            if price is not None:
                prices[trade.contract_label] = price
        closed = list(self.ledger.open_trades.values())
        self.ledger.close_all(prices, ExitReason.DAY_END, now)
        for trade in closed:
            store.save_trade(trade)
            await self._grade(trade)
            await self.notifier.exit(trade)

    async def _grade(self, trade) -> None:
        """Journal and grade a closed trade. Never fatal to the loop.

        A P&L number teaches nothing on its own. The card is the learning, and
        it grades the decision rather than the result.
        """
        if not bool(self.cfg.get("journal.auto_grade", True)):
            return
        try:
            from panaoptions.journal import store as journal_store
            from panaoptions.journal.grade import build_card, build_entry

            entry = build_entry(trade, self.cfg)
            card = await build_card(entry, self.cfg)
            journal_store.save_entry(entry)
            journal_store.save_card(card)
            journal_store.export_index()
            log.info("graded %s → %s (%d/10)%s", trade.contract_label,
                     entry.verdict.value if entry.verdict else "?",
                     entry.execution_score,
                     f" — {', '.join(m.value for m in entry.mistakes)}"
                     if entry.mistakes else "")
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not grade %s: %s", trade.id, exc)

    async def _maybe_write_daily_review(self) -> None:
        """Write the day's review once the session is over, once per day.

        A day is a diary entry rather than evidence — it says how the session
        was executed, not which strategy works. That judgement needs the week,
        and the review says so rather than letting one good day read as proof.
        """
        if not bool(self.cfg.get("journal.auto_daily_review", True)):
            return
        try:
            from panaoptions.journal import weekly

            day = weekly.today(self.cfg)
            if self._daily_written_for == day.isoformat():
                return
            if not weekly.session_over(self.cfg, day):
                return

            review = await weekly.build_daily(self.cfg, day)
            self._daily_written_for = day.isoformat()
            if review.trades:
                weekly.save(review, self.cfg)
            else:
                log.info("no trades to review for %s", day)
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not write the daily review: %s", exc)
            from panaoptions.journal import weekly
            self._daily_written_for = weekly.today(self.cfg).isoformat()

    async def _maybe_write_weekly_review(self) -> None:
        """Have the week's review waiting once Friday's session has closed."""
        if not bool(self.cfg.get("journal.auto_weekly_review", True)):
            return
        try:
            from panaoptions.journal import weekly

            start, end = weekly.current_week(self.cfg)
            if self._weekly_written_for == end.isoformat():
                return
            if not weekly.is_complete(end, self.cfg):
                return

            review = await weekly.build(self.cfg, start, end)
            self._weekly_written_for = end.isoformat()
            if review.trades:
                weekly.save(review, self.cfg)
            else:
                log.info("no graded trades in the week to %s", end)
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not write the weekly review: %s", exc)
            from panaoptions.journal import weekly
            self._weekly_written_for = weekly.current_week(self.cfg)[1].isoformat()

    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        now = clock.now(self.cfg.timezone)
        return {
            "phase": clock.session_phase(self.cfg, now),
            "market_time": now.strftime("%Y-%m-%d %H:%M %Z"),
            "risk": self.risk.describe(),
            "ledger": self.ledger.stats(),
            "screened": [r.model_dump() for r in self.screened],
            "open_trades": [t.model_dump(mode="json")
                            for t in self.ledger.open_trades.values()],
            "ml_enabled": self._predictor is not None,
            "notifications": self.notifier.enabled,
            "paper_only": True,
        }
