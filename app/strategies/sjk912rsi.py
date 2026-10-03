"""sjk912RSi — the user's 9/21 EMA + RSI crossover strategy (5 Oct 2026).

    LONG   the 9 EMA crosses above the 21 EMA — the entry may come on that bar
           or within `cross_within` bars after it while the 9 stays above —
           on a candle whose open AND close are strictly above both EMAs, that
           closes up (close > open), with RSI(14) > 50. Enter on the close.
           Stop: the lowest low of the `swing_lookback` (5) bars BEFORE the
           entry bar. Target: entry + rr (2) x risk.
    SHORT  the mirror: 9 crosses below 21; open and close strictly below both;
           close < open; RSI < 50; stop the highest high of the 5 bars before.

One entry per crossover, one position at a time; intraday (out by the
square-off). The same rules in Pine Script for TradingView: pine/sjk912RSi.pine.

The parts: Rsi (Wilder's, incremental), SJK912_RSI_Engine.on_bar_update()
-> SignalEvent (the same contract as sjk912_vwapadx), detect_candles() for the
live desk and backtest(). panaoptions carries the same engine in
panaoptions/strategies/sjk912rsi.py.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from app.strategies.sjk912_vwapadx import BUY, EXIT, SELL, SignalEvent, ema_step

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
                 rsi_level: float = 50.0, rr: float = 2.0, swing_lookback: int = 5,
                 cross_within: int = 5, tz: str | None = None,
                 window: tuple[str, str] | None = None, square_off: str | None = None,
                 tick: float = 0.0) -> None:
        self.fast, self.slow, self.rsi_level, self.rr = fast, slow, rsi_level, rr
        self.lookback, self.within = swing_lookback, cross_within
        self.tz, self.window, self.square_off, self.tick = tz, window, square_off, tick
        self.e_fast: float | None = None
        self.e_slow: float | None = None
        self.rsi = Rsi(rsi_len)
        self.highs: deque[float] = deque(maxlen=swing_lookback)    # bars BEFORE this one
        self.lows: deque[float] = deque(maxlen=swing_lookback)
        self.seen = 0
        self.bar_no = 0
        self.cross: dict[str, Any] | None = None       # {direction, bar, used}
        self.position: dict[str, Any] | None = None
        self.day: Any = None

    def _local(self, ts: Any) -> Any:
        if self.tz and getattr(ts, "tzinfo", None):
            from zoneinfo import ZoneInfo
            return ts.astimezone(ZoneInfo(self.tz))
        return ts

    def on_bar_update(self, bar: Any) -> SignalEvent:
        ts = bar.ts
        at = self._local(ts)
        o, h, lo, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
        ev = SignalEvent(timestamp=ts)
        day = at.date() if hasattr(at, "date") else None
        prior_lows, prior_highs = list(self.lows), list(self.highs)

        pf, ps = self.e_fast, self.e_slow
        self.e_fast = ema_step(self.e_fast, c, self.fast)
        self.e_slow = ema_step(self.e_slow, c, self.slow)
        rsi = self.rsi.update(c)
        self.bar_no += 1
        self.seen += 1
        self.highs.append(h)
        self.lows.append(lo)
        ev.metadata = {"rsi": None if rsi is None else round(rsi, 2),
                       "ema_fast": round(self.e_fast, 4), "ema_slow": round(self.e_slow, 4)}

        # ---- the open position: a new day, the stop, the target, the square-off
        if self.position is not None:
            p = self.position
            if day != self.day:
                return self._exit(ts, p["last_close"], "session ended", ev.metadata)
            long = p["direction"] > 0
            p["last_close"] = c
            if (lo <= p["stop"]) if long else (h >= p["stop"]):
                return self._exit(ts, p["stop"], "stop", ev.metadata)
            if (h >= p["target"]) if long else (lo <= p["target"]):
                return self._exit(ts, p["target"], "target", ev.metadata)
            if self.square_off and hasattr(at, "hour") and \
                    at.hour * 60 + at.minute >= _hm(self.square_off):
                return self._exit(ts, c, "square-off", ev.metadata)
            ev.reason = "in a position"
            return ev
        self.day = day

        # ---- the crossover (remembered for cross_within bars)
        if pf is not None and ps is not None and self.seen > self.slow:
            if pf <= ps and self.e_fast > self.e_slow:
                self.cross = {"direction": 1, "bar": self.bar_no, "used": False}
            elif pf >= ps and self.e_fast < self.e_slow:
                self.cross = {"direction": -1, "bar": self.bar_no, "used": False}
        x = self.cross
        if x is None or x["used"] or self.bar_no - x["bar"] > self.within or rsi is None:
            return ev
        d = x["direction"]
        if (self.e_fast - self.e_slow) * d <= 0:
            return ev                                    # the cross has undone itself
        hi_ema, lo_ema = max(self.e_fast, self.e_slow), min(self.e_fast, self.e_slow)
        if d > 0:
            ok = o > hi_ema and c > hi_ema and c > o and rsi > self.rsi_level
        else:
            ok = o < lo_ema and c < lo_ema and c < o and rsi < self.rsi_level
        if not ok:
            ev.reason = "after the crossover, waiting for a confirming candle"
            return ev
        if self.window and hasattr(at, "hour"):
            m = at.hour * 60 + at.minute
            if not (_hm(self.window[0]) <= m < _hm(self.window[1])):
                ev.reason = "outside the entry window"
                return ev
        if len(prior_lows) < self.lookback:
            return ev
        stop = (min(prior_lows) - self.tick) if d > 0 else (max(prior_highs) + self.tick)
        risk = (c - stop) * d
        if risk <= 0:
            ev.reason = "no risk to define"
            return ev
        x["used"] = True
        ev.action = BUY if d > 0 else SELL
        ev.trigger_type = BULLISH if d > 0 else BEARISH
        ev.entry_price, ev.stop_loss = round(c, 4), round(stop, 4)
        ev.take_profit = round(c + d * self.rr * risk, 4)
        ev.metadata = {**ev.metadata, "risk": round(risk, 4),
                       "bars_after_cross": self.bar_no - x["bar"]}
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
def engine_from(cfg: Any, tz: str | None = None) -> SJK912_RSI_Engine:
    g = cfg.get
    p = f"{KEY}."
    return SJK912_RSI_Engine(
        fast=int(g(p + "fast", 9)), slow=int(g(p + "slow", 21)),
        rsi_len=int(g(p + "rsi_length", 14)), rsi_level=float(g(p + "rsi_level", 50.0)),
        rr=float(g(p + "rr", 2.0)), swing_lookback=int(g(p + "swing_lookback", 5)),
        cross_within=int(g(p + "cross_within", 5)), tz=tz,
        window=(str(g(p + "from", "09:45")), str(g(p + "to", "15:45"))),
        square_off=str(g("system.square_off_time", "15:15")),
        tick=float(g(p + "stop_buffer", 0.0) or 0.0))


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
            "trigger": ev.trigger_type, "rsi": m.get("rsi"), "ema_fast": m.get("ema_fast"),
            "ema_slow": m.get("ema_slow"), "ts": bars[-1].ts.isoformat(), "setup": SETUP_NAME,
            "note": (f"9 EMA {'above' if long else 'below'} the 21 EMA "
                     f"{m.get('bars_after_cross')} bar(s) after the crossover; a "
                     f"{'green' if long else 'red'} candle opening and closing "
                     f"{'above' if long else 'below'} both EMAs; RSI {m.get('rsi')}; stop at "
                     f"the {'lowest low' if long else 'highest high'} of the 5 bars before "
                     f"{ev.stop_loss:,.2f}")}


def backtest(bars: list[Any], cfg: Any, tz: str, **_: Any) -> list[dict[str, Any]]:
    """The engine over historical 5m candles, one trade at a time."""
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
            outcome = {"stop": "STOP", "target": "TARGET"}.get(ev.reason, "SQUARE_OFF")
            trades.append({**open_, "exit": ev.entry_price, "exit_ts": b.ts.isoformat(),
                           "outcome": outcome, "r": ev.metadata.get("r", 0.0)})
            open_ = None
    return trades
