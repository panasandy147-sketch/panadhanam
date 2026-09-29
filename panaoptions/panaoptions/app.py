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
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from panaoptions import alpha, auto_watchlist, clock, markets, watchlist
from panaoptions.activity import ActivityLog
from panaoptions.agents import CMIO
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
from panaoptions.models import Direction, ExitReason, PreMarketRead, option_side
from panaoptions.notify.webhook import Notifier
from panaoptions.risk.gatekeeper import RiskGatekeeper
from panaoptions.risk.guardrails import RiskManager

log = get_logger("app")


class OptionsDesk:
    def __init__(self, cfg: Config | None = None, feed: Any | None = None) -> None:
        self.cfg = cfg or get_config()
        # Each market keeps its own ledger, journal and audit log.
        markets.activate(self.cfg.market)
        from panaoptions.config import saved_market_mode
        # US, IN, or AUTO (follow whichever session is open).
        self.market_mode: str = saved_market_mode() if feed is None else self.cfg.market
        self._feed_factory = make_feed
        self._lock = asyncio.Lock()
        self.feed = feed or make_feed(self.cfg)
        self.risk = RiskManager(self.cfg)
        self.ledger = PaperLedger(self.cfg, self.risk)
        # Signals pass the committee and then the gatekeeper before any order.
        self.gatekeeper = RiskGatekeeper(self.cfg, self.risk)
        self.cmio = CMIO(self.cfg, self.gatekeeper)
        # One committee vote per setup per bar: a setup that stays valid for
        # several cycles is not re-argued (and Ollama not re-asked) each minute.
        self._verdicts: dict[tuple, Any] = {}
        # Every chain read is sampled, so the spread check can use the rolling
        # 1-minute volume-weighted spread instead of one snapshot.
        from panaoptions.engine.liquidity import SpreadTracker
        self.spreads = SpreadTracker(float(self.cfg.get(
            "contracts.rolling_spread.window_seconds", 60)))
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
        self._reflection_task: asyncio.Task | None = None
        # The auto watchlist's sources (Yahoo screeners, trending, analyst
        # ratings). Built on first use; tests put a fake here.
        self.discovery: Any = None
        self._watch_refreshing = False
        # A watchlist saved from the dashboard, or the auto list, wins over
        # the config universe. Applied at construction so a restart keeps
        # scanning what was asked for rather than the shipped list.
        self._apply_saved_universe()

    def _apply_saved_universe(self) -> None:
        how = auto_watchlist.mode(self.cfg)
        saved: list[str] = []
        if how == "custom":
            saved = watchlist.load()
        elif how == "auto":
            saved = [s for s in auto_watchlist.load_state().get("symbols") or []
                     if isinstance(s, str)][:watchlist.MAX_SYMBOLS]
        if saved:
            self.cfg.data.setdefault("universe", {})["symbols"] = list(saved)
            log.info("watchlist in force (%s): %s", how, ", ".join(saved))

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
        # A list typed by a person is theirs: the hourly refresh leaves it be.
        auto_watchlist.set_mode("custom")
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
        auto_watchlist.set_mode("config")
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
    # The auto watchlist
    # ------------------------------------------------------------------ #
    def _held_symbols(self) -> list[str]:
        out: list[str] = []
        for trade in self.ledger.open_trades.values():
            if trade.symbol.upper() not in out:
                out.append(trade.symbol.upper())
        return out

    async def _maybe_refresh_watchlist(self, now: datetime) -> None:
        """Before the open, then hourly — only while the auto list is on."""
        if auto_watchlist.mode(self.cfg) != "auto":
            return
        kind = auto_watchlist.due(self.cfg, now, auto_watchlist.load_state())
        if kind:
            await self.refresh_watchlist(now, kind)

    async def refresh_watchlist(self, now: datetime | None = None,
                                kind: str = "hourly") -> dict[str, Any]:
        """Re-rank the candidates and apply the new list.

        Symbols with an open position are never removed — they stay until the
        trade closes and are replaceable only at the next refresh after that.
        If every source fails, the list in force stays.
        """
        now = now or clock.now(self.cfg.timezone)
        today = now.date().isoformat()
        current = list(self.cfg.symbols)
        held = self._held_symbols()
        pool, restrict = auto_watchlist.pool_for(self.cfg, current, held)
        if self.discovery is None:
            self.discovery = auto_watchlist.YahooDiscovery()
        self._watch_refreshing = True
        try:
            found, sources = await asyncio.wait_for(
                auto_watchlist.gather(self.discovery, self.feed, self.cfg, pool, now),
                timeout=float(self.cfg.get("auto_watchlist.timeout_seconds", 45)))
        except TimeoutError:
            found, sources = {}, {"all": "timed out"}
        finally:
            self._watch_refreshing = False

        ranked, refused = auto_watchlist.rank(found, self.cfg, now, restrict)
        fallback = [str(s).upper() for s in
                    self.cfg.get("auto_watchlist.always_consider", []) or []] or current
        sel = auto_watchlist.select(current, ranked, held, self.cfg,
                                    fresh=kind == "morning", fallback=fallback,
                                    judged=set(found))
        symbols = sel.symbols[:max(watchlist.MAX_SYMBOLS, len(held))]

        every = int(self.cfg.get("auto_watchlist.refresh_minutes", 60))
        stamp = now
        if not ranked:
            # Nothing answered: try again in 10 minutes, not an hour.
            retry = int(self.cfg.get("auto_watchlist.retry_minutes", 10))
            stamp = now - timedelta(minutes=max(every - retry, 0))
        table = [c.row() for c in sorted(ranked.values(), key=lambda c: c.score,
                                         reverse=True)][:25]
        state = auto_watchlist.load_state()
        state.update(mode="auto", day=today, last_refresh=stamp.isoformat(),
                     refreshed_at=now.isoformat(), kind=kind, symbols=symbols,
                     table=table, held=sel.held, notes=sel.notes, sources=sources,
                     refused=dict(list(refused.items())[:30]))
        auto_watchlist.save_state(state)
        auto_watchlist.log_refresh(now, {
            "market": self.cfg.market, "kind": kind, "symbols": symbols,
            "added": sel.added, "removed": sel.removed, "held": sel.held,
            "notes": sel.notes, "sources": sources, "top": table[:15]})

        if symbols != current:
            self.cfg.data.setdefault("universe", {})["symbols"] = list(symbols)
            kept = set(symbols)
            self.screened = [r for r in self.screened if r.symbol in kept]
            # Already screened today: screen just the newcomers, now.
            if self._screened_on == today and sel.added:
                extra = await screen(self.feed, self.cfg, now, symbols=sel.added)
                self.screened.extend(extra)
        label = "pre-open top" if kind == "morning" else "hourly refresh —"
        change = (f"added {', '.join(sel.added)}" if sel.added else "no change")
        if sel.removed:
            change += f"; removed {', '.join(sel.removed)}"
        if sel.held:
            change += f"; kept {', '.join(sel.held)} (position open)"
        if not ranked:
            change += " — no source answered, keeping the list; retrying in 10 min"
        self.activity.add("watchlist.auto",
                          f"{label} {len(symbols)}: {', '.join(symbols)} ({change})",
                          level="good" if ranked else "warn", ts=now)
        log.info("auto watchlist (%s): %s", kind, ", ".join(symbols))
        return state

    async def set_auto_watchlist(self, enabled: bool) -> dict[str, Any]:
        """The Auto button. On: rank now and follow the hourly refresh. Off:
        back to the universe in settings.yaml."""
        if not enabled:
            self.reset_universe()
            return self.watchlist_status()
        if not auto_watchlist.available(self.cfg):
            raise ValueError("the auto watchlist is switched off "
                             "(auto_watchlist.enabled in settings.yaml)")
        auto_watchlist.set_mode("auto")
        async with self._lock:
            await self.refresh_watchlist(clock.now(self.cfg.timezone), "morning")
        return self.watchlist_status()

    def watchlist_status(self) -> dict[str, Any]:
        how = auto_watchlist.mode(self.cfg)
        state = auto_watchlist.load_state() if how == "auto" else {}
        return {
            "symbols": self.cfg.symbols, "source": how,
            "config_symbols": list((self.cfg.get("universe", {}) or {}).get("symbols", [])),
            "max": watchlist.MAX_SYMBOLS,
            "auto": {
                "available": auto_watchlist.available(self.cfg),
                "on": how == "auto",
                "size": int(self.cfg.get("auto_watchlist.size", 10)),
                "refresh_minutes": int(self.cfg.get("auto_watchlist.refresh_minutes", 60)),
                "start": str(self.cfg.get("auto_watchlist.start", "08:45")),
                "stop": str(self.cfg.get("auto_watchlist.stop", "15:00")),
                "refreshed_at": state.get("refreshed_at", ""),
                "next_refresh": auto_watchlist.next_refresh(self.cfg, state),
                "kind": state.get("kind", ""),
                "table": state.get("table", []),
                "held": self._held_symbols(),
                "notes": state.get("notes", []),
                "sources": state.get("sources", {}),
                "refreshing": self._watch_refreshing,
            },
        }

    # ------------------------------------------------------------------ #
    async def start(self, cycle_seconds: int = 60) -> None:
        self.cycle_seconds = cycle_seconds
        # On Auto, start on whichever market's day it is.
        if self.market_mode == "AUTO":
            target = markets.which_now()
            if target and target != self.cfg.market:
                self._rebuild(target, self._feed_factory)
        store.init()
        self._restore_open_book()
        if not await self.feed.connect():
            log.error("no market data — refusing to start. A desk that cannot "
                      "see prices must not pretend to trade.")
            return

        # Say up front if the rules cannot all hold. Discovering it by watching
        # the desk take nothing for a fortnight is the expensive way.
        from panaoptions import preflight
        preflight.report(self.cfg)

        self._load_predictor()
        # A desk stopped before Friday's close never saw the week end; catch
        # the reflection up in the background rather than skip a week.
        self._reflection_task = asyncio.create_task(self._catch_up_reflection())
        self.running = True
        log.info("panaoptions desk started — paper only, capital $%.2f, "
                 "entries %s-%s %s", self.risk.capital,
                 self.cfg.get("session.entry_open"),
                 self.cfg.last_entry_hhmm, self.cfg.timezone)

        try:
            while self.running:
                started = time.monotonic()
                try:
                    await self.follow_the_clock()
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

    # ------------------------------------------------------------------ #
    # Markets: the US, India, or Auto
    # ------------------------------------------------------------------ #
    def _rebuild(self, code: str, factory: Any) -> None:
        """Re-point everything market-bound at `code`. The caller has made
        sure nothing is open and no cycle is running."""
        markets.activate(code)
        self.cfg.market = code
        self.cfg.reload()
        self._apply_saved_universe()
        self.feed = factory(self.cfg)
        self.risk = RiskManager(self.cfg)
        self.ledger = PaperLedger(self.cfg, self.risk)
        self.gatekeeper = RiskGatekeeper(self.cfg, self.risk)
        self.cmio = CMIO(self.cfg, self.gatekeeper)
        self.notifier = Notifier(self.cfg)
        self._verdicts = {}
        self.screened, self._screened_on, self._screened_at = [], "", None
        self._levels, self._levels_on = {}, ""
        self.candidate, self.scanning = None, ""

    async def switch_market(self, code: str, feed_factory: Any | None = None
                            ) -> dict[str, Any]:
        """Trade `code` from the next cycle. Refused while a position is open."""
        code = code.upper()
        if code not in markets.MARKETS:
            return {"switched": False, "market": self.cfg.market,
                    "reason": f"unknown market {code!r}"}
        if code == self.cfg.market:
            return {"switched": False, "market": code, "reason": "already active"}
        if self.ledger.open_trades:
            return {"switched": False, "market": self.cfg.market,
                    "reason": (f"{len(self.ledger.open_trades)} position(s) open on "
                               f"{self.cfg.market} — they close by their own rules "
                               f"first; the new market's feed could not price them")}
        async with self._lock:
            old = self.feed
            previous = self.cfg.market
            self._rebuild(code, feed_factory or self._feed_factory)
            store.init()
            self._restore_open_book()
            connected = await self.feed.connect() if self.running else None
            try:
                await old.close()
            except Exception:                           # noqa: BLE001
                pass
        self.activity.add("market", f"switched {previous} → {code} "
                          f"({markets.NAMES.get(code, code)}), capital "
                          f"{self.cfg.currency}{self.cfg.capital:,.0f}", level="good")
        log.info("market switched %s → %s", previous, code)
        return {"switched": True, "market": code, "feed_connected": connected}

    async def follow_the_clock(self) -> str | None:
        """On Auto, move to whichever market's working day it is — never
        while a position is open, and never away from a session in progress."""
        if self.market_mode != "AUTO":
            return None
        target = markets.which_now()
        if not target or target == self.cfg.market:
            return None
        if markets.in_hours(self.cfg.market):
            return None
        result = await self.switch_market(target)
        return target if result.get("switched") else None

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
    def _restore_open_book(self) -> None:
        """Pick up the positions a restart would otherwise have dropped."""
        restored = [t for t in store.load_open_book() if t.is_open
                    and t.id not in self.ledger.open_trades]
        for trade in restored:
            self.ledger.open_trades[trade.id] = trade
        if restored:
            self.risk.state.open_trades = len(self.ledger.open_trades)
            self.risk.state.deployed = self.ledger._deployed()
            labels = ", ".join(t.contract_label for t in restored)
            log.info("restored %d open position(s): %s", len(restored), labels)
            self.activity.add("restored", f"{len(restored)} open position(s) "
                              f"picked up after the restart: {labels}")

    async def cycle(self) -> dict[str, Any]:
        async with self._lock:
            return await self._locked_cycle()

    async def _locked_cycle(self) -> dict[str, Any]:
        try:
            return await self._cycle()
        finally:
            # Whatever the cycle opened, marked, tightened or closed, the book
            # on disk now matches the book in memory.
            try:
                store.save_open_book(list(self.ledger.open_trades.values()))
            except Exception as exc:                   # noqa: BLE001
                log.warning("could not save the open book: %s", exc)

    async def _cycle(self) -> dict[str, Any]:
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

        # 2. The auto watchlist: built before the open, re-checked hourly.
        await self._maybe_refresh_watchlist(now)

        # 3. The pre-market screen.
        if self._should_screen(phase, today, now):
            self.screened = await screen(self.feed, self.cfg, now)
            self._screened_on = today
            self._screened_at = now
            passed = [r.symbol for r in self.screened if r.passed]
            self.activity.add(
                "screen.done", self._screen_summary(passed),
                level="good" if passed else "info", ts=now)

        # 3b. The previous day's F&O picture, once a day after the screen.
        await self._maybe_ingest_fno(now)

        # 4. Tightening is a clock event, not a phase one. The entry window now
        # runs to the last strategy's close, which is well past the tighten
        # time — so gating this on the "managing" phase would mean a trade
        # opened at 10:00 keeps its full stop until 13:30.
        await self._maybe_tighten(now)

        # 5. New entries: only in the window, only when flat.
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

            busy = self._symbol_busy(symbol, now)
            if busy:
                self.activity.add("hunt.skip", f"{symbol} — {busy}", ts=now)
                continue

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

            # The alpha engine's view: five numbers, no money in them.
            signal_a = alpha.from_setup(setup)
            if signal_a is None:
                self.activity.add(
                    "setup.pass",
                    f"{symbol} {setup.strategy.value} — its invalidation level "
                    f"is on the wrong side of the entry; not a usable signal",
                    ts=now)
                continue

            side = option_side(setup.direction)
            self.activity.add(
                "setup.fired",
                f"{symbol} {side} {setup.strategy.value} — "
                f"{setup.pattern} (trigger {signal_a.trigger_price:.2f}, "
                f"invalid at {signal_a.invalidation_level:.2f}, confidence "
                f"{signal_a.confidence_score:.2f})", level="good", ts=now)
            self._remember_candidate(symbol, setup, now)

            # Previous-day F&O confluence: reversals only at a PDL sweep with
            # rising call OI, or a PDH test with rising put OI.
            from panaoptions import fno
            from panaoptions.engine import confluence, reward
            if confluence.applies(setup, self.cfg):
                why, notes = confluence.check(setup, session_levels,
                                              fno.read_oi(symbol, now), self.cfg)
                if why:
                    self._candidate_refused(symbol, why)
                    store.save_signal_seen(signal_id, now, symbol, setup.direction.value,
                                           False, f"F&O confluence: {why}",
                                           {"strategy": setup.strategy.value,
                                            "pattern": setup.pattern, "side": side})
                    self.activity.add("confluence.refused", f"{symbol} {side} — {why}",
                                      level="warn", ts=now)
                    continue
                setup.confirmations.extend(notes)
                self.activity.add("confluence.ok", f"{symbol} {side} — {'; '.join(notes)}",
                                  level="good", ts=now)

            # The 1:3 gate: a projected target at least 3x the distance to the
            # invalidation, with open road to it.
            target, rr, why, note = reward.project(setup, session_levels, self.cfg)
            if why:
                self._candidate_refused(symbol, f"reward:risk — {why}")
                store.save_signal_seen(signal_id, now, symbol, setup.direction.value,
                                       False, f"reward:risk: {why}",
                                       {"strategy": setup.strategy.value,
                                        "pattern": setup.pattern, "side": side})
                self.activity.add("risk.refused",
                                  f"{symbol} {side} SKIPPED — hard risk failure: "
                                  f"reward:risk {why}", level="bad", ts=now)
                self._record_skip(symbol, side, setup, "reward:risk", why)
                continue
            if target:
                setup.underlying_target = target
                signal_a = replace(signal_a, target_price=target)
                if note:
                    setup.confirmations.append(note)

            search, chain = await self._pick_contract(symbol, setup, now)
            if search.chosen is not None and search.tier == "debit_spread":
                self.activity.add("contract.spread",
                                  f"{symbol} {setup.strategy.value} — {search.note}",
                                  level="good", ts=now)
            elif search.chosen is not None and search.budget_fallback:
                self.activity.add("contract.fallback", f"{symbol} — {search.note}",
                                  level="warn", ts=now)
            if search.chosen is None:
                self._candidate_refused(symbol, search.note or "no contract qualified")
                store.save_signal_seen(signal_id, now, symbol,
                                       setup.direction.value, False,
                                       search.note or "no contract qualified",
                                       {"rejected": search.rejected,
                                        "skipped_because": search.skipped_because,
                                        "strategy": setup.strategy.value,
                                        "pattern": setup.pattern,
                                        "spot": setup.indicators.close,
                                        "budget": self.gatekeeper.budget_for(symbol)})
                actions.append(f"{symbol}: {search.note}")
                hard = bool(search.skipped_because)
                self.activity.add(
                    "contract.skip" if hard else "contract.none",
                    f"{symbol} {side} {setup.strategy.value} — "
                    + (search.note if hard else search.note or "no contract qualified"),
                    level="bad" if hard else "warn", ts=now)
                if hard:
                    self._record_skip(symbol, side, setup, "contract ladder", search.note)
                log.info("%s setup fired but no contract qualified. %s",
                         symbol, search.note)
                continue

            if bool(self.cfg.get("agents.enabled", True)):
                verdict = await self._convene(symbol, signal_a, setup, candles,
                                              chain, search, now)
                if self.candidate and self.candidate.get("symbol") == symbol:
                    self.candidate["verdict"] = verdict.to_dict()
                    self.candidate.setdefault("reasoning", []).append(
                        f"**Committee.** {verdict.summary()}")
                if not verdict.approved:
                    if verdict.gate is not None and not verdict.gate.approved:
                        self._record_skip(symbol, side, setup, "Risk Gatekeeper",
                                          verdict.gate.reason)
                    self._candidate_refused(symbol, verdict.reason)
                    store.save_signal_seen(signal_id, now, symbol,
                                           setup.direction.value, False,
                                           verdict.reason,
                                           {"verdict": verdict.to_dict()})
                    actions.append(f"{symbol}: {verdict.reason}")
                    self.activity.add("vote.refused", f"{symbol} — {verdict.summary()}",
                                      level="warn", ts=now)
                    continue
                self.activity.add("vote.approved", f"{symbol} — {verdict.summary()}",
                                  level="good", ts=now)

            signal, refusal = self.risk.size(setup, search.chosen, signal_id,
                                             now, probability)
            if signal is None:
                self._candidate_refused(symbol, refusal)
                store.save_signal_seen(signal_id, now, symbol,
                                       setup.direction.value, False, refusal)
                actions.append(f"{symbol}: {refusal}")
                self.activity.add("risk.refused",
                                  f"{symbol} {side} SKIPPED — hard risk failure: {refusal}",
                                  level="bad", ts=now)
                self._record_skip(symbol, side, setup, "sizing", refusal)
                continue

            trade = self.ledger.open(signal, now)
            trade.tier = search.tier
            # The audit log: the fill and the whole case for it, as known now.
            from panaoptions import audit
            audit.record_buy(self.cfg, trade, signal, setup,
                             self.candidate if self.candidate
                             and self.candidate.get("symbol") == symbol else None)
            if self.candidate and self.candidate.get("symbol") == symbol:
                self.candidate["taken"] = True
                self.candidate["trade_id"] = trade.id
                self.candidate["contract"] = signal.contract.label
                self.candidate["entry"] = signal.entry_price
                self.candidate["quantity"] = signal.quantity
            store.save_signal_seen(signal_id, now, symbol,
                                   setup.direction.value, True, "taken",
                                   {"trade_id": trade.id,
                                    "strategy": setup.strategy.value,
                                    "side": signal.side_tag,
                                    "execution": signal.execution})
            await self.notifier.entry(signal)
            actions.append(f"{symbol}: ENTERED {signal.alert_line()}")
            how = ("converted debit spread" if signal.contract.is_spread
                   else "outright long option")
            self.activity.add(
                "trade.open",
                f"EXECUTED {signal.side_tag} as {how} ({signal.execution}): "
                f"{signal.alert_line()} — {setup.strategy.value}, x{signal.quantity}",
                level="good", ts=now)
            # Keep going while there are slots left. Stopping after the first
            # entry would make max_open_trades a limit the desk could only
            # reach one cycle at a time, so a second setup on another symbol
            # in the same minute would simply be missed.

        self.scanning = ""
        return actions

    async def _maybe_ingest_fno(self, now: datetime) -> None:
        """Map PDH/PDL/PDC, open interest and the build-up for every watched
        symbol — once a day, after the screen has run."""
        if not bool(self.cfg.get("fno.ingest", True)):
            return
        today = now.date().isoformat()
        if getattr(self, "_fno_on", "") == today or self._screened_on != today:
            return
        self._fno_on = today
        from panaoptions import fno
        max_dte = int(self.cfg.get("contracts.max_dte", 14))
        lines = []
        for symbol in list(self.cfg.symbols):
            try:
                levels = await self._levels_for(symbol, now)
                spot = levels.previous_close or 0.0
                chain = await self.feed.chain_for_window(symbol, spot, 0, max_dte)
                fno.record_oi(symbol, now, chain, max_dte)
                pic = fno.picture(symbol, now, levels)
                fno.save_picture(pic)
                lines.append(pic.line())
            except Exception as exc:                   # noqa: BLE001
                log.warning("F&O ingest failed for %s: %s", symbol, exc)
        if lines:
            self.activity.add("fno.ingest", "previous day F&O — " + " | ".join(lines)[:900],
                              level="info", ts=now)

    def _record_skip(self, symbol: str, side: str, setup, gate: str, reason: str) -> None:
        """A fired setup a hard risk gate refused: into the audit log, so the
        weekly review shows what was skipped and why beside what was bought."""
        from panaoptions import audit
        audit.record_skip(self.cfg, symbol, side, reason, strategy=setup.strategy.value,
                          pattern=setup.pattern, gate=gate,
                          detail={"spot": setup.indicators.close})

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

    def _symbol_busy(self, symbol: str, now: datetime) -> str:
        """Why this symbol must not be traded again right now, or "".

        A setup stays valid for several cycles, and nothing stopped the desk
        buying it again each minute: AMZN 250C was bought x5 and then x3 on
        the same idea, doubling the risk on one name. One position per
        symbol; and after a close, a cool-down so a trade just stopped out is
        not bought straight back on the setup that just failed.
        """
        if bool(self.cfg.get("risk.one_position_per_symbol", True)):
            for trade in self.ledger.open_trades.values():
                if trade.symbol == symbol:
                    return (f"already holding {trade.contract_label} — one "
                            f"position per symbol")
        cooldown = float(self.cfg.get("risk.reentry_cooldown_minutes", 15) or 0)
        if cooldown > 0:
            for trade in reversed(self.ledger.closed):
                if trade.symbol != symbol or trade.closed_at is None:
                    continue
                closed_at = trade.closed_at
                if closed_at.tzinfo is None and now.tzinfo is not None:
                    closed_at = closed_at.replace(tzinfo=now.tzinfo)
                waited = (now - closed_at).total_seconds() / 60.0
                if 0 <= waited < cooldown:
                    return (f"closed {waited:.0f} min ago — waiting "
                            f"{cooldown:.0f} min before trading it again")
                break
        return ""

    def _note_flow(self, symbol: str, setup, chain) -> None:
        """Say whether the options flow agrees with the setup. Never a veto."""
        from panaoptions.engine import flow as flow_mod

        seen = flow_mod.scan(chain, self.cfg)
        if not seen.found:
            return
        want = 1 if setup.direction is Direction.LONG else -1
        verdict = ("agrees" if seen.bias == want else
                   "OPPOSES" if seen.bias == -want else "is mixed")
        line = f"Options flow {verdict}: {seen.headline()}"
        if self.candidate and self.candidate.get("symbol") == symbol:
            self.candidate.setdefault("reasoning", []).append(line)
        self.activity.add("flow", f"{symbol} — {line}",
                          level="good" if verdict == "agrees" else "warn")

    def _candidate_refused(self, symbol: str, reason: str) -> None:
        """Put the refusal on the candidate card itself.

        "Not filled — see the activity log" sent people hunting for the one
        line that mattered; the card now says it.
        """
        if self.candidate and self.candidate.get("symbol") == symbol:
            self.candidate["refused"] = reason

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

    def _screen_summary(self, passed: list[str]) -> str:
        """One line for the log: who passed, or why nobody did.

        "0/20 passed" alone reads as a desk that is stuck. With nothing
        passed no strategy is asked about any symbol, so the line has to say
        that, and how near the nearest names came.
        """
        head = f"{len(passed)}/{len(self.screened)} passed"
        if passed:
            return f"{head} — hunting {', '.join(passed)}"
        min_gap = float(self.cfg.get("premarket.min_gap_pct", 1.0)) or 1.0
        min_rvol = float(self.cfg.get("premarket.min_rvol", 1.5)) or 1.5

        def nearness(r) -> float:
            # The weaker of the two, as a share of what it needs: both must pass.
            return min(abs(r.gap_pct or 0.0) / min_gap, (r.rvol or 0.0) / min_rvol)

        closest = sorted((r for r in self.screened if r.previous_close),
                         key=nearness, reverse=True)[:3]
        # Two decimals: "RVOL 1.5x" against a 1.5x bar read as a pass that
        # was refused, when it was 1.46.
        near = ", ".join(f"{r.symbol} gap {r.gap_pct:+.1f}% RVOL {r.rvol:.2f}x"
                         for r in closest)
        return (f"{head} — no symbol to hunt. The screen needs a gap of "
                f"|{min_gap:g}|% AND RVOL {min_rvol:g}x; closest: {near or 'none'}. "
                f"Re-checking every {self.cfg.get('premarket.rescreen_minutes', 5)} min")

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
        computed = levels_mod.compute(
            bars, self.cfg.timezone, now.date(),
            str(self.cfg.get("session.market_open", "09:30")),
            str(self.cfg.get("session.market_close", "16:00")))
        self._levels[symbol] = computed
        return computed

    async def _pick_contract(self, symbol: str, setup, now: datetime):
        spot = setup.indicators.close
        min_dte = setup.min_dte_override or int(self.cfg.get("contracts.min_dte", 7))
        max_dte = setup.max_dte_override or int(self.cfg.get("contracts.max_dte", 14))
        # Fetched down to the global minimum as well, so that when the setup's
        # own contract is over budget the same delta with less time can be
        # considered. choose() still prefers the setup's window.
        shortest = min(min_dte, int(self.cfg.get("contracts.min_dte", 7)))
        chain = await self.feed.chain_for_window(symbol, spot, shortest, max_dte)
        self._note_flow(symbol, setup, chain)
        try:
            from panaoptions import fno
            fno.record_oi(symbol, now, chain, int(self.cfg.get("contracts.max_dte", 14)))
        except Exception:                              # noqa: BLE001
            pass
        rolling = bool(self.cfg.get("contracts.rolling_spread.enabled", True))
        if rolling:
            self.spreads.annotate(chain, now)
        budget = self.gatekeeper.budget_for(symbol)
        search = contract_filter.choose(symbol, chain, setup.direction,
                                        self.cfg, setup=setup, budget=budget)
        # A contract refused only on its spread may have hit a momentary
        # spike (the open, a news print). Sample the chain again inside the
        # minute and judge the volume-weighted average, not the one quote.
        spread_refused = any("spread" in k for k in search.rejected)
        if rolling and spread_refused and search.tier != "primary":
            resamples = int(self.cfg.get("contracts.rolling_spread.resamples", 2))
            delay = float(self.cfg.get("contracts.rolling_spread.resample_delay_seconds", 5))
            for i in range(resamples):
                if delay > 0:
                    await asyncio.sleep(delay)
                again = await self.feed.chain_for_window(symbol, spot, shortest, max_dte)
                if not again:
                    break
                self.spreads.annotate(again, now + timedelta(seconds=delay * (i + 1)))
                chain = again
            before = search
            search = contract_filter.choose(symbol, chain, setup.direction,
                                            self.cfg, setup=setup, budget=budget)
            if (search.chosen is not None
                    and (before.chosen is None
                         or (search.tier == "primary" and before.tier != "primary"))):
                self.activity.add(
                    "spread.rolling",
                    f"{symbol} — a spread spike was averaged out: "
                    f"{search.chosen.label} passes on the 1-minute volume-weighted "
                    f"spread ({search.chosen.effective_spread_pct:.1f}%) after "
                    f"{resamples + 1} samples", level="good", ts=now)

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
        return search, chain

    async def _convene(self, symbol: str, signal_a, setup, candles, chain,
                       search, now: datetime):
        """The committee's verdict on this setup, once per setup per bar."""
        key = (symbol, signal_a.source, signal_a.direction, setup.ts,
               search.chosen.label if search.chosen else "")
        cached = self._verdicts.get(key)
        if cached is not None:
            return self.cmio.gate(cached, signal_a, setup, search, self._unrealised())
        screen_rvol = next((r.rvol for r in self.screened if r.symbol == symbol), 0.0)
        verdict = await self.cmio.convene(
            signal=signal_a, setup=setup, candles=candles, chain=chain,
            search=search, feed=self.feed, now=now, screen_rvol=screen_rvol,
            unrealised=self._unrealised())
        if len(self._verdicts) > 500:
            self._verdicts.clear()
        self._verdicts[key] = verdict
        return verdict

    def _unrealised(self) -> float:
        """Open profit or loss across the book, at the last marks."""
        return round(sum((t.last_price - t.entry_price) * t.remaining
                         * (t.multiplier or self.cfg.multiplier)
                         for t in self.ledger.open_trades.values()
                         if t.last_price), 2)

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
        actions.extend(await self._circuit_breaker(now))
        return actions

    async def _circuit_breaker(self, now: datetime) -> list[str]:
        """Flatten everything once today's drawdown reaches the daily limit.

        Realised AND open losses count: -$250 closed plus -$150 still open is
        a $400 day on a $4,000 account whichever way it is booked. Once hit,
        the RiskManager is halted for the session and every open position is
        sold at its last mark.
        """
        if not self.ledger.open_trades:
            return []
        unrealised = self._unrealised()
        limit = self.risk.daily_limit
        if limit <= 0 or self.gatekeeper.drawdown(unrealised) < limit:
            return []
        self.gatekeeper.breaker_tripped(unrealised)
        prices = {t.contract_label: t.last_price or t.entry_price
                  for t in self.ledger.open_trades.values()}
        closed = list(self.ledger.open_trades.values())
        self.ledger.close_all(prices, ExitReason.CIRCUIT_BREAKER, now)
        self.activity.add("halt", f"circuit breaker — {self.risk.state.halt_reason}; "
                          f"flattened {len(closed)} position(s)", level="bad", ts=now)
        for trade in closed:
            store.save_trade(trade)
            await self._grade(trade)
            await self.notifier.exit(trade)
        return [f"circuit breaker: flattened {len(closed)} position(s)"]

    async def _contract_price(self, trade) -> float | None:
        """Re-price the exact contract being held — for a debit spread, the
        long leg's mid less the short leg's."""
        chain = await self.feed.chain_for_window(
            trade.symbol, 0.0, 0, 60)
        if getattr(trade, "short_label", ""):
            mids = {c.label: c.mid for c in chain}
            long_mid, short_mid = mids.get(trade.long_label), mids.get(trade.short_label)
            if long_mid is not None and short_mid is not None:
                return round(max(long_mid - short_mid, 0.0), 4)
            log.debug("could not re-price both legs of %s", trade.contract_label)
            return None
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
        # Every close passes through here, so the audit's SELL is written here.
        from panaoptions import audit
        audit.record_sell(self.cfg, trade)
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
            # Keyed by market too: on Auto, India's session and the US one
            # share a date, and each needs its own review.
            marker = f"{self.cfg.market}:{day.isoformat()}"
            if self._daily_written_for == marker:
                return
            if not weekly.session_over(self.cfg, day):
                return

            review = await weekly.build_daily(self.cfg, day)
            self._daily_written_for = marker
            if review.trades or review.audit_days:
                weekly.save(review, self.cfg)
            else:
                log.info("no trades to review for %s", day)
            # And the week so far: journal/weekly/ holds Monday-to-today after
            # every session, not only after Friday. No coach until Friday.
            start, end = weekly.current_week(self.cfg)
            week = await weekly.build(self.cfg, start, end, with_coach=False)
            if week.trades or week.audit_days:
                weekly.save(week, self.cfg)
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not write the daily review: %s", exc)
            from panaoptions.journal import weekly
            self._daily_written_for = f"{self.cfg.market}:{weekly.today(self.cfg).isoformat()}"

    async def _maybe_write_weekly_review(self) -> None:
        """Have the week's review waiting once Friday's session has closed."""
        if not bool(self.cfg.get("journal.auto_weekly_review", True)):
            return
        try:
            from panaoptions.journal import weekly

            start, end = weekly.current_week(self.cfg)
            marker = f"{self.cfg.market}:{end.isoformat()}"
            if self._weekly_written_for == marker:
                return
            if not weekly.is_complete(end, self.cfg):
                return

            review = await weekly.build(self.cfg, start, end)
            self._weekly_written_for = marker
            if review.trades:
                weekly.save(review, self.cfg)
            else:
                log.info("no graded trades in the week to %s", end)
            await self._reflect(start, end)
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not write the weekly review: %s", exc)
            from panaoptions.journal import weekly
            self._weekly_written_for = (f"{self.cfg.market}:"
                                        f"{weekly.current_week(self.cfg)[1].isoformat()}")

    async def _catch_up_reflection(self) -> None:
        """Reflect on the last finished week if nobody has yet."""
        try:
            from datetime import timedelta

            from panaoptions.journal import store as journal_store
            from panaoptions.journal import weekly

            start, end = weekly.current_week(self.cfg)
            if not weekly.is_complete(end, self.cfg):
                start, end = start - timedelta(days=7), end - timedelta(days=7)
            record = (journal_store.JOURNAL_DIR / "reflections"
                      / f"{start.isoformat()}_to_{end.isoformat()}.json")
            if not record.exists():
                await self._reflect(start, end)
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("could not catch up the weekly reflection: %s", exc)

    async def _reflect(self, start, end) -> None:
        """Friday's self-reflection: Ollama tunes the strategy weights."""
        if not bool(self.cfg.get("reflection.enabled", True)):
            return
        try:
            from panaoptions.learning.reflect import reflect

            done = await reflect(self.cfg, start, end)
            self.activity.add(
                "reflection",
                f"week to {end}: {done.note}"
                + (f" — {', '.join(f'{k} {v:+.2f}' for k, v in done.changes.items())}"
                   if done.changes else ""),
                level="good" if done.applied else "info")
        except Exception as exc:                 # noqa: BLE001 - never fatal
            log.warning("weekly reflection failed: %s", exc)

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
            "market": {"active": self.cfg.market, "mode": self.market_mode,
                       "name": self.cfg.market_name, "currency": self.cfg.currency,
                       "timezone": self.cfg.timezone,
                       "open_now": markets.which_now()},
            "notifications": self.notifier.enabled,
            "paper_only": True,
        }
