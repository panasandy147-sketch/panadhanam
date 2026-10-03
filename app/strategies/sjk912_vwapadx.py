"""SJK 9/21 · VWAP · ADX — the user's confluence model (sjk912_vwapadx), v2 (5 Oct 2026).

    BUY    bar t-1 (the Crossover Candle): the 9 EMA crosses ABOVE the 21 EMA.
           bar t   (the Confirmation Candle, the very next one — else the setup
           expires): close strictly above the 9 EMA, the 21 EMA and the session
           VWAP, no more than vwap_cap (2.5) x ATR(14) from the VWAP; ADX(14) >
           20 AND rising (ADX[t] > ADX[t-1]) with +DI > -DI. Enter on that close.
           Stop: min(crossover low, confirmation low) - 2 ticks, at least
           1.0 x ATR(14) away. Target 1: 1:rr (2). exit_mode "split" (Variant
           B): half there, the stop to breakeven, the rest held to the opposing
           9/21 crossover or the session's end; "target" (Variant A): all at
           Target 1.
    SELL   the mirror: crossunder, close below 9 / 21 / VWAP, -DI > +DI, stop
           max(crossover high, confirmation high) + 2 ticks.
    NONE   Sideways / Choppy — ADX <= 20, |EMA9 - EMA21| < 0.25 x ATR, or the
           VWAP trapped between the 9 and the 21 EMA.

The parts:

  Indicators      ema_step(), Atr (Wilder's), SessionVwap (typical price x
                  volume, reset at each market day's open), Adx (Wilder's,
                  with +DI / -DI)
  SignalEvent     the output contract: id, time, action (BUY / SELL /
                  PARTIAL_EXIT_TP1 / FULL_EXIT / NONE), trigger, entry, stop,
                  take-profit 1, the take-profit-2 rule, metadata (ADX, +DI,
                  -DI, VWAP, EMA9, EMA21, ATR14)
  SJK912_VWAP_ADX_Engine
                  on_bar_update(bar) -> SignalEvent; a deterministic state
                  machine IDLE -> AWAITING_CONFIRMATION -> IN_POSITION_FULL ->
                  IN_POSITION_RUNNER -> IDLE; no lookahead
  detect_candles  the live desk's question: a BUY / SELL on the latest candle?
  backtest / BacktestRunner
                  the engine over historical candles, round-trip costs
                  (0.05%), partial exits, and the report: trades, TP1 hit rate,
                  runner avg R, blended expectancy, profit factor, max DD (R)

The EMAs, ATR and ADX run continuously (they need 21 / 28 bars to settle);
the VWAP, a pending crossover and any open position reset each market day.
panaoptions carries the same engine in panaoptions/strategies/sjk912_vwapadx.py.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

KEY = "sjk912_vwapadx"                      # the settings section
SETUP_NAME = "SJK 9/21 · VWAP · ADX"
BUY, SELL, NONE = "BUY", "SELL", "NONE"
PARTIAL_EXIT_TP1, FULL_EXIT = "PARTIAL_EXIT_TP1", "FULL_EXIT"
EXIT = FULL_EXIT                            # what the other engines call a full exit
BULLISH, BEARISH = "Bullish_Confluence", "Bearish_Confluence"
SIDEWAYS = "Sideways / Choppy"
IDLE, AWAITING, FULL, RUNNER = ("IDLE", "AWAITING_CONFIRMATION", "IN_POSITION_FULL",
                                "IN_POSITION_RUNNER")


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def ema_step(prev: float | None, value: float, n: int) -> float:
    """One step of an EMA seeded with the first value."""
    if prev is None:
        return float(value)
    k = 2.0 / (n + 1)
    return float(value) * k + prev * (1 - k)


class Atr:
    """Wilder's ATR, one bar at a time (TradingView's ta.atr). None until
    `n` true ranges have been seen."""

    def __init__(self, n: int = 14) -> None:
        self.n = n
        self.prev_close: float | None = None
        self.count = 0
        self.total = 0.0
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        tr = high - low if self.prev_close is None else max(
            high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        self.count += 1
        if self.value is None:
            self.total += tr
            if self.count >= self.n:
                self.value = self.total / self.n
        else:
            self.value = (self.value * (self.n - 1) + tr) / self.n
        return self.value


class SessionVwap:
    """Session VWAP on the typical price (H + L + C) / 3, weighted by volume,
    reset at each new market day — overnight and prior-day volume never
    count. A feed with no volume (an index) weights every bar equally."""

    def __init__(self) -> None:
        self.day: Any = None
        self.pv = self.v = self.tp_sum = 0.0
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
    """Wilder's ADX with +DI and -DI. `value`, `pdi`, `mdi`; None until the
    ADX has settled (period + smoothing bars)."""

    def __init__(self, period: int = 14, smoothing: int = 14) -> None:
        self.n, self.s = period, smoothing
        self.prev: tuple[float, float, float] | None = None
        self.tr = self.pdm = self.mdm = 0.0
        self.count = 0
        self.dx: list[float] = []
        self.value: float | None = None
        self.pdi = self.mdi = 0.0

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
        if self.count <= self.n:
            self.tr += tr
            self.pdm += pdm
            self.mdm += mdm
            if self.count < self.n:
                return None
        else:
            self.tr += tr - self.tr / self.n
            self.pdm += pdm - self.pdm / self.n
            self.mdm += mdm - self.mdm / self.n
        if self.tr <= 0:
            return self.value
        self.pdi, self.mdi = 100 * self.pdm / self.tr, 100 * self.mdm / self.tr
        s = self.pdi + self.mdi
        dx = 100 * abs(self.pdi - self.mdi) / s if s > 0 else 0.0
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
    action: str = NONE
    trigger_type: str = ""
    entry_price: float | None = None       # the entry, or the exit price on an exit
    stop_loss: float | None = None
    take_profit: float | None = None       # Target 1
    take_profit2_rule: str = ""
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        ts = out["timestamp"]
        out["timestamp"] = ts.isoformat() if hasattr(ts, "isoformat") else ts
        return out


def _hm(value: str) -> int:
    h, _, m = str(value).partition(":")
    return int(h) * 60 + int(m or 0)


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class SJK912_VWAP_ADX_Engine:
    """Feed closed bars in order with on_bar_update(); one SignalEvent each.
    `state` is IDLE, AWAITING_CONFIRMATION, IN_POSITION_FULL or
    IN_POSITION_RUNNER."""

    def __init__(self, fast: int = 9, slow: int = 21, adx_period: int = 14,
                 adx_smoothing: int = 14, adx_min: float = 20.0, rr: float = 2.0,
                 exit_mode: str = "split", atr_period: int = 14, vwap_cap_atr: float = 2.5,
                 min_stop_atr: float = 1.0, stop_ticks: int = 2, tick: float = 0.01,
                 min_spread_atr: float = 0.25, tz: str | None = None,
                 window: tuple[str, str] | None = None, square_off: str | None = None,
                 **_: Any) -> None:
        self.fast, self.slow, self.adx_min, self.rr = fast, slow, adx_min, rr
        self.exit_mode = exit_mode               # "split" (Variant B) | "target" (A)
        self.vwap_cap, self.min_stop_atr = vwap_cap_atr, min_stop_atr
        self.stop_pad = stop_ticks * tick
        self.min_spread_atr = min_spread_atr
        self.tz, self.window, self.square_off = tz, window, square_off
        self.e_fast: float | None = None
        self.e_slow: float | None = None
        self.atr = Atr(atr_period)
        self.adx = Adx(adx_period, adx_smoothing)
        self.prev_adx: float | None = None
        self.vwap = SessionVwap()
        self.day: Any = None
        self.seen = 0
        self.state = IDLE
        self.cross: dict[str, Any] | None = None
        self.position: dict[str, Any] | None = None

    def _local(self, ts: Any) -> Any:
        if self.tz and getattr(ts, "tzinfo", None):
            from zoneinfo import ZoneInfo
            return ts.astimezone(ZoneInfo(self.tz))
        return ts

    def chop_reason(self, close: float, vwap: float, adx: float | None,
                    atr: float | None) -> str:
        """'' when the market trends, else why it is Sideways / Choppy."""
        if adx is None or atr is None:
            return "ADX / ATR not settled yet"
        if adx <= self.adx_min:
            return f"ADX {adx:.1f} <= {self.adx_min:g}"
        if abs(self.e_fast - self.e_slow) < self.min_spread_atr * atr:
            return (f"the 9 and 21 EMAs are within {self.min_spread_atr:g} x ATR "
                    f"({abs(self.e_fast - self.e_slow):.4f} < {self.min_spread_atr * atr:.4f})")
        if min(self.e_fast, self.e_slow) < vwap < max(self.e_fast, self.e_slow):
            return "VWAP is trapped between the 9 and the 21 EMA"
        return ""

    # ---- the bar ---------------------------------------------------------- #
    def on_bar_update(self, bar: Any) -> SignalEvent:
        ts = bar.ts
        at = self._local(ts)
        day = at.date() if hasattr(at, "date") else None
        h, lo, c = float(bar.high), float(bar.low), float(bar.close)
        vol = float(getattr(bar, "volume", 0.0) or 0.0)

        new_day = day != self.day
        self.day = day
        pf, ps = self.e_fast, self.e_slow
        self.e_fast = ema_step(self.e_fast, c, self.fast)
        self.e_slow = ema_step(self.e_slow, c, self.slow)
        atr = self.atr.update(h, lo, c)
        prev_adx, adx = self.prev_adx, self.adx.update(h, lo, c)
        self.prev_adx = adx
        vwap = self.vwap.update(day, h, lo, c, vol)
        self.seen += 1
        crossed = 0
        if pf is not None and ps is not None and self.seen > self.slow:
            if pf <= ps and self.e_fast > self.e_slow:
                crossed = 1
            elif pf >= ps and self.e_fast < self.e_slow:
                crossed = -1
        meta = {"adx": None if adx is None else round(adx, 2),
                "di_plus": round(self.adx.pdi, 2), "di_minus": round(self.adx.mdi, 2),
                "vwap": round(vwap, 4), "ema9": round(self.e_fast, 4),
                "ema21": round(self.e_slow, 4), "atr14": None if atr is None else round(atr, 4)}
        ev = SignalEvent(timestamp=ts, metadata=meta)

        # ---- a new day: an open position is closed at yesterday's last close
        if new_day:
            self.cross = None
            if self.position is not None:
                return self._full_exit(ts, self.position["last_close"], "session ended", meta)
            self.state = IDLE

        # ---- managing the position
        if self.position is not None:
            return self._manage(ts, at, h, lo, c, crossed, meta)

        # ---- AWAITING_CONFIRMATION: this is bar t, the only chance
        if self.state == AWAITING and self.cross is not None:
            x, self.cross, self.state = self.cross, None, IDLE
            d = x["direction"]
            fired = self._confirm(ev, at, x, d, h, lo, c, vwap, adx, prev_adx, atr)
            if fired:
                return ev
            if not ev.reason:
                ev.reason = "the confirmation candle did not confirm — setup expired"
        # ---- a new crossover: bar t-1
        if crossed:
            self.cross = {"direction": crossed, "high": h, "low": lo, "ts": ts}
            self.state = AWAITING
            ev.reason = ev.reason or "crossover candle — waiting for the next candle"
        return ev

    def _confirm(self, ev: SignalEvent, at: Any, x: dict[str, Any], d: int, h: float,
                 lo: float, c: float, vwap: float, adx: float | None,
                 prev_adx: float | None, atr: float | None) -> bool:
        ef, es = self.e_fast, self.e_slow
        side_ok = (c > ef and c > es and c > vwap) if d > 0 else (c < ef and c < es and c < vwap)
        if not side_ok:
            ev.reason = (f"the confirmation candle closed {'below' if d > 0 else 'above'} the "
                         f"9 EMA, 21 EMA or VWAP — setup expired")
            return False
        why = self.chop_reason(c, vwap, adx, atr)
        if why:
            ev.reason = f"{SIDEWAYS}: {why}"
            return False
        if prev_adx is None or adx <= prev_adx:
            ev.reason = f"ADX {adx:.1f} is not rising (was {prev_adx or 0:.1f})"
            return False
        if (self.adx.pdi <= self.adx.mdi) if d > 0 else (self.adx.mdi <= self.adx.pdi):
            ev.reason = (f"{'+DI' if d > 0 else '-DI'} is not leading "
                         f"(+DI {self.adx.pdi:.1f}, -DI {self.adx.mdi:.1f})")
            return False
        if abs(c - vwap) > self.vwap_cap * atr:
            ev.reason = (f"over-extended: {abs(c - vwap):.2f} from VWAP, more than "
                         f"{self.vwap_cap:g} x ATR {atr:.2f}")
            return False
        if self.window and hasattr(at, "hour"):
            m = at.hour * 60 + at.minute
            if not (_hm(self.window[0]) <= m < _hm(self.window[1])):
                ev.reason = "outside the entry window"
                return False
        if d > 0:
            stop = min(x["low"], lo) - self.stop_pad
            stop = min(stop, c - self.min_stop_atr * atr)
        else:
            stop = max(x["high"], h) + self.stop_pad
            stop = max(stop, c + self.min_stop_atr * atr)
        risk = (c - stop) * d
        target = c + d * self.rr * risk
        ev.action = BUY if d > 0 else SELL
        ev.trigger_type = BULLISH if d > 0 else BEARISH
        ev.entry_price, ev.stop_loss, ev.take_profit = round(c, 4), round(stop, 4), round(target, 4)
        ev.take_profit2_rule = ("opposing 9/21 EMA crossover or the session's end (half the "
                                "position, stop at breakeven)" if self.exit_mode == "split"
                                else "none — the whole position exits at Target 1")
        ev.metadata = {**ev.metadata, "crossover_ts": str(x["ts"]), "risk": round(risk, 4)}
        self.position = {"direction": d, "entry": c, "stop": stop, "target": target,
                         "risk": risk, "last_close": c, "id": ev.signal_id, "booked_r": 0.0,
                         "size": 1.0}
        self.state = FULL
        return True

    def _manage(self, ts: Any, at: Any, h: float, lo: float, c: float, crossed: int,
                meta: dict[str, Any]) -> SignalEvent:
        p = self.position
        d = p["direction"]
        long = d > 0
        p["last_close"] = c
        if (lo <= p["stop"]) if long else (h >= p["stop"]):
            return self._full_exit(ts, p["stop"], "stop" if self.state == FULL
                                   else "runner stopped at breakeven", meta)
        if self.state == FULL and ((h >= p["target"]) if long else (lo <= p["target"])):
            if self.exit_mode != "split":
                return self._full_exit(ts, p["target"], "target", meta)
            # Target 1: half off, the stop to breakeven, the runner rides.
            p["booked_r"] += 0.5 * self.rr
            p["size"] = 0.5
            p["stop"] = p["entry"]
            self.state = RUNNER
            return SignalEvent(timestamp=ts, action=PARTIAL_EXIT_TP1,
                               entry_price=round(p["target"], 4), reason="target 1 — half off",
                               metadata={**meta, "of": p["id"], "r_booked": p["booked_r"]})
        if self.state == RUNNER and crossed == -d:
            return self._full_exit(ts, c, "opposing crossover", meta)
        if self.square_off and hasattr(at, "hour") and \
                at.hour * 60 + at.minute >= _hm(self.square_off):
            return self._full_exit(ts, c, "square-off", meta)
        return SignalEvent(timestamp=ts, reason="in a position", metadata=meta)

    def _full_exit(self, ts: Any, price: float, why: str, meta: dict[str, Any]) -> SignalEvent:
        p, self.position = self.position, None
        self.state = IDLE
        d = p["direction"]
        r_rest = (price - p["entry"]) * d / p["risk"]
        r = p["booked_r"] + p["size"] * r_rest
        return SignalEvent(timestamp=ts, action=FULL_EXIT, entry_price=round(price, 4),
                           reason=why, metadata={**meta, "r": round(r, 3), "of": p["id"],
                                                 "tp1": p["size"] < 1.0,
                                                 "runner_r": round(r_rest, 3) if p["size"] < 1.0
                                                 else None,
                                                 "direction": "LONG" if d > 0 else "SHORT"})


# --------------------------------------------------------------------------- #
# Settings, the live desk and the backtest
# --------------------------------------------------------------------------- #
def engine_from(cfg: Any, tz: str | None = None, **over: Any) -> SJK912_VWAP_ADX_Engine:
    g = cfg.get
    p = f"{KEY}."
    tick = 0.01 if str(getattr(cfg, "active_market", "IN")).upper() == "US" else 0.05
    kw = dict(fast=int(g(p + "fast", 9)), slow=int(g(p + "slow", 21)),  # noqa: C408
              adx_period=int(g(p + "adx_period", 14)),
              adx_smoothing=int(g(p + "adx_smoothing", 14)),
              adx_min=float(g(p + "adx_min", 20.0)), rr=float(g(p + "rr", 2.0)),
              exit_mode=str(g(p + "exit_mode", "split")),
              vwap_cap_atr=float(g(p + "vwap_cap_atr", 2.5)),
              min_stop_atr=float(g(p + "min_stop_atr", 1.0)),
              stop_ticks=int(g(p + "stop_ticks", 2)), tick=tick,
              min_spread_atr=float(g(p + "min_spread_atr", 0.25)), tz=tz,
              window=(str(g(p + "from", "09:45")), str(g(p + "to", "15:45"))),
              square_off=str(g("system.square_off_time", "15:15")))
    kw.update(over)
    return SJK912_VWAP_ADX_Engine(**kw)


def detect_candles(bars: list[Any], cfg: Any, tz: str) -> dict[str, Any] | None:
    """A BUY / SELL on the LATEST closed 5m candle, for the analyst, or None.
    The engine replays the tape, so its one-position memory is the backtest's."""
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
            "ema_fast": m.get("ema9"), "ema_slow": m.get("ema21"),
            "ts": bars[-1].ts.isoformat(), "setup": SETUP_NAME,
            "note": (f"9 EMA crossed {'above' if long else 'below'} the 21 EMA; the next "
                     f"close {ev.entry_price:,.2f} {'above' if long else 'below'} the 9 / 21 "
                     f"EMAs and VWAP {m.get('vwap'):,.2f}; ADX {m.get('adx')} rising, "
                     f"{'+DI' if long else '-DI'} leading; stop {ev.stop_loss:,.2f}")}


def backtest(bars: list[Any], cfg: Any, tz: str, cost_pct: float = 0.05,
             **over: Any) -> list[dict[str, Any]]:
    """The engine over historical 5m candles, one trade at a time, each
    trade's R net of `cost_pct` % round-trip costs (on the entry price)."""
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
        elif ev.action == FULL_EXIT and open_ is not None:
            cost_r = cost_pct / 100.0 * open_["entry"] / open_["risk"] if open_["risk"] else 0.0
            r = float(ev.metadata.get("r", 0.0)) - cost_r
            outcome = {"stop": "STOP", "target": "TARGET", "opposing crossover": "CROSS",
                       "runner stopped at breakeven": "BREAKEVEN"}.get(ev.reason, "SQUARE_OFF")
            trades.append({**open_, "exit": ev.entry_price, "exit_ts": b.ts.isoformat(),
                           "outcome": outcome, "r": round(r, 3),
                           "tp1": bool(ev.metadata.get("tp1")),
                           "runner_r": ev.metadata.get("runner_r")})
            open_ = None
    return trades


def report(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """The BacktestRunner's report: trades, TP1 hit rate, runner avg R,
    blended expectancy, profit factor, max drawdown (R)."""
    if not trades:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    wins, losses = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    runners = [t["runner_r"] for t in trades if t.get("runner_r") is not None]
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"trades": len(rs),
            "tp1_hit_rate": round(100 * sum(1 for t in trades if t.get("tp1") or
                                            t["outcome"] == "TARGET") / len(rs), 1),
            "win_rate": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1),
            "runner_avg_r": round(sum(runners) / len(runners), 3) if runners else None,
            "expectancy_r": round(sum(rs) / len(rs), 3),
            "profit_factor": round(wins / losses, 2) if losses else None,
            "max_drawdown_r": round(dd, 2)}


class BacktestRunner:
    """Runs the engine over many symbols' candles and reports Variant A (all
    at Target 1) beside Variant B (half at Target 1, the runner to the
    opposing crossover)."""

    def __init__(self, cfg: Any, tz: str) -> None:
        self.cfg, self.tz = cfg, tz

    def run(self, candles_by_symbol: dict[str, list[Any]]) -> dict[str, Any]:
        out = {}
        for variant, mode in (("A_full_at_tp1", "target"), ("B_split_runner", "split")):
            trades = []
            for sym, bars in candles_by_symbol.items():
                trades += [{**t, "symbol": sym}
                           for t in backtest(bars, self.cfg, self.tz, exit_mode=mode)]
            out[variant] = report(trades)
        return out
