"""sjk912RSi — the user's 9/21 EMA + RSI crossover strategy, v2 (5 Oct 2026).

    LONG   the 9 EMA crossed above the 21 EMA within the last `cross_within`
           (3) bars and is still above; the candle closes strictly above both
           EMAs with its LOW at or above the 21 EMA; it closes up (close >
           open); RSI(14) strictly between 50 and 68 (no buying exhaustion);
           at least `warmup_bars` (5) bars into the session. Enter on the
           close. Stop: min(lowest low of the last 5 bars, close - 0.5 x
           ATR(14)) — never a micro-stop. Target: entry + rr (2) x risk,
           locked at entry.
    SHORT  the mirror: crossunder within 3 bars; close below both EMAs with the
           HIGH at or below the 21 EMA; close < open; RSI between 32 and 50;
           stop max(highest high of 5, close + 0.5 x ATR).

One trade per crossover cycle (a new crossover re-arms it), one position at
a time; intraday (out by the square-off). The same rules for TradingView:
pine/sjk912RSi.pine.

The parts: Rsi (Wilder's), SJK912_RSI_Engine.on_bar_update() -> SignalEvent
(the contract of sjk912_vwapadx), detect_candles() for the live desk,
backtest() and report(). panaoptions carries the same engine in
panaoptions/strategies/sjk912rsi.py.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from app.strategies.sjk912_vwapadx import (
    BUY,
    EXIT,
    SELL,
    Atr,
    SignalEvent,
    ema_step,
)
from app.strategies.sjk912_vwapadx import report as _report

KEY = "sjk912rsi"                          # the settings section
SETUP_NAME = "sjk912RSi · 9/21 EMA + RSI"
BULLISH, BEARISH = "Bullish_Crossover", "Bearish_Crossover"


class Rsi:
    """Wilder's RSI, one close at a time (TradingView's ta.rsi). None until
    `n` changes have been seen."""

    def __init__(self, n: int = 14) -> None:
        self.n = n
        self.prev: float | None = None
        self.gain = self.loss = 0.0
        self.count = 0
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self.prev is None:
            self.prev = close
            return None
        ch = close - self.prev
        self.prev = close
        up, down = max(ch, 0.0), max(-ch, 0.0)
        self.count += 1
        if self.count <= self.n:
            self.gain += up / self.n
            self.loss += down / self.n
            if self.count < self.n:
                return None
        else:
            self.gain = (self.gain * (self.n - 1) + up) / self.n
            self.loss = (self.loss * (self.n - 1) + down) / self.n
        if self.loss == 0:
            self.value = 100.0 if self.gain > 0 else 50.0
        else:
            self.value = 100 - 100 / (1 + self.gain / self.loss)
        return self.value


def _hm(value: str) -> int:
    h, _, m = str(value).partition(":")
    return int(h) * 60 + int(m or 0)


class SJK912_RSI_Engine:
    """Feed closed bars in order with on_bar_update(); one SignalEvent each."""

    def __init__(self, fast: int = 9, slow: int = 21, rsi_len: int = 14,
                 rsi_mid: float = 50.0, rsi_long_max: float = 68.0,
                 rsi_short_min: float = 32.0, rr: float = 2.0, swing_lookback: int = 5,
                 cross_within: int = 3, atr_period: int = 14, min_stop_atr: float = 0.5,
                 warmup_bars: int = 5, tz: str | None = None,
                 window: tuple[str, str] | None = None, square_off: str | None = None,
                 **_: Any) -> None:
        self.fast, self.slow, self.rr = fast, slow, rr
        self.rsi_mid, self.rsi_hi, self.rsi_lo = rsi_mid, rsi_long_max, rsi_short_min
        self.lookback, self.within = swing_lookback, cross_within
        self.min_stop_atr, self.warmup = min_stop_atr, warmup_bars
        self.tz, self.window, self.square_off = tz, window, square_off
        self.e_fast: float | None = None
        self.e_slow: float | None = None
        self.rsi = Rsi(rsi_len)
        self.atr = Atr(atr_period)
        self.highs: deque[float] = deque(maxlen=swing_lookback)
        self.lows: deque[float] = deque(maxlen=swing_lookback)
        self.seen = self.bar_no = 0
        self.cross: dict[str, Any] | None = None     # {direction, bar, entered}
        self.position: dict[str, Any] | None = None
        self.day: Any = None
        self.session_bars = 0

    def _local(self, ts: Any) -> Any:
        if self.tz and getattr(ts, "tzinfo", None):
            from zoneinfo import ZoneInfo
            return ts.astimezone(ZoneInfo(self.tz))
        return ts

    def on_bar_update(self, bar: Any) -> SignalEvent:
        ts = bar.ts
        at = self._local(ts)
        o, h, lo, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
        day = at.date() if hasattr(at, "date") else None
        new_day = day != self.day
        self.day = day
        self.session_bars = 1 if new_day else self.session_bars + 1

        pf, ps = self.e_fast, self.e_slow
        self.e_fast = ema_step(self.e_fast, c, self.fast)
        self.e_slow = ema_step(self.e_slow, c, self.slow)
        rsi = self.rsi.update(c)
        atr = self.atr.update(h, lo, c)
        self.bar_no += 1
        self.seen += 1
        self.highs.append(h)
        self.lows.append(lo)
        meta = {"rsi": None if rsi is None else round(rsi, 2),
                "ema9": round(self.e_fast, 4), "ema21": round(self.e_slow, 4),
                "atr14": None if atr is None else round(atr, 4)}
        ev = SignalEvent(timestamp=ts, metadata=meta)

        # ---- the open position: a new day, the stop, the target, the square-off
        if self.position is not None:
            p = self.position
            if new_day:
                return self._exit(ts, p["last_close"], "session ended", meta)
            long = p["direction"] > 0
            p["last_close"] = c
            if (lo <= p["stop"]) if long else (h >= p["stop"]):
                return self._exit(ts, p["stop"], "stop", meta)
            if (h >= p["target"]) if long else (lo <= p["target"]):
                return self._exit(ts, p["target"], "target", meta)
            if self.square_off and hasattr(at, "hour") and \
                    at.hour * 60 + at.minute >= _hm(self.square_off):
                return self._exit(ts, c, "square-off", meta)
            ev.reason = "in a position"
            return ev

        # ---- a new crossover re-arms the one trade per cycle
        if pf is not None and ps is not None and self.seen > self.slow:
            if pf <= ps and self.e_fast > self.e_slow:
                self.cross = {"direction": 1, "bar": self.bar_no, "entered": False}
            elif pf >= ps and self.e_fast < self.e_slow:
                self.cross = {"direction": -1, "bar": self.bar_no, "entered": False}
        x = self.cross
        if x is None or x["entered"] or self.bar_no - x["bar"] > self.within \
                or rsi is None or atr is None:
            return ev
        d = x["direction"]
        if (self.e_fast - self.e_slow) * d <= 0:
            return ev
        ef, es = self.e_fast, self.e_slow
        if d > 0:
            ok = (c > ef and c > es and lo >= es and c > o
                  and self.rsi_mid < rsi < self.rsi_hi)
        else:
            ok = (c < ef and c < es and h <= es and c < o
                  and self.rsi_lo < rsi < self.rsi_mid)
        if not ok:
            ev.reason = "after the crossover, waiting for a confirming candle"
            return ev
        if self.session_bars <= self.warmup:
            ev.reason = f"session warm-up: bar {self.session_bars} of the day (needs > {self.warmup})"
            return ev
        if self.window and hasattr(at, "hour"):
            m = at.hour * 60 + at.minute
            if not (_hm(self.window[0]) <= m < _hm(self.window[1])):
                ev.reason = "outside the entry window"
                return ev
        if d > 0:
            stop = min(min(self.lows), c - self.min_stop_atr * atr)
        else:
            stop = max(max(self.highs), c + self.min_stop_atr * atr)
        risk = (c - stop) * d
        if risk <= 0:
            return ev
        x["entered"] = True
        ev.action = BUY if d > 0 else SELL
        ev.trigger_type = BULLISH if d > 0 else BEARISH
        ev.entry_price, ev.stop_loss = round(c, 4), round(stop, 4)
        ev.take_profit = round(c + d * self.rr * risk, 4)
        ev.metadata = {**meta, "risk": round(risk, 4), "bars_after_cross": self.bar_no - x["bar"]}
        self.position = {"direction": d, "entry": c, "stop": stop,
                         "target": c + d * self.rr * risk, "last_close": c, "id": ev.signal_id}
        return ev

    def _exit(self, ts: Any, price: float, why: str, meta: dict[str, Any]) -> SignalEvent:
        p, self.position = self.position, None
        d = p["direction"]
        r = (price - p["entry"]) * d / abs(p["entry"] - p["stop"])
        return SignalEvent(timestamp=ts, action=EXIT, entry_price=round(price, 4), reason=why,
                           metadata={**meta, "r": round(r, 3), "of": p["id"],
                                     "direction": "LONG" if d > 0 else "SHORT"})


# --------------------------------------------------------------------------- #
# Settings, the live desk and the backtest
# --------------------------------------------------------------------------- #
def engine_from(cfg: Any, tz: str | None = None, **over: Any) -> SJK912_RSI_Engine:
    g = cfg.get
    p = f"{KEY}."
    kw = dict(fast=int(g(p + "fast", 9)), slow=int(g(p + "slow", 21)),  # noqa: C408
              rsi_len=int(g(p + "rsi_length", 14)), rsi_mid=float(g(p + "rsi_level", 50.0)),
              rsi_long_max=float(g(p + "rsi_long_max", 68.0)),
              rsi_short_min=float(g(p + "rsi_short_min", 32.0)),
              rr=float(g(p + "rr", 2.0)), swing_lookback=int(g(p + "swing_lookback", 5)),
              cross_within=int(g(p + "cross_within", 3)),
              min_stop_atr=float(g(p + "min_stop_atr", 0.5)),
              warmup_bars=int(g(p + "warmup_bars", 5)), tz=tz,
              window=(str(g(p + "from", "09:45")), str(g(p + "to", "15:45"))),
              square_off=str(g("system.square_off_time", "15:15")))
    kw.update(over)
    return SJK912_RSI_Engine(**kw)


def detect_candles(bars: list[Any], cfg: Any, tz: str) -> dict[str, Any] | None:
    """A BUY / SELL on the LATEST closed 5m candle, for the analyst, or None."""
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
            "trigger": ev.trigger_type, "rsi": m.get("rsi"), "ema_fast": m.get("ema9"),
            "ema_slow": m.get("ema21"), "ts": bars[-1].ts.isoformat(), "setup": SETUP_NAME,
            "note": (f"9 EMA {'above' if long else 'below'} the 21 EMA "
                     f"{m.get('bars_after_cross')} bar(s) after the crossover; a "
                     f"{'green' if long else 'red'} candle closing beyond both EMAs with its "
                     f"{'low' if long else 'high'} clear of the 21; RSI {m.get('rsi')}; stop "
                     f"{ev.stop_loss:,.2f}")}


def backtest(bars: list[Any], cfg: Any, tz: str, cost_pct: float = 0.0,
             **over: Any) -> list[dict[str, Any]]:
    """The engine over historical 5m candles, one trade at a time; R net of
    `cost_pct` % round-trip costs."""
    over.pop("warmup", None)
    over.pop("detector", None)
    eng = engine_from(cfg, tz, **over)
    trades: list[dict[str, Any]] = []
    open_: dict[str, Any] | None = None
    for b in bars:
        ev = eng.on_bar_update(b)
        if ev.action in (BUY, SELL):
            open_ = {"ts": b.ts.isoformat(), "direction": "LONG" if ev.action == BUY else "SHORT",
                     "entry": ev.entry_price, "stop": ev.stop_loss, "target": ev.take_profit,
                     "risk": abs(ev.entry_price - ev.stop_loss), "kind": ev.trigger_type}
        elif ev.action == EXIT and open_ is not None:
            cost_r = cost_pct / 100.0 * open_["entry"] / open_["risk"] if open_["risk"] else 0.0
            outcome = {"stop": "STOP", "target": "TARGET"}.get(ev.reason, "SQUARE_OFF")
            trades.append({**open_, "exit": ev.entry_price, "exit_ts": b.ts.isoformat(),
                           "outcome": outcome,
                           "r": round(float(ev.metadata.get("r", 0.0)) - cost_r, 3)})
            open_ = None
    return trades


report = _report
