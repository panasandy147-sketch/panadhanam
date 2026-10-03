"""SJK 9/21 · VWAP · ADX — the user's confluence model (sjk912_vwapadx, 5 Oct 2026).

    BUY    the 9 EMA crosses ABOVE the 21 EMA (the Crossover Candle), its close
           above the session VWAP; the very next candle (the Confirmation
           Candle) closes above the 9 EMA and the VWAP with ADX(14) > 20.
           Enter on that close. Stop: the Crossover Candle's LOW. Target:
           1:rr (2). Optional dynamic exit: the opposing (bearish) crossover.
    SELL   the mirror: 9 crosses below 21, closes below VWAP, confirmed by the
           next candle closing below the 9 EMA and VWAP with ADX > 20. Stop:
           the Crossover Candle's HIGH.
    NONE   Sideways / Choppy — ADX <= 20, the 9 and 21 crossing back and forth
           (more than max_crosses in chop_bars bars), or the EMAs, VWAP and
           price bunched inside a band narrower than band_pct of the price.
           No entry, even on a nominal crossover.

The parts:

  Indicators      ema_step(), SessionVwap (typical price x volume, reset each
                  market day), Adx (Wilder's, period 14, smoothing 14)
  SignalEvent     the output contract: id, time, action, trigger, entry, stop,
                  take-profit, metadata (ADX, VWAP, EMAs)
  SJK912_VWAP_ADX_Engine
                  on_bar_update(bar) -> SignalEvent: keeps the indicators,
                  remembers the Crossover Candle, judges the next candle as
                  its Confirmation (no lookahead), and holds ONE virtual
                  position at a time, emitting EXIT at the stop, the target,
                  the opposing crossover (exit_mode) or the session's end
  detect_candles  the live desk's question: is there a BUY / SELL on the
                  latest closed candle, inside the entry window?
  backtest        the engine over historical candles, one trade at a time

Indicators run continuously across days (the EMAs and ADX need 21 / 28 bars
to settle — a daily reset would blind the first two hours); the VWAP, the
pending Crossover Candle and any open position reset each market day
(reset_emas_daily: true resets the EMAs and ADX too).

panaoptions carries the same engine in panaoptions/strategies/sjk912_vwapadx.py;
the two apps share no code.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

KEY = "sjk912_vwapadx"                      # the settings section
SETUP_NAME = "SJK 9/21 · VWAP · ADX"
BUY, SELL, EXIT, NONE = "BUY", "SELL", "EXIT", "NONE"
BULLISH, BEARISH = "Bullish_Confluence", "Bearish_Confluence"
SIDEWAYS = "Sideways / Choppy"


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def ema_step(prev: float | None, value: float, n: int) -> float:
    """One step of an EMA seeded with the first value."""
    if prev is None:
        return float(value)
    k = 2.0 / (n + 1)
    return float(value) * k + prev * (1 - k)


class SessionVwap:
    """Session VWAP on the typical price (H + L + C) / 3, weighted by volume,
    reset at each new market day. A feed with no volume (an index) weights
    every bar equally rather than dividing by zero."""

    def __init__(self) -> None:
        self.day: Any = None
        self.pv = self.v = 0.0
        self.tp_sum = 0.0
        self.n = 0

    def update(self, day: Any, high: float, low: float, close: float, volume: float) -> float:
        if day != self.day:
            self.day, self.pv, self.v, self.tp_sum, self.n = day, 0.0, 0.0, 0.0, 0
        tp = (high + low + close) / 3.0
        vol = max(float(volume or 0.0), 0.0)
        self.pv += tp * vol
        self.v += vol
        self.tp_sum += tp
        self.n += 1
        return self.pv / self.v if self.v > 0 else self.tp_sum / self.n


class Adx:
    """Wilder's ADX: TR, +DM and -DM smoothed over `period`, then DX averaged
    over `smoothing`. None until it has settled (period + smoothing bars)."""

    def __init__(self, period: int = 14, smoothing: int = 14) -> None:
        self.n, self.s = period, smoothing
        self.prev: tuple[float, float, float] | None = None
        self.tr = self.pdm = self.mdm = 0.0
        self.count = 0
        self.dx: list[float] = []
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self.prev is None:
            self.prev = (high, low, close)
            return None
        ph, pl, pc = self.prev
        self.prev = (high, low, close)
        tr = max(high - low, abs(high - pc), abs(low - pc))
        up, down = high - ph, pl - low
        pdm = up if up > down and up > 0 else 0.0
        mdm = down if down > up and down > 0 else 0.0
        self.count += 1
        if self.count <= self.n:                     # the first sums
            self.tr += tr
            self.pdm += pdm
            self.mdm += mdm
            if self.count < self.n:
                return None
        else:                                        # Wilder's smoothing
            self.tr += tr - self.tr / self.n
            self.pdm += pdm - self.pdm / self.n
            self.mdm += mdm - self.mdm / self.n
        if self.tr <= 0:
            return self.value
        pdi, mdi = 100 * self.pdm / self.tr, 100 * self.mdm / self.tr
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0
        if self.value is None:
            self.dx.append(dx)
            if len(self.dx) == self.s:
                self.value = sum(self.dx) / self.s
        else:
            self.value = (self.value * (self.s - 1) + dx) / self.s
        return self.value


# --------------------------------------------------------------------------- #
# The output contract
# --------------------------------------------------------------------------- #
@dataclass
class SignalEvent:
    """One decision on one closed bar."""
    timestamp: Any
    action: str = NONE                     # BUY / SELL / EXIT / NONE
    trigger_type: str = ""                 # Bullish_Confluence / Bearish_Confluence
    entry_price: float | None = None       # the Confirmation Candle's close
    stop_loss: float | None = None         # the Crossover Candle's low / high
    take_profit: float | None = None       # entry +/- rr x risk
    reason: str = ""                       # why NONE / why EXIT
    metadata: dict[str, Any] = field(default_factory=dict)
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        ts = out["timestamp"]
        out["timestamp"] = ts.isoformat() if hasattr(ts, "isoformat") else ts
        return out


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def _hm(value: str) -> int:
    h, _, m = str(value).partition(":")
    return int(h) * 60 + int(m or 0)


class SJK912_VWAP_ADX_Engine:
    """Feed it closed bars in order with on_bar_update(); it returns one
    SignalEvent per bar. No lookahead: every decision uses that bar and the
    ones before it."""

    def __init__(self, fast: int = 9, slow: int = 21, adx_period: int = 14,
                 adx_smoothing: int = 14, adx_min: float = 20.0, rr: float = 2.0,
                 exit_mode: str = "target", chop_bars: int = 12, max_crosses: int = 2,
                 band_pct: float = 0.10, reset_emas_daily: bool = False,
                 tz: str | None = None, window: tuple[str, str] | None = None,
                 square_off: str | None = None, tick: float = 0.0) -> None:
        self.fast, self.slow, self.adx_min, self.rr = fast, slow, adx_min, rr
        self.exit_mode = exit_mode              # "target" | "cross" | "both"
        self.chop_bars, self.max_crosses, self.band_pct = chop_bars, max_crosses, band_pct
        self.reset_emas_daily, self.tz = reset_emas_daily, tz
        self.window = window
        self.square_off = square_off
        self.tick = tick
        self.adx_args = (adx_period, adx_smoothing)
        self._reset_indicators()
        self.vwap = SessionVwap()
        self.day: Any = None
        self.crosses: list[int] = []            # bar numbers of 9/21 crosses
        self.bar_no = 0
        self.pending: dict[str, Any] | None = None    # the Crossover Candle
        self.position: dict[str, Any] | None = None   # the one open trade
        self.last: dict[str, Any] = {}

    def _reset_indicators(self) -> None:
        self.e_fast: float | None = None
        self.e_slow: float | None = None
        self.adx = Adx(*self.adx_args)
        self.seen = 0

    # ---- helpers ---------------------------------------------------------- #
    def _local(self, ts: Any) -> Any:
        if self.tz and hasattr(ts, "astimezone") and getattr(ts, "tzinfo", None):
            from zoneinfo import ZoneInfo
            return ts.astimezone(ZoneInfo(self.tz))
        return ts

    def _in_window(self, at: Any) -> bool:
        if not self.window or not hasattr(at, "hour"):
            return True
        m = at.hour * 60 + at.minute
        return _hm(self.window[0]) <= m < _hm(self.window[1])

    def _past_square_off(self, at: Any) -> bool:
        return bool(self.square_off and hasattr(at, "hour")
                    and at.hour * 60 + at.minute >= _hm(self.square_off))

    def chop_reason(self, close: float, vwap: float, adx: float | None) -> str:
        """'' when the market trends, else why it is Sideways / Choppy."""
        if adx is None:
            return "ADX not settled yet"
        if adx <= self.adx_min:
            return f"ADX {adx:.1f} <= {self.adx_min:g} (no trend strength)"
        recent = [b for b in self.crosses if b > self.bar_no - self.chop_bars]
        if len(recent) > self.max_crosses:
            return (f"the 9 and 21 EMAs crossed {len(recent)} times in the last "
                    f"{self.chop_bars} bars (intertwined)")
        pts = [self.e_fast, self.e_slow, vwap, close]
        width = (max(pts) - min(pts)) / close * 100 if close else 0.0
        if width < self.band_pct:
            return (f"the EMAs, VWAP and price sit inside {width:.2f}% "
                    f"(under {self.band_pct:g}%) — compressed")
        return ""

    # ---- the bar ---------------------------------------------------------- #
    def on_bar_update(self, bar: Any) -> SignalEvent:
        """Update the indicators with one CLOSED bar and decide."""
        ts = bar.ts
        at = self._local(ts)
        day = at.date() if hasattr(at, "date") else None
        h, lo, c = float(bar.high), float(bar.low), float(bar.close)
        vol = float(getattr(bar, "volume", 0.0) or 0.0)
        ev = SignalEvent(timestamp=ts)

        new_day = day != self.day
        if new_day:
            self.day = day
            self.pending = None
            if self.reset_emas_daily:
                self._reset_indicators()
        exit_ev = None
        if new_day and self.position is not None:
            exit_ev = self._exit(ts, self.position["last_close"], "session ended")

        prev_fast, prev_slow = self.e_fast, self.e_slow
        self.e_fast = ema_step(self.e_fast, c, self.fast)
        self.e_slow = ema_step(self.e_slow, c, self.slow)
        vwap = self.vwap.update(day, h, lo, c, vol)
        adx = self.adx.update(h, lo, c)
        self.bar_no += 1
        self.seen += 1
        crossed = 0
        if prev_fast is not None and prev_slow is not None:
            if prev_fast <= prev_slow and self.e_fast > self.e_slow:
                crossed = 1
            elif prev_fast >= prev_slow and self.e_fast < self.e_slow:
                crossed = -1
        if crossed:
            self.crosses.append(self.bar_no)
        meta = {"adx": None if adx is None else round(adx, 2), "vwap": round(vwap, 4),
                "ema_fast": round(self.e_fast, 4), "ema_slow": round(self.e_slow, 4)}
        ev.metadata = meta
        self.last = {**meta, "close": c}
        if exit_ev is not None:
            return exit_ev

        # ---- manage the open position first ----
        if self.position is not None:
            p = self.position
            long = p["direction"] > 0
            p["last_close"] = c
            if (lo <= p["stop"]) if long else (h >= p["stop"]):
                return self._exit(ts, p["stop"], "stop", meta)
            if self.exit_mode in ("target", "both") and (
                    (h >= p["target"]) if long else (lo <= p["target"])):
                return self._exit(ts, p["target"], "target", meta)
            if self.exit_mode in ("cross", "both") and crossed == -p["direction"]:
                return self._exit(ts, c, "opposing crossover", meta)
            if self._past_square_off(at):
                return self._exit(ts, c, "square-off", meta)
            ev.reason = "in a position"
            return ev

        # ---- a Crossover Candle pending: is THIS its Confirmation Candle? ----
        pend, self.pending = self.pending, None
        if pend is not None:
            d = pend["direction"]
            confirmed = (c > self.e_fast and c > vwap) if d > 0 else (c < self.e_fast and c < vwap)
            why = self.chop_reason(c, vwap, adx)
            if not confirmed:
                ev.reason = (f"the candle after the crossover closed "
                             f"{'below' if d > 0 else 'above'} the 9 EMA or VWAP — not confirmed")
            elif why:
                ev.reason = f"{SIDEWAYS}: {why}"
            elif not self._in_window(at):
                ev.reason = "outside the entry window"
            else:
                stop = pend["low"] - self.tick if d > 0 else pend["high"] + self.tick
                risk = (c - stop) * d
                if risk <= 0:
                    ev.reason = "no risk to define (the close is beyond the crossover candle)"
                else:
                    target = c + d * self.rr * risk
                    ev.action = BUY if d > 0 else SELL
                    ev.trigger_type = BULLISH if d > 0 else BEARISH
                    ev.entry_price, ev.stop_loss = round(c, 4), round(stop, 4)
                    ev.take_profit = round(target, 4)
                    ev.metadata = {**meta, "crossover_ts": str(pend["ts"]), "risk": round(risk, 4)}
                    self.position = {"direction": d, "entry": c, "stop": stop,
                                     "target": target, "last_close": c, "id": ev.signal_id}
                    return ev

        # ---- a new Crossover Candle? (its close on the VWAP's right side) ----
        if crossed and self.seen >= self.slow:
            beyond = c > vwap if crossed > 0 else c < vwap
            if beyond:
                self.pending = {"direction": crossed, "high": h, "low": lo, "ts": ts}
                ev.reason = "crossover candle — waiting for the confirmation candle"
            else:
                ev.reason = (f"crossover with the close {'below' if crossed > 0 else 'above'} "
                             f"VWAP — ignored")
        return ev

    def _exit(self, ts: Any, price: float, why: str,
              meta: dict[str, Any] | None = None) -> SignalEvent:
        p, self.position = self.position, None
        d = p["direction"]
        r = (price - p["entry"]) * d / abs(p["entry"] - p["stop"])
        return SignalEvent(timestamp=ts, action=EXIT, entry_price=round(price, 4),
                           reason=why, metadata={**(meta or {}), "r": round(r, 3),
                                                 "of": p["id"], "entry": p["entry"],
                                                 "direction": "LONG" if d > 0 else "SHORT"})


# --------------------------------------------------------------------------- #
# Settings, the live desk and the backtest
# --------------------------------------------------------------------------- #
def engine_from(cfg: Any, tz: str | None = None) -> SJK912_VWAP_ADX_Engine:
    g = cfg.get
    p = f"{KEY}."
    return SJK912_VWAP_ADX_Engine(
        fast=int(g(p + "fast", 9)), slow=int(g(p + "slow", 21)),
        adx_period=int(g(p + "adx_period", 14)), adx_smoothing=int(g(p + "adx_smoothing", 14)),
        adx_min=float(g(p + "adx_min", 20.0)), rr=float(g(p + "rr", 2.0)),
        exit_mode=str(g(p + "exit_mode", "target")), chop_bars=int(g(p + "chop_bars", 12)),
        max_crosses=int(g(p + "max_crosses", 2)), band_pct=float(g(p + "band_pct", 0.10)),
        reset_emas_daily=bool(g(p + "reset_emas_daily", False)), tz=tz,
        window=(str(g(p + "from", "09:45")), str(g(p + "to", "15:45"))),
        square_off=str(g("system.square_off_time", "15:15")),
        tick=float(g(p + "stop_buffer", 0.0) or 0.0))


def detect_candles(bars: list[Any], cfg: Any, tz: str) -> dict[str, Any] | None:
    """A BUY / SELL on the LATEST closed 5m candle (inside sjk912_vwapadx.from
    - .to on the market's clock), as a dict for the analyst, or None. The
    engine replays the tape so its one-position memory is the backtest's."""
    if len(bars) < 30:
        return None
    eng = engine_from(cfg, tz)
    ev = None
    for b in bars:
        ev = eng.on_bar_update(b)
    if ev is None or ev.action not in (BUY, SELL):
        return None
    long = ev.action == BUY
    m = ev.metadata
    return {"direction": "LONG" if long else "SHORT", "entry": ev.entry_price,
            "stop": ev.stop_loss, "target": ev.take_profit, "signal_id": ev.signal_id,
            "trigger": ev.trigger_type, "adx": m.get("adx"), "vwap": m.get("vwap"),
            "ema_fast": m.get("ema_fast"), "ema_slow": m.get("ema_slow"),
            "ts": bars[-1].ts.isoformat(), "setup": SETUP_NAME,
            "note": (f"9 EMA crossed {'above' if long else 'below'} the 21 EMA "
                     f"(stop at that candle's {'low' if long else 'high'} {ev.stop_loss:,.2f}); "
                     f"confirmed by the next close {ev.entry_price:,.2f} "
                     f"{'above' if long else 'below'} the 9 EMA {m.get('ema_fast'):,.2f} and "
                     f"VWAP {m.get('vwap'):,.2f}; ADX {m.get('adx')}")}


def backtest(bars: list[Any], cfg: Any, tz: str, **_: Any) -> list[dict[str, Any]]:
    """The engine over historical 5m candles: one trade at a time, out at the
    stop (first, when a bar touches both), the target or the opposing
    crossover (exit_mode), or the square-off. One dict per trade, with R."""
    eng = engine_from(cfg, tz)
    trades: list[dict[str, Any]] = []
    open_: dict[str, Any] | None = None
    for b in bars:
        ev = eng.on_bar_update(b)
        if ev.action in (BUY, SELL):
            open_ = {"ts": b.ts.isoformat(), "direction": "LONG" if ev.action == BUY else "SHORT",
                     "entry": ev.entry_price, "stop": ev.stop_loss, "target": ev.take_profit,
                     "risk": abs(ev.entry_price - ev.stop_loss), "kind": ev.trigger_type}
        elif ev.action == EXIT and open_ is not None:
            outcome = {"stop": "STOP", "target": "TARGET"}.get(ev.reason, "SQUARE_OFF"
                                                                if ev.reason != "opposing crossover"
                                                                else "CROSS")
            trades.append({**open_, "exit": ev.entry_price, "exit_ts": b.ts.isoformat(),
                           "outcome": outcome, "r": ev.metadata.get("r", 0.0)})
            open_ = None
    return trades
