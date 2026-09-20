"""Practice day — replay a real trading session, bar by bar, at your own speed.

This is the deliberate-practice loop. It takes a REAL historical day, walks it
forward one bar at a time using only the data available at that moment, runs
the full agent desk at each step, and streams everything to the dashboard as if
it were happening live.

Why replay rather than wait for tomorrow:
  * A day takes minutes, not hours, so you get a fortnight of reps in an
    afternoon.
  * You can practise at the weekend, which is when you actually have time.
  * The outcome is known to the engine but not to you, so grading is honest.

No lookahead anywhere: at bar N the agents see bars 0..N and nothing else.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.bus import Topic, bus
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import Candle, MarketContext, Quote, SignalStatus, TradeSignal

log = get_logger("practice")


class PracticeState:
    IDLE = "idle"
    LOADING = "loading"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass
class OpenPractice:
    """A position taken during the practice day."""
    signal: TradeSignal
    opened_at_bar: int
    opened_ts: datetime


@dataclass
class PracticeResult:
    session_id: str
    trading_day: str
    symbols: list[str]
    bars_total: int = 0
    bars_done: int = 0
    signals: int = 0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    pnl: float = 0.0
    rejected: int = 0
    # Why nothing fired. A day with no trades is a normal outcome, but only
    # useful if you can see what the desk was waiting for.
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    closed: list[dict[str, Any]] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "trading_day": self.trading_day,
            "symbols": self.symbols,
            "bars_total": self.bars_total,
            "bars_done": self.bars_done,
            "progress_pct": round(self.bars_done / self.bars_total * 100, 1)
            if self.bars_total else 0.0,
            "signals": self.signals,
            "trades_closed": self.trades_closed,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.wins / self.trades_closed * 100, 1)
            if self.trades_closed else 0.0,
            "total_r": round(self.total_r, 2),
            "pnl": round(self.pnl, 2),
            "rejected": self.rejected,
            "top_rejections": sorted(
                ({"reason": k, "count": v} for k, v in self.rejection_reasons.items()),
                key=lambda x: x["count"], reverse=True)[:6],
            "closed": self.closed,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class PracticeSession:
    """Runs one practice day."""

    def __init__(self, engine: Any, cfg: Config | None = None) -> None:
        self.engine = engine
        self.cfg = cfg or get_config()
        self.state: str = PracticeState.IDLE
        self.result: PracticeResult | None = None
        self.error: str = ""
        self.speed: int = 60
        self._task: asyncio.Task | None = None
        self._bars: dict[str, list[Candle]] = {}
        self._open: dict[str, OpenPractice] = {}
        self._current_ts: datetime | None = None

    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self.state in {PracticeState.RUNNING, PracticeState.PAUSED,
                              PracticeState.LOADING}

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "speed": self.speed,
            "error": self.error,
            "clock": self._current_ts.isoformat() if self._current_ts else None,
            "open_positions": len(self._open),
            "result": self.result.to_dict() if self.result else None,
        }

    # ------------------------------------------------------------------ #
    async def start(self, trading_day: str | None = None,
                    symbols: list[str] | None = None,
                    timeframe: str = "5m", speed: int = 60) -> dict[str, Any]:
        if self.running:
            return {"started": False, "reason": "A practice session is already running."}

        if self.engine.risk.state.open_positions > 0:
            return {"started": False,
                    "reason": ("Close your live positions first. Practice uses the "
                               "same risk desk, so mixing the two would corrupt "
                               "both P&L figures.")}

        self.speed = max(1, min(int(speed), 600))
        self.error = ""
        self._open.clear()
        self.state = PracticeState.LOADING

        targets = symbols or [w["symbol"] for w in self.cfg.watchlist()][:4]
        session_id = f"PRAC-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4].upper()}"
        self.result = PracticeResult(session_id=session_id,
                                     trading_day=trading_day or "latest",
                                     symbols=targets,
                                     started_at=datetime.now())

        await bus.publish("practice.state", self.status())

        try:
            await self._load(targets, timeframe, trading_day)
        except Exception as exc:
            self.state = PracticeState.FAILED
            self.error = str(exc)
            await bus.publish("practice.state", self.status())
            return {"started": False, "reason": str(exc)}

        if not self.result.bars_total:
            self.state = PracticeState.FAILED
            self.error = ("No historical bars available for that day. Markets are "
                          "shut at weekends and on holidays — pick a weekday, or "
                          "leave the date blank for the most recent session.")
            await bus.publish("practice.state", self.status())
            return {"started": False, "reason": self.error}

        self.state = PracticeState.RUNNING
        self._task = asyncio.create_task(self._run(timeframe))
        log.info("practice day started: %s, %d symbols, %d bars, %dx speed",
                 self.result.trading_day, len(targets),
                 self.result.bars_total, self.speed)
        return {"started": True, "session": self.result.to_dict()}

    # ------------------------------------------------------------------ #
    async def _load(self, symbols: list[str], timeframe: str,
                    trading_day: str | None) -> None:
        """Fetch the day's real bars for every symbol."""
        self._bars = {}
        wanted: date | None = None
        if trading_day:
            try:
                wanted = datetime.fromisoformat(trading_day).date()
            except ValueError as exc:
                raise ValueError(f"'{trading_day}' is not a YYYY-MM-DD date") from exc

        for symbol in symbols:
            candles = await self.engine.broker.get_candles(symbol, timeframe, 500)
            if not candles:
                continue
            if wanted:
                day_bars = [c for c in candles if c.ts.date() == wanted]
            else:
                # The most recent complete session present in the data.
                last_day = candles[-1].ts.date()
                day_bars = [c for c in candles if c.ts.date() == last_day]
                self.result.trading_day = last_day.isoformat()
            if len(day_bars) >= 10:
                self._bars[symbol] = day_bars

        self.result.symbols = list(self._bars)
        self.result.bars_total = max((len(v) for v in self._bars.values()), default=0)

    # ------------------------------------------------------------------ #
    async def _run(self, timeframe: str) -> None:
        """Walk the day forward, running the desk at each bar."""
        assert self.result is not None
        warmup = 20            # the indicators need history before they mean anything
        delay = 60.0 / max(self.speed, 1)   # one 5m bar per `delay` seconds

        try:
            for i in range(warmup, self.result.bars_total):
                while self.state == PracticeState.PAUSED:
                    await asyncio.sleep(0.3)
                if self.state != PracticeState.RUNNING:
                    break

                self.result.bars_done = i
                await self._step(i, timeframe)
                await bus.publish("practice.progress", self.result.to_dict())
                await asyncio.sleep(delay)

            # Square off whatever is still open, as the real desk would.
            await self._square_off()
            self.result.bars_done = self.result.bars_total
            self.result.finished_at = datetime.now()
            self.state = PracticeState.FINISHED
            log.info("practice day finished: %d signals, %d closed, %+.2fR",
                     self.result.signals, self.result.trades_closed,
                     self.result.total_r)
        except asyncio.CancelledError:
            self.state = PracticeState.IDLE
            raise
        except Exception as exc:
            log.exception("practice session failed: %s", exc)
            self.state = PracticeState.FAILED
            self.error = str(exc)
        finally:
            await bus.publish("practice.state", self.status())

    # ------------------------------------------------------------------ #
    async def _step(self, bar_index: int, timeframe: str) -> None:
        """One bar: mark open positions, then look for a new setup."""
        assert self.result is not None

        for symbol, bars in self._bars.items():
            if bar_index >= len(bars):
                continue
            bar = bars[bar_index]
            self._current_ts = bar.ts

            await self._mark(symbol, bar, bar_index)

            if symbol in self._open:
                continue                      # one position per symbol at a time
            if len(self._open) >= int(self.cfg.get("risk.max_open_positions", 3)):
                continue

            # Only the bars up to now. This is what keeps the replay honest.
            window = bars[: bar_index + 1]
            ctx = await self._context(symbol, window, bar, timeframe)
            result = await self.engine.desk.run_cycle(
                ctx, cycle_id=f"{self.result.session_id}-{bar_index}-{symbol}")

            if result.signal and result.signal.status == SignalStatus.APPROVED:
                self.result.signals += 1
                self._open[symbol] = OpenPractice(signal=result.signal,
                                                  opened_at_bar=bar_index,
                                                  opened_ts=bar.ts)
                log.info("practice ENTRY %s", result.signal.alert_line())
                await bus.publish(Topic.SIGNAL_APPROVED, result.signal)
            elif result.rejected:
                self.result.rejected += 1
                self._note_rejection(result.rejected[0])

    def _note_rejection(self, reason: str) -> None:
        """Bucket rejections by cause, not by their exact wording — the numbers
        inside each message differ every bar and would fragment the tally."""
        assert self.result is not None
        low = reason.lower()
        if "confirmation" in low:
            key = "Not enough confirmations"
        elif "neutral band" in low or "conviction" in low:
            key = "Conviction below threshold"
        elif "lot" in low or "share" in low or "sizes to" in low:
            key = "Position sizes to zero (capital too small)"
        elif "r:r" in low or "risk_reward" in low or "reward" in low:
            key = "Risk:reward below minimum"
        elif "stop" in low:
            key = "Stop loss too tight or too wide"
        elif "cutoff" in low or "halt" in low or "open positions" in low:
            key = "Desk closed (cutoff, halt, or full)"
        elif "exposure" in low:
            key = "Exposure limit reached"
        else:
            key = reason[:60]
        self.result.rejection_reasons[key] = \
            self.result.rejection_reasons.get(key, 0) + 1

    async def _context(self, symbol: str, window: list[Candle],
                       bar: Candle, timeframe: str) -> MarketContext:
        """Build the desk's view using only the bars seen so far."""
        from app.indicators import patterns as pattern_mod
        from app.indicators import ta

        tech = self.cfg.get("technical", {}) or {}
        df = ta.candles_to_df(window)
        snapshot = ta.compute_all(df, tech)
        snapshot["patterns"] = pattern_mod.scan(df, tech.get("patterns_enabled"))

        ctx = MarketContext(symbol=symbol,
                            cycle_id=f"practice-{len(window)}",
                            quote=Quote(symbol=symbol, last_price=bar.close))
        ctx.indicators = {
            "primary": snapshot,
            "by_timeframe": {timeframe: snapshot},
            "mtf_alignment": {"aligned": False, "direction": 0},
            "primary_timeframe": timeframe,
        }
        regime = snapshot.get("regime")
        from app.core.models import Regime
        if regime in {r.value for r in Regime}:
            ctx.regime = Regime(regime)
        return ctx

    # ------------------------------------------------------------------ #
    async def _mark(self, symbol: str, bar: Candle, bar_index: int) -> None:
        """Did this bar hit the stop or the target?"""
        pos = self._open.get(symbol)
        if not pos:
            return
        sig = pos.signal
        long = sig.side.value == "BUY"

        hit_stop = bar.low <= sig.stop_loss if long else bar.high >= sig.stop_loss
        hit_target = bar.high >= sig.target if long else bar.low <= sig.target
        if hit_stop and hit_target:
            # Both touched in one bar. Without tick data the order is unknowable,
            # so assume the stop — optimism here would flatter every session.
            hit_target = False

        if not (hit_stop or hit_target):
            return

        exit_price = sig.stop_loss if hit_stop else sig.target
        await self._close(symbol, exit_price,
                          "STOP" if hit_stop else "TARGET", bar.ts, bar_index)

    async def _square_off(self) -> None:
        """Close anything still open at the last price, as the desk would."""
        for symbol in list(self._open):
            bars = self._bars.get(symbol) or []
            if not bars:
                continue
            last = bars[-1]
            await self._close(symbol, last.close, "SQUARE_OFF",
                              last.ts, len(bars) - 1)

    async def _close(self, symbol: str, exit_price: float, reason: str,
                     ts: datetime, bar_index: int) -> None:
        assert self.result is not None
        pos = self._open.pop(symbol, None)
        if not pos:
            return

        sig = pos.signal
        direction = 1 if sig.side.value == "BUY" else -1
        risk_per_unit = abs(sig.entry - sig.stop_loss) or 1.0
        r = (exit_price - sig.entry) * direction / risk_per_unit
        pnl = (exit_price - sig.entry) * sig.quantity * direction

        self.result.trades_closed += 1
        self.result.total_r += r
        self.result.pnl += pnl
        if r > 0:
            self.result.wins += 1
        else:
            self.result.losses += 1

        record = {
            "symbol": symbol,
            "instrument": sig.instrument.tradingsymbol,
            "side": sig.side.value,
            "entry": sig.entry,
            "stop": sig.stop_loss,
            "target": sig.target,
            "exit": round(exit_price, 2),
            "quantity": sig.quantity,
            "outcome": reason,
            "r_multiple": round(r, 2),
            "pnl": round(pnl, 2),
            "bars_held": bar_index - pos.opened_at_bar,
            "entry_ts": pos.opened_ts.isoformat(),
            "exit_ts": ts.isoformat(),
            "confirmations": sig.confirmations,
            "signal_id": sig.id,
        }
        self.result.closed.append(record)
        log.info("practice EXIT %s @ %.2f → %s (%+.2fR)",
                 symbol, exit_price, reason, r)
        await bus.publish("practice.trade", record)

    # ------------------------------------------------------------------ #
    async def pause(self) -> dict[str, Any]:
        if self.state == PracticeState.RUNNING:
            self.state = PracticeState.PAUSED
        elif self.state == PracticeState.PAUSED:
            self.state = PracticeState.RUNNING
        await bus.publish("practice.state", self.status())
        return self.status()

    async def stop(self) -> dict[str, Any]:
        if self._task:
            self.state = PracticeState.IDLE
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.state = PracticeState.IDLE
        self._open.clear()
        await bus.publish("practice.state", self.status())
        return self.status()

    def set_speed(self, speed: int) -> int:
        self.speed = max(1, min(int(speed), 600))
        return self.speed

    # ------------------------------------------------------------------ #
    async def log_to_journal(self) -> dict[str, Any]:
        """Write every closed practice trade into the journal so the session
        is graded by the same coach as a real one."""
        if not self.result or not self.result.closed:
            return {"logged": 0}

        from app.journal import store
        from app.journal.models import JournalEntry, SetupType
        from app.journal.postmortem import PostMortemEngine

        store.init_journal()
        engine = PostMortemEngine(self.cfg)
        logged = 0

        for t in self.result.closed:
            entry = JournalEntry(
                id=f"{self.result.session_id}-{t['symbol']}-{logged}",
                market=self.cfg.active_market,
                symbol=t["symbol"],
                instrument=t["instrument"],
                setup=SetupType.OTHER,
                side=t["side"],
                planned_entry=t["entry"],
                planned_stop=t["stop"],
                planned_target=t["target"],
                planned_quantity=t["quantity"],
                actual_entry=t["entry"],
                actual_exit=t["exit"],
                actual_quantity=t["quantity"],
                entry_ts=datetime.fromisoformat(t["entry_ts"]),
                exit_ts=datetime.fromisoformat(t["exit_ts"]),
                notes=(f"Practice session {self.result.session_id} replaying "
                       f"{self.result.trading_day}. Exit: {t['outcome']}."),
                context={"capital": self.engine.risk.state.capital,
                         "practice": True,
                         "time_stop_hit": t["outcome"] == "SQUARE_OFF"},
            )
            card = await engine.build(entry)
            entry.execution_score = card.execution_score
            store.save_entry(entry)
            store.save_card(card)
            logged += 1

        store.export_summary()
        log.info("practice session logged %d trades to the journal", logged)
        return {"logged": logged, "session_id": self.result.session_id}
