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
import time
import uuid
from datetime import datetime
from typing import Any

from panaoptions import clock, watchlist
from panaoptions.activity import ActivityLog
from panaoptions.config import Config, get_config
from panaoptions.data.premarket import screen
from panaoptions.data.provider import make_feed
from panaoptions.engine import contracts as contract_filter
from panaoptions.engine import indicators as ta
from panaoptions.engine import levels as levels_mod
from panaoptions.engine import strategies
from panaoptions.ledger import store
from panaoptions.ledger.paper import PaperLedger
from panaoptions.logging import get_logger
from panaoptions.models import Direction, ExitReason, PreMarketRead
from panaoptions.notify.webhook import Notifier
from panaoptions.risk.guardrails import RiskManager

log = get_logger("app")


class OptionsDesk:
    def __init__(self, cfg: Config | None = None, feed: Any | None = None) -> None:
        self.cfg = cfg or get_config()
        self.feed = feed or make_feed(self.cfg)
        self.risk = RiskManager(self.cfg)
        self.ledger = PaperLedger(self.cfg, self.risk)
        self.notifier = Notifier(self.cfg)
        # What the desk just did, so a working desk and a hung one look
        # different from the outside.
        self.activity = ActivityLog()
        self.running = False
        self.screened: list[PreMarketRead] = []
        self._screened_on: str = ""
        self._screened_at: datetime | None = None
        # Opening range and pre-market extremes, per symbol, for today.
        self._levels: dict[str, Any] = {}
        self._levels_on: str = ""
        self._weekly_written_for: str | None = None
        self._daily_written_for: str | None = None
        # What the desk is judging right now, and the last case it made for a
        # trade — so the dashboard can show the reasoning, not just the result.
        self.scanning: str = ""
        self.candidate: dict[str, Any] | None = None
        # How long the last cycle took, and what the desk intends between
        # them. Both shown on the dashboard, because "every 60 seconds" is a
        # claim that ought to be checkable.
        self.cycle_seconds: int = 60
        self.last_cycle_seconds: float = 0.0
        self._predictor: Any = None
        # A watchlist saved from the dashboard wins over the config universe.
        # Applied at construction so a restart keeps scanning what was asked
        # for rather than quietly reverting to the shipped list.
        saved = watchlist.load()
        if saved:
            self.cfg.data.setdefault("universe", {})["symbols"] = list(saved)
            log.info("watchlist in force: %s", ", ".join(saved))

    # ------------------------------------------------------------------ #
    def set_universe(self, symbols: list[str]) -> list[str]:
        """Change what the desk scans, now, without a restart.

        Open positions are left alone on purpose: they were opened under rules
        that still apply, and their exits are already defined. Dropping a
        symbol stops the desk looking for NEW trades in it — it does not
        liquidate what is already on, because a watchlist edit is not a
        trading decision and must not become one by accident.
        """
        self.cfg.data.setdefault("universe", {})["symbols"] = list(symbols)
        watchlist.save(symbols)
        # Force a re-screen on the next cycle rather than waiting for
        # tomorrow: the point of typing a symbol is to have it looked at.
        self._screened_on = ""
        self._screened_at = None
        self.screened = []
        self._levels = {}
        self._levels_on = ""
        self.activity.add(
            "watchlist",
            f"now scanning {len(symbols)}: {', '.join(symbols)}"
            + (f" — {len(self.ledger.open_trades)} open position(s) left "
               f"to their own exit rules" if self.ledger.open_trades else ""),
            level="good")
        log.info("watchlist set: %s", ", ".join(symbols))
        return symbols

    def reset_universe(self) -> list[str]:
        """Back to `universe.symbols` from the config file."""
        watchlist.clear()
        self.cfg.reload()
        symbols = self.cfg.symbols
        self._screened_on = ""
        self._screened_at = None
        self.screened = []
        self.activity.add("watchlist",
                          f"back to the config universe: {', '.join(symbols)}",
                          level="info")
        return symbols

    # ------------------------------------------------------------------ #
    async def start(self, cycle_seconds: int = 60) -> None:
        self.cycle_seconds = cycle_seconds
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
                 self.cfg.last_entry_hhmm, self.cfg.timezone)

        try:
            while self.running:
                started = time.monotonic()
                try:
                    await self.cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.activity.add("error", str(exc)[:200], level="bad")
                    log.exception("cycle failed: %s", exc)

                # Sleep the REMAINDER, not the whole interval. Sleeping the
                # full amount after the work makes the real period
                # `work + interval`: with three symbols that is nearer 65
                # seconds than 60, and the desk drifts a bar further behind
                # every cycle while claiming to run every minute.
                elapsed = time.monotonic() - started
                self.last_cycle_seconds = round(elapsed, 2)
                if elapsed > cycle_seconds:
                    # It cannot keep up. Say so — a desk quietly running at
                    # half its stated rate looks identical to one that is fine.
                    self.activity.add(
                        "slow.cycle",
                        f"a cycle took {elapsed:.0f}s against a "
                        f"{cycle_seconds}s interval — the desk is behind. "
                        f"Fewer symbols, or a longer --interval.",
                        level="warn")
                    log.warning("cycle took %.1fs, longer than the %ds "
                                "interval", elapsed, cycle_seconds)
                await asyncio.sleep(max(0.0, cycle_seconds - elapsed))
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

        self.activity.add("cycle.start", phase, ts=now)

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
            passed = [r.symbol for r in self.screened if r.passed]
            self.activity.add(
                "screen.done",
                f"{len(passed)}/{len(self.screened)} passed"
                + (f" — {', '.join(passed)}" if passed else ""),
                level="good" if passed else "info", ts=now)

        # 3. Tightening is a clock event, not a phase one. The entry window now
        # runs to the last strategy's close, which is well past the tighten
        # time — so gating this on the "managing" phase would mean a trade
        # opened at 10:00 keeps its full stop until 13:30.
        await self._maybe_tighten(now)

        # 4. New entries: only in the window, only when flat.
        if phase == "entry_window":
            hunted = await self._hunt(now)
            result["actions"].extend(hunted)

        store.save_session(today, self.risk.state)
        return result

    # ------------------------------------------------------------------ #
    async def _hunt(self, now: datetime) -> list[str]:
        """Look for trades among the symbols that passed the screen.

        Every symbol is read at the same moment and judged against every
        enabled strategy, and the desk takes as many as it has room for —
        `risk.max_open_trades` positions, inside the `max_total_deployed_pct`
        ceiling.
        """
        if self.risk.state.halted:
            self.scanning = ""
            return []
        max_open = int(self.cfg.get("risk.max_open_trades", 1))
        if len(self.ledger.open_trades) >= max_open:
            # Not scanning anything, and the panel must not keep showing the
            # last symbol it looked at as though it still were.
            self.scanning = ""
            self.activity.add(
                "hunt.skip",
                f"holding {len(self.ledger.open_trades)} of {max_open} "
                f"allowed — not looking for new trades until one closes",
                ts=now)
            return []

        candidates = [r.symbol for r in self.screened if r.passed]
        if not candidates:
            self.activity.add("hunt.skip", "nothing passed the pre-market screen",
                              ts=now)
            return []

        # Read every symbol's tape AT ONCE rather than one after another.
        # Sequentially, five symbols is five round trips to Yahoo laid end to
        # end — several seconds during which the first symbol's chart is going
        # stale while the last one is still being fetched. Gathered, they are
        # all read at the same moment, which is also the only way the setups
        # are comparable: a cycle should judge one instant, not a smear of
        # five.
        self.scanning = ", ".join(candidates)
        tapes = await asyncio.gather(
            *(self._tape(symbol, now) for symbol in candidates),
            return_exceptions=True)

        actions: list[str] = []
        # Said once per cycle, not once per symbol: the window is the same for
        # all of them, and five copies of it would bury everything else.
        window_reported = False
        for symbol, tape in zip(candidates, tapes, strict=False):
            if isinstance(tape, Exception):
                log.warning("could not read %s this cycle: %s", symbol, tape)
                self.activity.add("error", f"{symbol} — {tape}"[:200],
                                  level="bad", ts=now)
                continue
            candles, session_levels = tape
            if not candles:
                continue

            # Room can run out part-way through: three slots and four setups
            # means the fourth is refused, and that refusal belongs in the log
            # rather than being silently skipped.
            if len(self.ledger.open_trades) >= max_open:
                self.activity.add(
                    "hunt.skip",
                    f"{max_open} position(s) open — {symbol} and anything "
                    f"after it were not judged this cycle", ts=now)
                break

            setup, attempts = strategies.evaluate_all(
                symbol, candles, session_levels, self.cfg)
            signal_id = f"SIG-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4].upper()}"

            # No attempts at all means no strategy was even asked — every one
            # of them is outside its own window. That is a completely
            # different state from "they all looked and passed", and logging
            # nothing makes the two identical on screen: symbols pass the
            # screen, nothing trades, and the log is silent.
            if not attempts:
                if not window_reported:
                    window_reported = True
                    self.activity.add("hunt.skip", self._window_note(now),
                                      ts=now)
                continue

            if setup is None:
                for attempt in attempts:
                    self.activity.add(
                        "setup.pass",
                        f"{symbol} {attempt.strategy.value} — "
                        + (attempt.blockers[0] if attempt.blockers else "no setup"),
                        ts=now)
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

            self.activity.add(
                "setup.fired",
                f"{symbol} {setup.strategy.value} {setup.direction.value} — "
                f"{setup.pattern}", level="good", ts=now)
            self._remember_candidate(symbol, setup, now)

            search = await self._pick_contract(symbol, setup, now)
            if search.chosen is None:
                store.save_signal_seen(signal_id, now, symbol,
                                       setup.direction.value, False,
                                       search.note or "no contract qualified",
                                       {"rejected": search.rejected})
                actions.append(f"{symbol}: {search.note}")
                self.activity.add("contract.none", f"{symbol} — {search.note}",
                                  level="warn", ts=now)
                log.info("%s setup fired but no contract qualified. %s",
                         symbol, search.note)
                continue

            signal, refusal = self.risk.size(setup, search.chosen, signal_id,
                                             now, probability)
            if signal is None:
                store.save_signal_seen(signal_id, now, symbol,
                                       setup.direction.value, False, refusal)
                actions.append(f"{symbol}: {refusal}")
                self.activity.add("risk.refused", f"{symbol} — {refusal}",
                                  level="warn", ts=now)
                continue

            trade = self.ledger.open(signal, now)
            if self.candidate and self.candidate.get("symbol") == symbol:
                self.candidate["taken"] = True
                self.candidate["trade_id"] = trade.id
                self.candidate["contract"] = signal.contract.label
                self.candidate["entry"] = signal.entry_price
                self.candidate["quantity"] = signal.quantity
            store.save_signal_seen(signal_id, now, symbol,
                                   setup.direction.value, True, "taken",
                                   {"trade_id": trade.id,
                                    "strategy": setup.strategy.value})
            await self.notifier.entry(signal)
            actions.append(f"{symbol}: ENTERED {signal.alert_line()}")
            self.activity.add(
                "trade.open",
                f"{signal.alert_line()} — {setup.strategy.value}, "
                f"x{signal.quantity}",
                level="good", ts=now)
            # Keep going while there are slots left. Stopping after the first
            # entry would make max_open_trades a limit the desk could only
            # reach one cycle at a time, so a second setup on another symbol
            # in the same minute would simply be missed.

        self.scanning = ""
        return actions

    def _window_note(self, now: datetime) -> str:
        """Why nothing was judged, and when that changes."""
        current = now.hour * 60 + now.minute
        soonest, name = None, ""
        for factory in strategies.ALL:
            strategy = factory(self.cfg)
            if not strategy.enabled:
                continue
            opens = strategy.opens.hour * 60 + strategy.opens.minute
            if opens > current and (soonest is None or opens < soonest):
                soonest, name = opens, strategy.name.value
        if soonest is None:
            return ("every strategy window has closed for today — managing "
                    "open positions only")
        wait = soonest - current
        return (f"no strategy is in its window yet — {name} opens at "
                f"{soonest // 60:02d}:{soonest % 60:02d}, in "
                f"{wait} minute{'s' if wait != 1 else ''}")

    async def _tape(self, symbol: str, now: datetime):
        """One symbol's candles and session levels, fetched together."""
        candles = await self.feed.candles(
            symbol, self.cfg.get("technical.timeframe", "5m"))
        levels = await self._levels_for(symbol, now)
        return candles, levels

    def _remember_candidate(self, symbol: str, setup, now: datetime) -> None:
        """Keep the case for the newest setup, for the dashboard to show.

        A signal on its own tells you what happened. This is the why — the
        pattern, the level it formed at, the trigger, and what would kill it —
        which is the only part you can actually learn from.
        """
        self.candidate = {
            "symbol": symbol,
            "ts": now.isoformat(),
            "strategy": setup.strategy.value,
            "direction": setup.direction.value,
            "right": "CALL" if setup.direction is Direction.LONG else "PUT",
            "pattern": setup.pattern,
            "reasoning": list(setup.reasoning),
            "confirmations": list(setup.confirmations),
            "key_level": setup.key_level,
            "key_level_source": setup.key_level_source,
            "entry_trigger": setup.entry_trigger,
            "invalidation": setup.underlying_support,
            "invalidation_note": setup.invalidation_note,
            "delta_band": list(setup.delta_band) if setup.delta_band else None,
            "underlying": setup.indicators.close,
            "taken": False,
        }

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
        min_dte = setup.min_dte_override or int(self.cfg.get("contracts.min_dte", 7))
        max_dte = setup.max_dte_override or int(self.cfg.get("contracts.max_dte", 14))
        chain = await self.feed.chain_for_window(symbol, spot, min_dte, max_dte)
        search = contract_filter.choose(symbol, chain, setup.direction,
                                        self.cfg, setup=setup)

        # An empty chain has two very different causes and one useless
        # message. "No put contracts came back" reads as "the market has no
        # puts today"; nine times in ten it means the request was refused, and
        # the feed now knows which.
        if not chain:
            reason = getattr(self.feed, "options_error", "")
            if reason:
                search.note = (
                    f"No contracts for {symbol} at {min_dte}-{max_dte} DTE — "
                    f"{reason}")
        return search

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
                self.activity.add(
                    "trade.exit",
                    f"{trade.contract_label} {fill.reason} at {fill.price:.2f} "
                    f"({trade.realised_pnl:+,.2f})",
                    level="good" if trade.realised_pnl >= 0 else "bad", ts=now)
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
        """After the tighten time, pull stops to breakeven on anything green.

        Only on trades that were ALREADY open at the tighten time, and only
        while they are in profit. It used to move every open stop to entry
        regardless: a trade opened at 13:00 started life with its stop at its
        own entry price — out at the first tick against it — and, because that
        also armed breakeven, its first-target scale-out and its time exit
        were switched off too. The tighten time protects a morning trade going
        into the lunch slump; an afternoon entry has its own exits.
        """
        cutoff = str(self.cfg.get("session.tighten_stops_at", "10:45"))
        if not clock.at_or_after(self.cfg.timezone, cutoff, now):
            return
        tighten_at = clock.parse_hhmm(cutoff)
        for trade in self.ledger.open_trades.values():
            if trade.breakeven_armed or trade.stop_price >= trade.entry_price:
                continue
            opened = trade.opened_at.astimezone(now.tzinfo) if (
                trade.opened_at.tzinfo and now.tzinfo) else trade.opened_at
            if opened.timetz().replace(tzinfo=None) >= tighten_at:
                continue
            if trade.last_price <= trade.entry_price:
                continue
            trade.stop_price = trade.entry_price
            trade.breakeven_armed = True
            log.info("%s green past the tighten time — stop moved to "
                     "breakeven %.2f", trade.contract_label, trade.stop_price)

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
            self.activity.add(
                "graded",
                f"{trade.contract_label} {entry.verdict.value if entry.verdict else '?'} "
                f"({entry.execution_score}/10)"
                + (f" — {', '.join(m.value for m in entry.mistakes)}"
                   if entry.mistakes else ""),
                level="bad" if entry.mistakes else "good")
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
            "scanning": self.scanning,
            "candidate": self.candidate,
            "cycle": {"seconds": self.cycle_seconds,
                      "last_took": self.last_cycle_seconds,
                      "symbols": len(self.cfg.symbols)},
            "ml_enabled": self._predictor is not None,
            "notifications": self.notifier.enabled,
            "paper_only": True,
        }
