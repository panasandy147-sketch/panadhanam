"""SJK 9-15-21 — the 9 / 15 / 21 EMA Master Trading Strategy, v2 (the user's, 5 Oct 2026).

    LONG   strict ascending fan-out: EMA 9 > EMA 15 > EMA 21, all three rising
           (EMA[t] > EMA[t-1]), and not Sideways / Choppy.
           Trigger A  ALIGNMENT_BREAKOUT: the exact bar the market turns into
                      that valid alignment, on a green candle closing in the
                      top 40% of its range ((close - low) >= 0.6 x range).
           Trigger B  RIBBON_PULLBACK: while the alignment holds, the low dips
                      into the ribbon (low <= EMA 15) without a close under
                      the 21 (close >= EMA 21), and the candle closes up and
                      above the 9 EMA.
           Stop: min(lowest low of the last 5 bars, EMA 21) - 0.2 x ATR(14).
           Target: entry + rr (2) x risk.
    SHORT  the mirror (21 > 15 > 9, all falling).
    NONE   CHOPPY/SIDEWAYS — any of: |EMA9 - EMA21| < spread_atr (0.35) x
           ATR(14); the three EMAs not all sloping the trade's way; EMA 9 and
           21 crossing 2+ times in the last 12 bars.

One ALIGNMENT_BREAKOUT and one RIBBON_PULLBACK at most per alignment cycle
(NEUTRAL -> BULL_CYCLE / BEAR_CYCLE; the cycle ends when the EMA order
breaks), one position at a time, intraday.

The parts: ema_step / Atr (from sjk912_vwapadx), chop_reason(),
SJK91521Engine.on_bar_update() -> TradeSignal, calculate_signals(history),
detect_candles() for the live desk, engine_backtest() with per-trigger
attribution, and the generic bar walker backtest() (SJK 50-200 uses it).
panaoptions carries the same engine in panaoptions/strategies/sjk_9_15_21.py.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from app.strategies.sjk912_vwapadx import Atr, ema_step

KEY = "sjk_9_15_21"                     # the settings section
SETUP_NAME = "SJK 9-15-21 · EMA Fan"
SIDEWAYS = "CHOPPY/SIDEWAYS"
BREAKOUT, PULLBACK = "ALIGNMENT_BREAKOUT", "RIBBON_PULLBACK"
NEUTRAL, BULL, BEAR = "NEUTRAL", "BULL_CYCLE", "BEAR_CYCLE"
# Chart colours (the dashboards draw the same three lines).
COLOURS = {"fast": "#a855f7", "mid": "#3b82f6", "slow": "#6b7280"}   # purple, blue, dark grey


@dataclass
class TradeSignal:
    """The output contract."""
    timestamp: Any
    signal_type: str = "HOLD"              # BUY / SELL / HOLD / EXIT
    trigger_mode: str = ""                 # ALIGNMENT_BREAKOUT / RIBBON_PULLBACK
    entry_price: float | None = None       # the entry, or the exit price on an EXIT
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    risk_reward_ratio: float | None = None
    reason: str = ""
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        ts = out["timestamp"]
        out["timestamp"] = ts.isoformat() if hasattr(ts, "isoformat") else ts
        return out


def chop_reason(d: int, e9: float, e15: float, e21: float, slopes: tuple[float, float, float],
                atr: float, crosses_recent: int, spread_atr: float = 0.35,
                max_crosses: int = 2) -> str:
    """'' when the fan-out in direction d is clean, else why it is CHOPPY/SIDEWAYS."""
    if abs(e9 - e21) < spread_atr * atr:
        return f"spread compression: |EMA9 - EMA21| < {spread_atr:g} x ATR"
    if not all((s * d) > 0 for s in slopes):
        return "slope divergence: the 9, 15 and 21 EMAs are not all sloping the same way"
    if crosses_recent >= max_crosses:
        return f"whip-saw: EMA 9 and 21 crossed {crosses_recent} times in the last 12 bars"
    return ""


def _hm(value: str) -> int:
    h, _, m = str(value).partition(":")
    return int(h) * 60 + int(m or 0)


class SJK91521Engine:
    """Feed closed bars with on_bar_update(); one TradeSignal each."""

    def __init__(self, fast: int = 9, mid: int = 15, slow: int = 21, atr_period: int = 14,
                 swing: int = 5, stop_atr: float = 0.20, rr: float = 2.0,
                 spread_atr: float = 0.35, whipsaw_bars: int = 12, max_crosses: int = 2,
                 breakout_body: float = 0.60, triggers: str = "both",
                 tz: str | None = None, window: tuple[str, str] | None = None,
                 square_off: str | None = None, **_: Any) -> None:
        self.n = (fast, mid, slow)
        self.swing, self.stop_atr, self.rr = swing, stop_atr, rr
        self.spread_atr, self.whipsaw_bars, self.max_crosses = spread_atr, whipsaw_bars, max_crosses
        self.body, self.triggers = breakout_body, triggers      # "both" | "pullback" | "breakout"
        self.tz, self.window, self.square_off = tz, window, square_off
        self.e: list[float | None] = [None, None, None]
        self.atr = Atr(atr_period)
        self.lows: deque[float] = deque(maxlen=swing)
        self.highs: deque[float] = deque(maxlen=swing)
        self.crosses: deque[int] = deque()
        self.bar_no = 0
        self.valid_prev = 0
        self.cycle = NEUTRAL
        self.used: set[str] = set()
        self.position: dict[str, Any] | None = None
        self.day: Any = None
        self.state = NEUTRAL

    def _local(self, ts: Any) -> Any:
        if self.tz and getattr(ts, "tzinfo", None):
            from zoneinfo import ZoneInfo
            return ts.astimezone(ZoneInfo(self.tz))
        return ts

    def on_bar_update(self, bar: Any) -> TradeSignal:
        ts = bar.ts
        at = self._local(ts)
        o, h, lo, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
        day = at.date() if hasattr(at, "date") else None
        new_day = day != self.day
        self.day = day
        prev = list(self.e)
        self.e = [ema_step(self.e[k], c, self.n[k]) for k in range(3)]
        e9, e15, e21 = self.e
        atr = self.atr.update(h, lo, c)
        self.bar_no += 1
        self.lows.append(lo)
        self.highs.append(h)
        if prev[0] is not None and (prev[0] - prev[2]) * (e9 - e21) < 0:
            self.crosses.append(self.bar_no)
        while self.crosses and self.crosses[0] <= self.bar_no - self.whipsaw_bars:
            self.crosses.popleft()
        meta = {"ema9": round(e9, 4), "ema15": round(e15, 4), "ema21": round(e21, 4),
                "atr14": None if atr is None else round(atr, 4)}
        sig = TradeSignal(timestamp=ts, metadata=meta)

        # ---- the alignment cycle (raw EMA order)
        order = 1 if e9 > e15 > e21 else -1 if e9 < e15 < e21 else 0
        cycle = BULL if order > 0 else BEAR if order < 0 else NEUTRAL
        if cycle != self.cycle:
            self.cycle, self.used = cycle, set()
        # ---- valid alignment: order + slopes + no chop
        valid, why = 0, ""
        if order and atr is not None and prev[2] is not None:
            slopes = (e9 - prev[0], e15 - prev[1], e21 - prev[2])
            why = chop_reason(order, e9, e15, e21, slopes, atr, len(self.crosses),
                              self.spread_atr, self.max_crosses)
            valid = 0 if why else order
        elif not order:
            why = "the 9 / 15 / 21 EMAs are out of order"
        self.state = SIDEWAYS if why else self.cycle
        was_valid, self.valid_prev = self.valid_prev, valid

        # ---- the open position
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
            sig.reason = "in a position"
            return sig

        if not valid:
            sig.reason = f"{SIDEWAYS}: {why}" if why else ""
            return sig
        d = valid
        rng = h - lo
        mode = ""
        if self.triggers in ("both", "breakout") and was_valid != d and BREAKOUT not in self.used:
            body_ok = (c > o and (c - lo) >= self.body * rng) if d > 0 else \
                      (c < o and (h - c) >= self.body * rng)
            if rng > 0 and body_ok:
                mode = BREAKOUT
        if not mode and self.triggers in ("both", "pullback") and was_valid == d \
                and PULLBACK not in self.used:
            if d > 0 and lo <= e15 and c >= e21 and c > e9 and c > o:
                mode = PULLBACK
            elif d < 0 and h >= e15 and c <= e21 and c < e9 and c < o:
                mode = PULLBACK
        if not mode:
            return sig
        if self.window and hasattr(at, "hour"):
            m = at.hour * 60 + at.minute
            if not (_hm(self.window[0]) <= m < _hm(self.window[1])):
                sig.reason = "outside the entry window"
                return sig
        if d > 0:
            stop = min(min(self.lows), e21) - self.stop_atr * atr
        else:
            stop = max(max(self.highs), e21) + self.stop_atr * atr
        risk = (c - stop) * d
        if risk <= 0:
            return sig
        self.used.add(mode)
        target = c + d * self.rr * risk
        sig.signal_type = "BUY" if d > 0 else "SELL"
        sig.trigger_mode = mode
        sig.entry_price, sig.stop_loss_price = round(c, 4), round(stop, 4)
        sig.take_profit_price, sig.risk_reward_ratio = round(target, 4), self.rr
        sig.reason = (f"{'ascending' if d > 0 else 'descending'} 9/15/21 fan-out, all sloping "
                      f"{'up' if d > 0 else 'down'}; {mode.lower().replace('_', ' ')}")
        sig.metadata = {**meta, "risk": round(risk, 4),
                        "stop_atr": round(risk / atr, 2) if atr else None}
        self.position = {"direction": d, "entry": c, "stop": stop, "target": target,
                         "last_close": c, "mode": mode}
        return sig

    def calculate_signals(self, history: list[Any]) -> list[TradeSignal]:
        return [self.on_bar_update(b) for b in history]

    def _exit(self, ts: Any, price: float, why: str, meta: dict[str, Any]) -> TradeSignal:
        p, self.position = self.position, None
        d = p["direction"]
        r = (price - p["entry"]) * d / abs(p["entry"] - p["stop"])
        return TradeSignal(timestamp=ts, signal_type="EXIT", trigger_mode=p["mode"],
                           entry_price=round(price, 4), reason=why,
                           metadata={**meta, "r": round(r, 3),
                                     "direction": "LONG" if d > 0 else "SHORT"})


def engine_from(cfg: Any, tz: str | None = None, **over: Any) -> SJK91521Engine:
    g = cfg.get
    p = f"{KEY}."
    kw = dict(fast=int(g(p + "fast", 9)), mid=int(g(p + "mid", 15)), slow=int(g(p + "slow", 21)),  # noqa: C408
              swing=int(g(p + "swing_lookback", 5)), stop_atr=float(g(p + "stop_atr", 0.20)),
              rr=float(g(p + "rr", 2.0)), spread_atr=float(g(p + "spread_atr", 0.35)),
              whipsaw_bars=int(g(p + "whipsaw_bars", 12)),
              max_crosses=int(g(p + "max_crosses", 2)),
              breakout_body=float(g(p + "breakout_body", 0.60)),
              triggers=str(g(p + "triggers", "both")), tz=tz,
              window=(str(g(p + "from", "09:45")), str(g(p + "to", "15:45"))),
              square_off=str(g("system.square_off_time", "15:15")))
    kw.update(over)
    return SJK91521Engine(**kw)


def detect_candles(bars: list[Any], cfg: Any, tz: str) -> dict[str, Any] | None:
    """A BUY / SELL on the LATEST closed 5m candle, for the analyst, or None."""
    if len(bars) < 30:
        return None
    eng = engine_from(cfg, tz)
    sig = None
    for b in bars:
        sig = eng.on_bar_update(b)
    if sig is None or sig.signal_type not in ("BUY", "SELL"):
        return None
    long = sig.signal_type == "BUY"
    m = sig.metadata or {}
    return {"direction": "LONG" if long else "SHORT", "entry": sig.entry_price,
            "stop": sig.stop_loss_price, "target": sig.take_profit_price,
            "kind": sig.trigger_mode, "ema_fast": m.get("ema9"), "ema_mid": m.get("ema15"),
            "ema_slow": m.get("ema21"), "ts": bars[-1].ts.isoformat(), "setup": SETUP_NAME,
            "note": f"{sig.reason}; stop {sig.stop_loss_price:,.2f} "
                    f"({m.get('stop_atr')} x ATR)"}


def engine_backtest(bars: list[Any], cfg: Any, tz: str, cost_pct: float = 0.0,
                    **over: Any) -> list[dict[str, Any]]:
    """The engine over historical candles; each trade tagged with its trigger
    mode (attribution) and its stop distance in ATR."""
    over.pop("warmup", None)
    over.pop("detector", None)
    eng = engine_from(cfg, tz, **over)
    trades: list[dict[str, Any]] = []
    open_: dict[str, Any] | None = None
    for b in bars:
        sig = eng.on_bar_update(b)
        if sig.signal_type in ("BUY", "SELL"):
            open_ = {"ts": b.ts.isoformat(),
                     "direction": "LONG" if sig.signal_type == "BUY" else "SHORT",
                     "entry": sig.entry_price, "stop": sig.stop_loss_price,
                     "target": sig.take_profit_price,
                     "risk": abs(sig.entry_price - sig.stop_loss_price),
                     "kind": sig.trigger_mode, "stop_atr": (sig.metadata or {}).get("stop_atr")}
        elif sig.signal_type == "EXIT" and open_ is not None:
            cost_r = cost_pct / 100.0 * open_["entry"] / open_["risk"] if open_["risk"] else 0.0
            outcome = {"stop": "STOP", "target": "TARGET"}.get(sig.reason, "SQUARE_OFF")
            trades.append({**open_, "exit": sig.entry_price, "exit_ts": b.ts.isoformat(),
                           "outcome": outcome,
                           "r": round(float((sig.metadata or {}).get("r", 0.0)) - cost_r, 3)})
            open_ = None
    return trades


def _minutes(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h) * 60 + int(m or 0)


def _local(ts: Any, tz: str) -> Any:
    from zoneinfo import ZoneInfo
    try:
        return ts.astimezone(ZoneInfo(tz)) if ts.tzinfo else ts
    except Exception:                                    # noqa: BLE001
        return ts


def session_start(bars: list[Any], tz: str, opens: str) -> int:
    """The index of the first bar of the latest session at or after `opens`
    (market time) — where this session's one trade per run counts from."""
    if not bars:
        return 0
    day = _local(bars[-1].ts, tz).date()
    start = _minutes(opens)
    for pos in range(len(bars) - 1, -1, -1):
        at = _local(bars[pos].ts, tz)
        if at.date() != day or at.hour * 60 + at.minute < start:
            return pos + 1
    return 0


# --------------------------------------------------------------------------- #
# 8. The backtest
# --------------------------------------------------------------------------- #
def backtest(bars: list[Any], cfg: Any, tz: str, square_off: str | None = None,
             warmup: int = 63, detector: Any = None) -> list[dict[str, Any]]:
    """Walk 5m candles bar by bar (no lookahead): one position at a time,
    entered on the signal bar's close, out at the stop, the target or the
    square-off (system.square_off_time), whichever first; a bar touching both
    is a stop (pessimistic). Returns one dict per trade with its R.

    `detector`: any detect_candles(bars, cfg, tz) — SJK 9-15-21's by default;
    SJK 50-200's (warmup 210) runs through the same walk."""
    detector = detector or detect_candles
    sq = _minutes(square_off or str(cfg.get("system.square_off_time", "15:15")))
    trades: list[dict[str, Any]] = []
    pos: dict[str, Any] | None = None
    for i in range(warmup, len(bars)):
        bar = bars[i]
        at = _local(bar.ts, tz)
        if pos is not None:
            long = pos["direction"] == "LONG"
            hit_stop = bar.low <= pos["stop"] if long else bar.high >= pos["stop"]
            hit_tgt = bar.high >= pos["target"] if long else bar.low <= pos["target"]
            late = at.date() != pos["day"] or at.hour * 60 + at.minute >= sq
            exit_px, why = None, ""
            if hit_stop:
                exit_px, why = pos["stop"], "STOP"
            elif hit_tgt:
                exit_px, why = pos["target"], "TARGET"
            elif late:
                exit_px, why = float(bars[i - 1].close), "SQUARE_OFF"
            if exit_px is not None:
                sign = 1 if long else -1
                r = (exit_px - pos["entry"]) * sign / pos["risk"]
                trades.append({**{k: v for k, v in pos.items() if k != "day"},
                               "exit": exit_px, "exit_ts": bar.ts.isoformat(),
                               "outcome": why, "r": round(r, 3)})
                pos = None
            else:
                continue
        found = detector(bars[max(0, i - 599):i + 1], cfg, tz)
        if found:
            pos = {"symbol": getattr(bar, "symbol", ""), "ts": bar.ts.isoformat(),
                   "day": at.date(), "direction": found["direction"],
                   "entry": found["entry"], "stop": found["stop"], "target": found["target"],
                   "risk": abs(found["entry"] - found["stop"]),
                   "kind": found.get("kind", found.get("setup", ""))}
    return trades


def summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Trades, win rate, average and total R, and the worst run in R."""
    rs = [float(t["r"]) for t in trades]
    if not rs:
        return {"trades": 0}
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    wins = [r for r in rs if r > 0]
    return {"trades": len(rs), "win_rate": round(len(wins) / len(rs) * 100, 1),
            "avg_r": round(sum(rs) / len(rs), 3), "total_r": round(sum(rs), 2),
            "max_drawdown_r": round(dd, 2)}
