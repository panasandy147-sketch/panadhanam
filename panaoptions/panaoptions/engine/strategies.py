"""The four entry strategies, each with its own session window and its own
invalidation level on the UNDERLYING.

Why four rather than one general rule: a breakout, a pullback and a reversal
at a level are not the same trade, they work at different times of day, and
lumping them together makes the journal unable to answer "which of these
actually pays". Every setup is tagged with the strategy that produced it.

Why each carries an underlying invalidation rather than a premium percentage:
a fixed -20% on the contract is at the mercy of an implied-volatility shift or
a wide spread, and says nothing about whether the trade was wrong. The level
below says exactly what "wrong" means for that entry.

They all avoid the 09:30-09:45 opening chop, where spreads are widest and the
first prints are noise.
"""
from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

import pandas as pd

from panaoptions.engine import contract_prefs, patterns
from panaoptions.engine import indicators as ta
from panaoptions.engine import levels as levels_mod
from panaoptions.logging import get_logger
from panaoptions.models import Candle, Direction, SessionLevels, Setup, SetupType

log = get_logger("strategies")


def _bar_time(df: pd.DataFrame) -> time:
    """The last bar's time of day, in the EXCHANGE's timezone.

    The feed returns UTC. Comparing a UTC stamp against a window written in
    New York time silently shifts every window by four or five hours, so a
    strategy that should run at 10:00 ET either never fires or fires in the
    middle of the night. The frame is localised before it gets here; this
    asserts the contract rather than trusting it.
    """
    stamp = df.index[-1]
    if stamp.tzinfo is not None and str(stamp.tzinfo) == "UTC":
        raise ValueError(
            "strategy windows must be evaluated in the exchange timezone — "
            "call localise() on the frame first")
    return stamp.time()


def localise(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Move a UTC frame onto the exchange's clock."""
    if df.empty:
        return df
    out = df.copy()
    index = pd.to_datetime(out.index, utc=True)
    out.index = index.tz_convert(ZoneInfo(tz))
    return out


def volume_unreported(snapshot) -> bool:
    """The live bar carries no volume (0 or missing): the data provider has
    not reported it yet, or — NSE indices on Yahoo — never does. That is
    "not measured", not "quiet", so the volume gates are bypassed rather than
    refusing with "volume 0 is below 1.5x the average"."""
    return not (snapshot.volume or 0)


def _volume_ok(snapshot, multiple: float) -> bool:
    if volume_unreported(snapshot):
        return True
    return snapshot.avg_volume > 0 and snapshot.volume >= snapshot.avg_volume * multiple


def _volume_note(snapshot, text: str) -> str:
    return ("volume not reported on the live bar (feed lag or an index) — the "
            "volume check is bypassed" if volume_unreported(snapshot) else text)


def _hm(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h) * 60 + int(m or 0)


def volume_multiple(cfg, base: float, when) -> float:
    """The RVOL a volume gate asks for at `when` (market time): between
    technical.midday_rvol.from and .to (10:30-14:00) a requirement above
    technical.midday_rvol.min (1.2x) is lowered to it — volume tapers
    naturally at midday, and 1.5x there refused sound setups."""
    mid = cfg.get("technical.midday_rvol") or {}
    if not mid or not mid.get("enabled", True) or when is None:
        return base
    floor = float(mid.get("min", 1.2))
    t = when.hour * 60 + when.minute
    if _hm(mid.get("from", "10:30")) <= t < _hm(mid.get("to", "14:00")) and base > floor:
        return floor
    return base


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    out = 100 - 100 / (1 + gain / loss.where(loss > 0))
    out = out.where(loss > 0, 100.0)                     # no losses: 100
    return out.where((gain > 0) | (loss > 0), 50.0)      # no movement: 50


def perdices_check(df5: pd.DataFrame, long: bool, cfg) -> tuple[str, str]:
    """Pau Perdices' pullback (2025 World Cup of Forex champion): with the
    trend, the pullback retraces fib_min..fib_max (38.2-50%) of the impulse
    before it, and RSI diverges at the pullback's extreme. ('', note) when it
    holds, else (why not, '')."""
    g = cfg.get
    look = int(g("strategies.vwap_ema_pullback.perdices.lookback_bars", 24))
    lo_f = float(g("strategies.vwap_ema_pullback.perdices.fib_min", 0.382))
    hi_f = float(g("strategies.vwap_ema_pullback.perdices.fib_max", 0.5))
    win = df5.iloc[-look:]
    if len(win) < 10:
        return "too few bars to measure the impulse", ""
    highs, lows = win["high"].to_numpy(), win["low"].to_numpy()
    if long:
        top = int(highs.argmax())
        if top < 2 or top >= len(win) - 1:
            return "no impulse high followed by a pullback", ""
        base, peak = float(lows[:top + 1].min()), float(highs[top])
        pull = float(lows[top:].min())
        depth = (peak - pull) / (peak - base) if peak > base else 0.0
    else:
        bot = int(lows.argmin())
        if bot < 2 or bot >= len(win) - 1:
            return "no impulse low followed by a pullback", ""
        base, peak = float(highs[:bot + 1].max()), float(lows[bot])
        pull = float(highs[bot:].max())
        depth = (pull - peak) / (base - peak) if base > peak else 0.0
    if not lo_f <= depth <= hi_f:
        return (f"the pullback retraced {depth:.0%} of the impulse — Perdices "
                f"wants {lo_f:.0%}-{hi_f:.0%}"), ""
    note = f"pullback retraced {depth:.0%} of the impulse {base:.2f}-{peak:.2f}"
    if bool(g("strategies.vwap_ema_pullback.perdices.rsi_divergence", True)):
        r = rsi(df5["close"], int(g("strategies.vwap_ema_pullback.perdices.rsi_period", 14)))
        half = max(3, look // 4)
        recent, before = df5.iloc[-half:], df5.iloc[-look:-half]
        r_recent, r_before = r.iloc[-half:], r.iloc[-look:-half]
        if long:
            p_now, p_then = recent["low"].min(), before["low"].min()
            q_now, q_then = r_recent[recent["low"].idxmin()], r_before[before["low"].idxmin()]
            # regular (lower low, higher RSI) or hidden (higher low, lower RSI)
            div = (p_now < p_then and q_now > q_then) or (p_now > p_then and q_now < q_then)
        else:
            p_now, p_then = recent["high"].max(), before["high"].max()
            q_now, q_then = r_recent[recent["high"].idxmax()], r_before[before["high"].idxmax()]
            div = (p_now > p_then and q_now < q_then) or (p_now < p_then and q_now > q_then)
        if not div:
            return "no RSI divergence at the pullback", ""
        note += f"; RSI divergence ({q_then:.0f} -> {q_now:.0f})"
    return "", note


def td_setup(close: pd.Series) -> tuple[int, int]:
    """DeMark TD Setup counts on the latest bar: (buy, sell). A buy count
    rises while each close is below the close 4 bars earlier; 9 is a
    completed buy setup (selling exhaustion), and the mirror for a sell."""
    buy = sell = 0
    c = close.to_numpy()
    for i in range(4, len(c)):
        buy = buy + 1 if c[i] < c[i - 4] else 0
        sell = sell + 1 if c[i] > c[i - 4] else 0
    return buy, sell


def demark_check(df5: pd.DataFrame, long: bool, cfg) -> tuple[str, str]:
    """Kevin McCormick (2021 World Cup futures champion) times reversals with
    DeMark's Sequential: a reversal long only after a completed TD buy setup
    (9 closes each below the close 4 earlier) within `within_bars`, a short
    only after a completed sell setup."""
    within = int(cfg.get("demark.within_bars", 3))
    need = int(cfg.get("demark.count", 9))
    close = df5["close"]
    best = 0
    for k in range(within):
        cut = close.iloc[: len(close) - k] if k else close
        b, s = td_setup(cut.iloc[-60:])
        best = max(best, b if long else s)
    if best >= need:
        return "", f"DeMark TD {'buy' if long else 'sell'} setup {best} — the prior move is exhausted"
    return (f"no completed DeMark TD {'buy' if long else 'sell'} setup ({best} of "
            f"{need}) — the move it reverses is not exhausted"), ""


def _base(symbol: str, df: pd.DataFrame, strategy: SetupType) -> Setup:
    return Setup(symbol=symbol, ts=df.index[-1].to_pydatetime(), strategy=strategy)


# --------------------------------------------------------------------------- #
class Strategy:
    """One entry rule. `evaluate` returns a Setup, triggered or not."""

    name: SetupType = SetupType.OTHER
    window: tuple[str, str] = ("09:35", "10:30")

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    # -- session window ---------------------------------------------------
    @property
    def opens(self) -> time:
        return self._parse(self.cfg.get(f"strategies.{self.key}.from", self.window[0]))

    @property
    def closes(self) -> time:
        return self._parse(self.cfg.get(f"strategies.{self.key}.to", self.window[1]))

    @property
    def key(self) -> str:
        return self.name.name.lower()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get(f"strategies.{self.key}.enabled", True))

    @staticmethod
    def _parse(value: str) -> time:
        hour, _, minute = str(value).partition(":")
        return time(int(hour), int(minute or 0))

    def in_window(self, df: pd.DataFrame) -> bool:
        return self.opens <= _bar_time(df) < self.closes

    def evaluate(self, symbol: str, df5: pd.DataFrame, df15: pd.DataFrame,
                 levels: SessionLevels) -> Setup:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
class OpeningRangeBreakout(Strategy):
    """Strategy 1 — 15-minute opening range breakout, confirmed by VWAP.

    A close beyond the range, with the trend and volume behind it. The
    invalidation is the range boundary itself: back inside and the breakout
    has failed, whatever the option is worth.
    """

    name = SetupType.ORB_VWAP
    window = ("09:45", "11:00")      # the range is not final until 09:45

    def evaluate(self, symbol, df5, df15, levels) -> Setup:
        setup = _base(symbol, df5, self.name)
        if not levels.has_opening_range:
            setup.blockers.append("the 15m opening range has not finished forming")
            return setup

        snapshot = ta.compute(df5, self.cfg)
        multiple = volume_multiple(
            self.cfg, float(self.cfg.get("strategies.orb_vwap.volume_multiple", 1.5)),
            df5.index[-1])
        bar = df5.iloc[-1]
        high, low = levels.opening_range_high, levels.opening_range_low

        long_break = (bar["close"] > high and snapshot.close > snapshot.vwap
                      and snapshot.ema_fast > snapshot.ema_slow)
        short_break = (bar["close"] < low and snapshot.close < snapshot.vwap
                       and snapshot.ema_fast < snapshot.ema_slow)

        if not (long_break or short_break):
            setup.blockers.append(
                f"no close beyond the opening range ({low:.2f}-{high:.2f})")
            return setup

        setup.direction = Direction.LONG if long_break else Direction.SHORT
        setup.indicators = snapshot
        setup.pattern = "Opening range breakout"
        side = "above" if long_break else "below"
        setup.confirmations = [
            f"5m close {side} the 15m opening range "
            f"({bar['close']:.2f} vs {high if long_break else low:.2f})",
            f"price {side} VWAP ({snapshot.close:.2f} vs {snapshot.vwap:.2f})",
            f"9 EMA {'>' if long_break else '<'} 21 EMA "
            f"({snapshot.ema_fast:.2f} vs {snapshot.ema_slow:.2f})",
        ]

        if _volume_ok(snapshot, multiple):
            setup.confirmations.append(_volume_note(
                snapshot, f"volume {snapshot.volume:,.0f} is {snapshot.rvol:.1f}x the average"))
        else:
            setup.blockers.append(
                f"volume {snapshot.volume:,.0f} is below {multiple}x the "
                f"{snapshot.avg_volume:,.0f} average — the break is unbacked")

        # Back inside the range and the breakout has failed.
        setup.underlying_support = high if long_break else low
        setup.invalidation_note = (
            f"a 5m close back inside the opening range "
            f"({'below' if long_break else 'above'} {setup.underlying_support:.2f})")
        # 1.5x the range height, measured from the boundary.
        extension = float(self.cfg.get("strategies.orb_vwap.target_range_multiple", 1.5))
        setup.underlying_target = (high + levels.range_height * extension
                                   if long_break
                                   else low - levels.range_height * extension)
        setup.trend_aligned = True
        return setup


# --------------------------------------------------------------------------- #
class VwapEmaPullback(Strategy):
    """Strategy 2 — a pullback into the 9 EMA or VWAP inside an established
    trend, entered on the rejection candle rather than chased at the highs.

    The 15m chart sets the trend (price > 20 EMA > 50 EMA, or the inverse);
    the 5m chart provides the entry. Invalidation is a 5m close on the wrong
    side of VWAP — the line the whole setup is leaning on.
    """

    name = SetupType.VWAP_EMA_PULLBACK
    window = ("10:00", "13:30")

    def evaluate(self, symbol, df5, df15, levels) -> Setup:
        setup = _base(symbol, df5, self.name)
        if len(df15) < 50:
            setup.blockers.append(
                f"only {len(df15)} 15m bars — the 50 EMA is not meaningful yet")
            return setup

        trend = self._trend(df15)
        if trend == 0:
            setup.blockers.append(
                "the 15m chart is not in a stacked trend (price > 20 EMA > 50 EMA)")
            return setup

        snapshot = ta.compute(df5, self.cfg)
        prev, bar = df5.iloc[-2], df5.iloc[-1]
        long_side = trend > 0

        # Did price actually come back to the line, rather than never leaving?
        band = float(self.cfg.get("strategies.vwap_ema_pullback.touch_atr", 0.35))
        reach = max(snapshot.atr * band, snapshot.close * 0.0005)
        touched = (min(bar["low"], prev["low"]) <= max(snapshot.ema_fast,
                                                       snapshot.vwap) + reach
                   if long_side else
                   max(bar["high"], prev["high"]) >= min(snapshot.ema_fast,
                                                         snapshot.vwap) - reach)
        if not touched:
            setup.blockers.append("price has not pulled back to the 9 EMA or VWAP")
            return setup

        rejection = (patterns.bullish(df5) if long_side else patterns.bearish(df5))
        broke_prior = (bar["close"] > prev["high"] if long_side
                       else bar["close"] < prev["low"])
        if not rejection:
            setup.blockers.append("no rejection candle at the line")
            return setup
        if not broke_prior:
            setup.blockers.append(
                f"the rejection candle did not take out the previous candle's "
                f"{'high' if long_side else 'low'}")
            return setup

        if bool(self.cfg.get("strategies.vwap_ema_pullback.perdices.enabled", False)):
            why, note = perdices_check(df5, long_side, self.cfg)
            if why:
                setup.blockers.append(why)
                return setup
        else:
            note = ""

        setup.direction = Direction.LONG if long_side else Direction.SHORT
        setup.indicators = snapshot
        setup.pattern = rejection
        setup.trend_aligned = True
        setup.confirmations = [
            f"15m trend {'up' if long_side else 'down'} "
            f"(price {'>' if long_side else '<'} 20 EMA {'>' if long_side else '<'} 50 EMA)",
            f"pullback into the {'9 EMA' if abs(snapshot.close - snapshot.ema_fast) < abs(snapshot.close - snapshot.vwap) else 'VWAP'}",
            f"{rejection} taking out the previous candle's "
            f"{'high' if long_side else 'low'}",
        ] + ([note] if note else [])
        if _volume_ok(snapshot, volume_multiple(self.cfg, float(self.cfg.get(
                "strategies.vwap_ema_pullback.volume_multiple", 1.0)), df5.index[-1])):
            setup.confirmations.append(_volume_note(snapshot,
                                                    f"volume {snapshot.rvol:.1f}x average"))

        setup.underlying_support = snapshot.vwap
        setup.invalidation_note = (
            f"a 5m close {'below' if long_side else 'above'} VWAP "
            f"({snapshot.vwap:.2f})")
        return setup

    def _trend(self, df15: pd.DataFrame) -> int:
        close = df15["close"]
        price = float(close.iloc[-1])
        ema20 = float(ta.ema(close, 20).iloc[-1])
        ema50 = float(ta.ema(close, 50).iloc[-1])
        if price > ema20 > ema50:
            return 1
        if price < ema20 < ema50:
            return -1
        return 0


# --------------------------------------------------------------------------- #
class LiquiditySweepReversal(Strategy):
    """Strategy 3 — a failed breakout of the pre-market extreme.

    Price pokes through the pre-market low, takes the stops resting under it,
    then reclaims the level on the next candle and crosses VWAP. The trade is
    that the breakout buyers are trapped. Invalidation is the extreme of the
    sweep itself: back through it and the reclaim has failed too.
    """

    name = SetupType.LIQUIDITY_SWEEP
    window = ("09:45", "12:00")

    def evaluate(self, symbol, df5, df15, levels) -> Setup:
        setup = _base(symbol, df5, self.name)
        if not (levels.premarket_high and levels.premarket_low):
            setup.blockers.append("no pre-market high and low for this session")
            return setup

        snapshot = ta.compute(df5, self.cfg)
        sweep, bar = df5.iloc[-2], df5.iloc[-1]
        multiple = volume_multiple(
            self.cfg, float(self.cfg.get("strategies.liquidity_sweep.volume_multiple", 1.5)),
            df5.index[-1])

        swept_low = (sweep["low"] < levels.premarket_low
                     and bar["close"] > levels.premarket_low
                     and snapshot.close > snapshot.vwap)
        swept_high = (sweep["high"] > levels.premarket_high
                      and bar["close"] < levels.premarket_high
                      and snapshot.close < snapshot.vwap)

        if not (swept_low or swept_high):
            setup.blockers.append(
                f"no sweep-and-reclaim of the pre-market range "
                f"({levels.premarket_low:.2f}-{levels.premarket_high:.2f})")
            return setup

        setup.direction = Direction.LONG if swept_low else Direction.SHORT
        setup.indicators = snapshot
        setup.pattern = "Liquidity sweep reversal"
        level = levels.premarket_low if swept_low else levels.premarket_high
        setup.confirmations = [
            f"swept the pre-market {'low' if swept_low else 'high'} "
            f"({level:.2f}) and reclaimed it on the next 5m close",
            f"crossed {'above' if swept_low else 'below'} VWAP "
            f"({snapshot.close:.2f} vs {snapshot.vwap:.2f})",
        ]
        if _volume_ok(snapshot, multiple):
            setup.confirmations.append(_volume_note(
                snapshot, f"volume {snapshot.rvol:.1f}x average on the reclaim"))
        else:
            setup.blockers.append(
                f"the reclaim came on {snapshot.rvol:.1f}x volume, below "
                f"{multiple}x — a sweep without participation is not a reversal")

        setup.underlying_support = float(sweep["low"] if swept_low else sweep["high"])
        setup.invalidation_note = (
            f"a 5m close back through the sweep wick "
            f"({setup.underlying_support:.2f})")
        setup.underlying_target = snapshot.vwap if not swept_low else (
            levels.premarket_high or snapshot.vwap)
        setup.trend_aligned = True
        return setup


# --------------------------------------------------------------------------- #
ALL: list[type[Strategy]] = [
    OpeningRangeBreakout, VwapEmaPullback, LiquiditySweepReversal,
]


NO_SWEEP = "No institutional sweep of previous day extremes."


def pd_gate_strategies(cfg) -> set[str]:
    """The candlestick strategies behind the previous-day go/no-go."""
    if not bool(cfg.get("fno.go_no_go.enabled", True)):
        return set()
    return {str(s).lower() for s in (cfg.get("fno.go_no_go.strategies")
                                     or ["candlestick_at_level", "liquidity_sweep"])}


def pd_sweep_now(df5: pd.DataFrame, levels: SessionLevels, cfg) -> dict | None:
    """A clean sweep of the PDL (calls) or PDH (puts) on today's 5m tape,
    closed back inside — the same rule as the PD Liquidity Sweep strategy."""
    key = "strategies.pd_liquidity_sweep"
    if not (levels.previous_high and levels.previous_low):
        return None
    return sweep_of_previous_day(df5, levels, float(cfg.get(f"{key}.proximity_pct", 0.25)),
                                 int(cfg.get(f"{key}.lookback_bars", 3)),
                                 bool(cfg.get(f"{key}.first_test_only", True)))


def evaluate_all(symbol: str, candles: list[Candle], levels: SessionLevels,
                 cfg) -> tuple[Setup | None, list[Setup]]:
    """Run every enabled, in-window strategy. Returns (winner, all attempts).

    The first strategy to trigger wins. They are ordered by how specific they
    are, and in practice their windows barely overlap, so a contest is rare.
    Every attempt is returned regardless, because the ones that did NOT fire
    are what tell you whether a rule is selective or simply impossible.
    """
    df5 = localise(ta.to_frame(candles),
                   str(cfg.get("session.timezone", "America/New_York")))
    if len(df5) < 21:
        return None, []

    df15 = ta.resample(df5, "15min")
    attempts: list[Setup] = []
    winner: Setup | None = None
    gated = pd_gate_strategies(cfg)
    sweep: dict | None | bool = False           # not looked yet

    for cls in ALL:
        strategy = cls(cfg)
        if not strategy.enabled or not strategy.in_window(df5):
            continue
        if strategy.key in gated:
            # Previous-day go/no-go, BEFORE the pattern is even read: calls
            # only after a clean sweep of the PDL, puts only after the PDH.
            if sweep is False:
                sweep = pd_sweep_now(df5, levels, cfg)
            if sweep is None:
                skipped = _base(symbol, df5, strategy.name)
                skipped.blockers.append(NO_SWEEP)
                attempts.append(skipped)
                continue
        setup = strategy.evaluate(symbol, df5, df15, levels)
        if (setup.triggered and bool(cfg.get("demark.enabled", False))
                and strategy.key in set(cfg.get("demark.strategies") or [])):
            why, note = demark_check(df5, setup.direction is Direction.LONG, cfg)
            if why:
                setup.blockers.append(why)
            else:
                setup.confirmations.append(note)
        if strategy.key in gated and setup.triggered and sweep:
            want = Direction.LONG if sweep["direction"] > 0 else Direction.SHORT
            if setup.direction is not want:
                setup.blockers.append(
                    f"{NO_SWEEP} ({'LONG CALL needs the PDL' if setup.direction is Direction.LONG else 'LONG PUT needs the PDH'} "
                    f"swept; only the {'PDL' if want is Direction.LONG else 'PDH'} was)")
        attempts.append(setup)
        if winner is None and setup.triggered:
            winner = setup
    return winner, attempts


# --------------------------------------------------------------------------- #
class CandlestickAtLevel(Strategy):
    """Strategy 4 — a reversal candle, but only where one can mean something.

    Seven patterns: Hammer, Bullish Engulfing, Morning Star, Tweezer Bottom
    for calls; Shooting Star, Bearish Engulfing, Evening Star, Tweezer Top for
    puts.

    The pattern is the smaller half of the rule. The larger half is WHERE:
    a hammer in the middle of a range is a bar with a wick, and trading it is
    how people conclude candlesticks do not work. It must print at a level the
    market has already turned at — a swing high or low, the pre-market extreme,
    the opening range boundary, yesterday's close, VWAP or a moving average —
    and `nearest_level` measures that in ATR so "at the level" means the same
    on a quiet stock and a volatile one.

    The pattern is also not the entry. Price must take out the trigger (the
    high of a hammer, the low of a shooting star) before anything is bought,
    and the stop is the structure that would prove it wrong.

    Each pattern asks for its own contract. A hammer wants delta near the
    money because it is a sharp reversal off a level; a morning star is a
    slower structural turn and tolerates less. Those bands come from the
    config, per pattern.
    """

    name = SetupType.CANDLESTICK_AT_LEVEL
    window = ("09:45", "15:00")

    # Which contract each pattern asks for is a derivatives question and lives
    # in engine/contract_prefs.py. These stay as thin lookups so the setup can
    # carry the request to the contract picker; the strategy itself never
    # reads a price, a balance or a chain.
    @staticmethod
    def _key(pattern: str) -> str:
        return contract_prefs.key(pattern)

    def delta_band(self, pattern: str) -> tuple[float, float]:
        return contract_prefs.delta_band(self.cfg, pattern)

    def dte_window(self, pattern: str) -> tuple[int, int]:
        return contract_prefs.dte_window(self.cfg, pattern)

    def claimed_accuracy(self, pattern: str) -> float:
        """The win rate this pattern is published as having, or 0 — a claim
        the journal holds against what it actually does on this desk."""
        return contract_prefs.claimed_accuracy(self.cfg, pattern)

    def evaluate(self, symbol, df5, df15, levels) -> Setup:
        setup = _base(symbol, df5, self.name)
        timeframe = str(self.cfg.get(
            "strategies.candlestick_at_level.timeframe", "15m"))
        # Patterns pay on the higher timeframes. A 5m hammer is mostly noise,
        # which is why the default reads the 15m chart and enters on the 5m.
        frame = df15 if timeframe == "15m" and df15 is not None and len(df15) >= 20 else df5
        if len(frame) < 20:
            setup.blockers.append(
                f"only {len(frame)} {timeframe} bars — not enough to place a level")
            return setup

        # The entry is a break of the pattern's trigger, which happens on a
        # LATER candle — so the pattern is allowed to be a bar or two back.
        lookback = int(self.cfg.get(
            "strategies.candlestick_at_level.trigger_within_bars", 2))
        # A profile can narrow the patterns: a same-day desk has no time for
        # structures that take sessions to play out.
        allowed_cfg = self.cfg.get("strategies.candlestick_at_level.allowed_patterns")
        allowed = set(allowed_cfg) if allowed_cfg else None
        candidates_found = patterns.detect_recent_all(frame, within=lookback,
                                                      allowed=allowed)
        if not candidates_found:
            setup.blockers.append(
                f"no reversal pattern on the last {lookback + 1} "
                f"{timeframe} closes")
            return setup

        snapshot = ta.compute(df5, self.cfg)
        setup.indicators = snapshot

        # --- what it interrupted ------------------------------------------ #
        # Most of these patterns are defined by their context: three long red
        # candles after a rally is distribution, and the same three in the
        # middle of a range is noise with a story attached. When the newest,
        # strongest pattern fails its context, the next one on the same bars
        # is tried — a double rejection at the high is still one even where a
        # dark cloud cover without its uptrend is not.
        run_in = int(self.cfg.get(
            "strategies.candlestick_at_level.trend_lookback", 10))
        moving_averages = {"20 EMA": float(ta.ema(frame["close"], 20).iloc[-1]),
                           "50 EMA": float(ta.ema(frame["close"], 50).iloc[-1])
                           if len(frame) >= 50 else 0.0}
        candidates = levels_mod.key_levels(frame, levels, snapshot.vwap,
                                           moving_averages)
        tolerance = float(self.cfg.get(
            "strategies.candlestick_at_level.level_tolerance_atr", 0.5))
        last_price = snapshot.close

        def judge(found, bars_ago):
            """(level, pattern_bar, "") when this pattern is actionable now,
            else (None, None, why not)."""
            if found.requires_trend:
                trend = patterns.prior_trend(
                    frame, before=bars_ago + found.bars, lookback=run_in)
                if trend != found.requires_trend:
                    wanted = "a downtrend" if found.requires_trend < 0 else "an uptrend"
                    saw = ("an uptrend" if trend > 0
                           else "a downtrend" if trend < 0 else "no clear trend")
                    return None, None, (f"{found.name} needs {wanted} to interrupt and "
                                        f"the last {run_in} {timeframe} bars show {saw}")
            # --- the location gate: measured against the PATTERN's own
            # extreme, not the latest bar's — the pattern is what formed there.
            pattern_bar = frame.iloc[len(frame) - 1 - bars_ago]
            anchor = float(pattern_bar["low"] if found.bullish else pattern_bar["high"])
            level = levels_mod.nearest_level(anchor, candidates, snapshot.atr, tolerance)
            if level is None:
                return None, None, (f"{found.name} printed, but not at a level — a "
                                    f"reversal candle in the middle of a range is a "
                                    f"bar with a wick")
            wanted = "support" if found.bullish else "resistance"
            if level.kind != wanted:
                return None, None, (f"{found.name} is at {level.source} "
                                    f"({level.price:.2f}), which is {level.kind}, "
                                    f"not {wanted}")
            # --- the entry trigger
            triggered = (last_price > found.trigger if found.bullish
                         else last_price < found.trigger)
            if not triggered:
                return None, None, (f"{found.name} at {level.source} — waiting for "
                                    f"price to {'break above' if found.bullish else 'break below'} "
                                    f"{found.trigger:.2f} (now {last_price:.2f})")
            return level, pattern_bar, ""

        # Newest and strongest first; when one fails its context, its level or
        # its trigger, the next pattern on the same bars is judged — a tweezer
        # top that has triggered is a trade even while a fresher evening star
        # on the next bar is still waiting for its own break.
        first_refusal = ""
        chosen = None
        for found, bars_ago in candidates_found:
            level, pattern_bar, why = judge(found, bars_ago)
            if level is not None:
                chosen = (found, bars_ago, level, pattern_bar)
                break
            first_refusal = first_refusal or why
        if chosen is None:
            setup.blockers.append(first_refusal)
            return setup
        found, bars_ago, level, pattern_bar = chosen

        # --- confirmed ----------------------------------------------------- #
        # A single-wick rejection that actually PIERCED the level and closed
        # back inside is not the same event as one that stopped at it: the
        # stops resting beyond the level were taken first, and the trade is
        # that whoever was filled there is now offside. It gets its own name
        # and its own contract rather than being logged as a plain hammer.
        name = found.name
        swept = False
        if found.name in {"Hammer", "Shooting Star"}:
            swept = (float(pattern_bar["low"]) < level.price
                     <= float(pattern_bar["close"])) if found.bullish else (
                float(pattern_bar["high"]) > level.price
                >= float(pattern_bar["close"]))
            if swept:
                name = "Liquidity Sweep Rejection"

        band = self.delta_band(name)
        min_dte, max_dte = self.dte_window(name)
        setup.direction = Direction.LONG if found.bullish else Direction.SHORT
        setup.pattern = name
        setup.trend_aligned = True
        setup.entry_trigger = found.trigger
        setup.key_level = level.price
        setup.key_level_source = level.source
        setup.delta_band = band
        setup.min_dte_override = min_dte
        setup.max_dte_override = max_dte
        setup.claimed_accuracy = self.claimed_accuracy(name)
        setup.underlying_support = found.invalidation
        setup.invalidation_note = (
            f"a close back through {found.invalidation:.2f} "
            f"({'below' if found.bullish else 'above'} the "
            f"{found.name.lower()})")
        if swept:
            setup.invalidation_note = (
                f"a close back through {found.invalidation:.2f}, beyond the "
                f"tip of the sweep wick")

        right = "CALL" if found.bullish else "PUT"
        setup.confirmations = [
            f"{name} on the {timeframe} close"
            + (f", {bars_ago} bar{'s' if bars_ago > 1 else ''} ago"
               if bars_ago else ""),
            (f"wick swept {level.source} ({level.price:.2f}) and closed back "
             f"inside — the stops beyond it were taken first"
             if swept else
             f"at {level.source} ({level.price:.2f}) — {level.kind}"),
            f"price took out {found.trigger:.2f}",
        ]
        needed = volume_multiple(self.cfg, float(self.cfg.get(
            "strategies.candlestick_at_level.volume_multiple", 1.0)), df5.index[-1])
        if _volume_ok(snapshot, needed):
            setup.confirmations.append(_volume_note(
                snapshot, f"volume {snapshot.rvol:.1f}x average — institutional "
                          f"commitment, not a thin wick"))
        elif needed > 1.0:
            setup.blockers.append(
                f"volume {snapshot.rvol:.1f}x is below {needed}x — "
                f"a pattern without participation is a false break waiting "
                f"to happen")

        # The case for the trade, in the order somebody would read it.
        setup.reasoning = [
            f"**Buy a {right}** on {symbol}.",
            f"**The pattern.** {name} completed on the {timeframe} "
            f"chart. {found.note}",
            (f"**Why here.** The wick pushed through {level.source} "
             f"({level.price:.2f}) — taking the stops resting beyond it — and "
             f"the candle closed back inside. The trade is that whoever got "
             f"filled out there is now offside."
             if swept else
             f"**Why here.** It formed at {level.source} ({level.price:.2f}), "
             f"a {level.kind} level the market has already turned at. The "
             f"same candle mid-range would be ignored."),
            f"**The trigger.** Price took out {found.trigger:.2f}, which is "
            f"what turns a pattern into an entry.",
            f"**What kills it.** A close back through "
            f"{found.invalidation:.2f}. The option is sold on that, whatever "
            f"the premium is doing.",
            f"**The contract.** {band[0]:.2f}–{band[1]:.2f} delta, "
            f"{setup.min_dte_override}–{setup.max_dte_override} days out, so "
            f"theta does not eat the move before it happens.",
        ]
        if found.requires_trend:
            setup.reasoning.insert(3, (
                "**What it interrupted.** "
                + ("A run of selling into the level, which is the only context "
                   "this pattern means anything in."
                   if found.requires_trend < 0 else
                   "A run of buying into the level — this is distribution, "
                   "not a dip.")))
        if setup.claimed_accuracy:
            setup.reasoning.append(
                f"**The published number.** This pattern is cited at "
                f"~{setup.claimed_accuracy:.0%} on DAILY bars in somebody "
                f"else's study. That is a hypothesis about this 15m tape, not "
                f"a result — the journal tracks what it actually does here.")
        return setup


ALL.append(CandlestickAtLevel)


# --------------------------------------------------------------------------- #
def sweep_of_previous_day(df5: pd.DataFrame, levels: SessionLevels, band_pct: float,
                          lookback: int = 3, first_test: bool = True) -> dict | None:
    """The two-candle Previous Day Liquidity Sweep, if the tape shows one.

    LONG:  a candle's low sweeps BELOW the previous-day low (PDL), no more than
           `band_pct` % beyond it, and the IMMEDIATELY NEXT candle closes back
           inside yesterday's range (PDL < close < PDH).
    SHORT: a candle's high sweeps ABOVE the previous-day high (PDH) within
           `band_pct` %, and the next candle closes back inside.
    The sweep candle is one of the last `lookback` completed bars, the reclaim
    the one after it. Returns {"direction", "level", "wick", "sweep_i", ...}.

    `first_test`: the sweep must be the FIRST time today's tape reached beyond
    that level. The stops resting there are run once; later pokes through the
    same level are price chopping around it, not a fresh trap (the backtest
    showed the same PDH "swept" up to six times a day, mostly losers).
    """
    pdh, pdl = float(levels.previous_high or 0), float(levels.previous_low or 0)
    if not pdh or not pdl or pdh <= pdl or len(df5) < 2:
        return None
    today = df5[df5.index.date == df5.index[-1].date()]
    n = len(today)
    for back in range(1, min(lookback, n - 1) + 1):
        i = n - 1 - back                              # the sweep candle
        sweep, nxt = today.iloc[i], today.iloc[i + 1]
        close = float(nxt["close"])
        inside = pdl < close < pdh
        low, high = float(sweep["low"]), float(sweep["high"])
        before = today.iloc[:i]
        fresh_low = not first_test or not (before["low"] < pdl).any()
        fresh_high = not first_test or not (before["high"] > pdh).any()
        if low < pdl and (pdl - low) / pdl * 100 <= band_pct and inside and fresh_low:
            return {"direction": 1, "level": pdl, "wick": low, "reclaim": close,
                    "sweep_ts": today.index[i], "bars_after": back - 1,
                    "tweezer": abs(float(nxt["low"]) - low) <= max(pdl * 0.0015, 0.0),
                    "swing": float(nxt["low"]) >= low
                    and all(float(today.iloc[j]["low"]) >= low for j in range(max(0, i - 2), i))}
        if high > pdh and (high - pdh) / pdh * 100 <= band_pct and inside and fresh_high:
            return {"direction": -1, "level": pdh, "wick": high, "reclaim": close,
                    "sweep_ts": today.index[i], "bars_after": back - 1,
                    "tweezer": abs(float(nxt["high"]) - high) <= max(pdh * 0.0015, 0.0),
                    "swing": float(nxt["high"]) <= high
                    and all(float(today.iloc[j]["high"]) <= high for j in range(max(0, i - 2), i))}
    return None


class PdLiquiditySweep(Strategy):
    """The Previous Day Liquidity Sweep (failed breakout) — calls and puts.

    Stops rest just beyond yesterday's high and low. When a 5-minute candle
    runs them — by no more than `proximity_pct` (0.25%) — and the very next
    candle closes back inside yesterday's range, the breakout traders are
    trapped and their exits fuel the move back:

        LONG_CALL  a Tweezer Bottom / swing low that swept the PDL
        LONG_PUT   a Tweezer Top / swing high that swept the PDH

    Stop: the sweep candle's extreme wick, `stop_ticks` beyond it. Target: the
    day's VWAP or `min_reward_risk` (3R) from the entry, whichever is further —
    so the trade is 1:3 or better by construction, and the risk desk checks it
    again. Rising call (PDL) / put (PDH) open interest is required by the F&O
    confluence filter (fno.confluence).
    """

    name = SetupType.PD_LIQUIDITY_SWEEP
    window = ("09:45", "15:00")

    def evaluate(self, symbol, df5, df15, levels) -> Setup:
        setup = _base(symbol, df5, self.name)
        key = "strategies.pd_liquidity_sweep"
        if not (levels.previous_high and levels.previous_low):
            setup.blockers.append("no previous-day high and low on the tape")
            return setup
        band = float(self.cfg.get(f"{key}.proximity_pct", 0.25))
        found = sweep_of_previous_day(df5, levels, band,
                                      int(self.cfg.get(f"{key}.lookback_bars", 3)),
                                      bool(self.cfg.get(f"{key}.first_test_only", True)))
        if found is None:
            setup.blockers.append(
                f"no sweep of the previous-day high {levels.previous_high:.2f} / low "
                f"{levels.previous_low:.2f} within {band:g}% with the next candle "
                f"closing back inside"
                + (" (first test of the level today only)"
                   if self.cfg.get(f"{key}.first_test_only", True) else ""))
            return setup
        long = found["direction"] > 0
        shape = ("Tweezer Bottom" if long else "Tweezer Top") if found["tweezer"] else (
            "Swing Low" if long else "Swing High") if found["swing"] else ""
        if not shape and bool(self.cfg.get(f"{key}.require_pattern", True)):
            setup.blockers.append(
                f"swept the previous-day {'low' if long else 'high'} {found['level']:.2f} "
                f"and closed back inside, but the candles are neither a tweezer nor a "
                f"swing {'low' if long else 'high'}")
            return setup

        snapshot = ta.compute(df5, self.cfg)
        setup.indicators = snapshot
        tick = float(self.cfg.get(f"{key}.tick", 0.01))
        ticks = int(self.cfg.get(f"{key}.stop_ticks", 2))
        stop = found["wick"] - ticks * tick if long else found["wick"] + ticks * tick
        entry = float(found["reclaim"]) if found["bars_after"] == 0 else snapshot.close
        risk = abs(entry - stop)
        if risk <= 0 or (long and entry <= stop) or (not long and entry >= stop):
            setup.blockers.append("price is back through the sweep wick — the trap failed")
            return setup
        need = float(self.cfg.get(f"{key}.min_reward_risk", 3.0))
        three_r = entry + need * risk if long else entry - need * risk
        vwap = float(snapshot.vwap or 0.0)
        vwap_r = ((vwap - entry) if long else (entry - vwap)) / risk if vwap else 0.0
        target = vwap if vwap_r >= need else three_r

        what = "low (PDL)" if long else "high (PDH)"
        setup.direction = Direction.LONG if long else Direction.SHORT
        setup.pattern = f"{'PDL' if long else 'PDH'} Sweep — {shape or 'reclaim'}"
        setup.entry_trigger = entry
        setup.key_level = found["level"]
        setup.key_level_source = f"previous day {'low' if long else 'high'}"
        setup.underlying_support = round(stop, 4)
        setup.underlying_target = round(target, 4)
        setup.trend_aligned = True
        setup.delta_band = contract_prefs.delta_band(self.cfg, setup.pattern)
        setup.invalidation_note = (f"a trade through the sweep wick {found['wick']:.2f} "
                                   f"({ticks} ticks beyond: {stop:.2f})")
        depth = abs(found["wick"] - found["level"]) / found["level"] * 100
        setup.confirmations = [
            f"swept the previous-day {what} {found['level']:.2f} by {depth:.2f}% "
            f"(wick {found['wick']:.2f}, inside the {band:g}% band)",
            f"the next 5m candle closed back inside yesterday's range at "
            f"{found['reclaim']:.2f}",
            f"{shape or 'reclaim'} at the level",
            f"target {'VWAP' if vwap_r >= need else f'{need:g}R'} {target:.2f} "
            f"= {max(vwap_r, need):.1f}R on a {risk:.2f} risk",
        ]
        setup.reasoning = [
            f"**The trap.** Stops beyond the previous-day {what} were run and price "
            f"closed back inside — whoever chased the breakout is offside.",
            f"**Wrong if** price trades through the sweep wick {found['wick']:.2f}.",
            f"**Target** {target:.2f} ({'VWAP' if vwap_r >= need else f'{need:g}R'}); no "
            f"time stop — it runs to the target, the stop or the square-off.",
        ]
        return setup


# First in line: when a sweep of yesterday's level prints, it is the trade.
ALL.insert(0, PdLiquiditySweep)


class VolatilityBreakout(Strategy):
    """Larry Williams' volatility breakout (1987 Robbins World Cup, +11,376%).

    Yesterday's range says how far price can travel today. When it has
    already moved `k` x that range away from today's open, the day's
    expansion is under way: buy the first 5m CLOSE above open + k x range
    (calls), sell the first close below open - k x range (puts), with price
    on the same side of VWAP and the 9 EMA over the 21 for calls (under for
    puts). Only the bar that crosses counts — a level crossed an hour ago is
    a chase, not a breakout.

    Stop: `stop_fraction` of the way back from the trigger to the open
    (1.0 = the open itself — back there and the expansion has failed).
    Target: `target_r` x that risk.
    """

    name = SetupType.VOLATILITY_BREAKOUT
    window = ("09:45", "14:30")
    swing = False               # SwingBreakout: the 1-4 day edition

    def evaluate(self, symbol, df5, df15, levels) -> Setup:
        setup = _base(symbol, df5, self.name)
        g = self.cfg.get
        swing = self.swing
        k = float(g("swing.k", 0.5) if swing else g("strategies.volatility_breakout.k", 0.5))
        prev_range = float(levels.previous_high or 0) - float(levels.previous_low or 0)
        if prev_range <= 0:
            setup.blockers.append("no previous-day range to measure from")
            return setup
        today = df5[df5.index.date == df5.index[-1].date()]
        open_at = self._parse(str(g("session.market_open", "09:30")))
        today = today[[t.time() >= open_at for t in today.index]]
        if len(today) < 2:
            setup.blockers.append("today's session has not opened long enough")
            return setup
        day_open = float(today.iloc[0]["open"])
        up, down = day_open + k * prev_range, day_open - k * prev_range
        bar, prev = today.iloc[-1], today.iloc[-2]
        snapshot = ta.compute(df5, self.cfg)
        if swing:
            # The multi-day desk, as backtested on 2 years of hourly bars:
            # the first bar whose HIGH (LOW) reaches the level today, with
            # the 20-day trend; no VWAP / EMA condition.
            slope = self._daily_slope(df5, int(g("swing.trend_days", 20)))
            if slope is None:
                setup.blockers.append("not enough daily history for the 20-day trend")
                return setup
            earlier = today.iloc[:-1]
            long_break = (bar["high"] >= up and not (earlier["high"] >= up).any()
                          and not (earlier["low"] <= down).any() and slope > 0)
            short_break = (bar["low"] <= down and not (earlier["low"] <= down).any()
                           and not (earlier["high"] >= up).any() and slope < 0)
        else:
            long_break = (bar["close"] > up >= prev["close"]
                          and snapshot.close > snapshot.vwap
                          and snapshot.ema_fast > snapshot.ema_slow)
            short_break = (bar["close"] < down <= prev["close"]
                           and snapshot.close < snapshot.vwap
                           and snapshot.ema_fast < snapshot.ema_slow)
        if not (long_break or short_break):
            setup.blockers.append(
                f"no first touch today of open {day_open:.2f} ± {k:g} x yesterday's "
                f"range {prev_range:.2f} ({down:.2f} / {up:.2f}) with the "
                f"{int(g('swing.trend_days', 20))}-day trend" if swing else
                f"no fresh 5m close beyond open {day_open:.2f} ± {k:g} x yesterday's "
                f"range {prev_range:.2f} ({down:.2f} / {up:.2f}) with VWAP and the EMAs")
            return setup

        setup.direction = Direction.LONG if long_break else Direction.SHORT
        setup.indicators = snapshot
        setup.pattern = f"Volatility breakout ({k:g} x range)" + (", swing" if swing else "")
        level = up if long_break else down
        side = "above" if long_break else "below"
        setup.entry_trigger = float(bar["close"])
        if swing:
            setup.swing = True
            setup.min_dte_override = int(g("swing.min_dte", 21))
            setup.max_dte_override = int(g("swing.max_dte", 45))
            setup.delta_band = (float(g("swing.min_delta", 0.40)),
                                float(g("swing.max_delta", 0.55)))
        setup.key_level = level
        setup.key_level_source = f"open {'+' if long_break else '-'} {k:g} x previous range"
        setup.confirmations = [
            f"first 5m close {side} open {day_open:.2f} {'+' if long_break else '-'} "
            f"{k:g} x yesterday's range {prev_range:.2f} = {level:.2f} "
            f"(close {bar['close']:.2f}, previous {prev['close']:.2f})",
            f"price {side} VWAP ({snapshot.close:.2f} vs {snapshot.vwap:.2f})",
            f"9 EMA {'>' if long_break else '<'} 21 EMA "
            f"({snapshot.ema_fast:.2f} vs {snapshot.ema_slow:.2f})",
        ]
        if swing:
            setup.confirmations.append(
                f"swing: 20-day trend {'up' if long_break else 'down'}; held overnight to "
                f"the first profitable open, the stop, or {g('swing.max_hold_days', 4)} sessions")
        frac = 1.0 if swing else float(g("strategies.volatility_breakout.stop_fraction", 1.0))
        entry = float(bar["close"])
        risk = abs(entry - day_open) * frac
        sign = 1.0 if long_break else -1.0
        setup.underlying_support = round(entry - sign * risk, 4)
        setup.invalidation_note = (
            f"back {'below' if long_break else 'above'} {setup.underlying_support:.2f} "
            f"({frac:g} of the way to today's open {day_open:.2f}) — the expansion failed")
        setup.underlying_target = round(
            entry + sign * risk * float(g("strategies.volatility_breakout.target_r", 3.0)), 4)
        setup.trend_aligned = True
        return setup


def _daily_slope(df5: pd.DataFrame, days: int) -> float | None:
    """Yesterday's close less the close `days` sessions before it, from the
    5m history (today excluded)."""
    closes = df5["close"].groupby(df5.index.date).last()
    closes = closes.iloc[:-1]
    if len(closes) <= days:
        return None
    return float(closes.iloc[-1] - closes.iloc[-1 - days])


VolatilityBreakout._daily_slope = staticmethod(_daily_slope)


class SwingBreakout(VolatilityBreakout):
    """The volatility breakout as the swing book trades it (`swing.*`): held
    1-4 days on a 21-45 day option, beside the same-day strategies.

    As backtested on two years of hourly bars: the first bar whose HIGH
    (LOW) reaches today's open +/- swing.k x yesterday's range, with the
    swing.trend_days trend, no VWAP / EMA condition; stop today's open; out
    at the first later session that OPENS in profit, else the stop, else
    after swing.max_hold_days sessions. Not in ALL: the desk hunts it over
    its own list (swing.symbols) with evaluate_swing, screen or no screen.
    """

    name = SetupType.SWING_BREAKOUT
    window = ("09:45", "15:30")
    swing = True

    @property
    def opens(self) -> time:
        return self._parse(self.cfg.get("swing.from", self.window[0]))

    @property
    def closes(self) -> time:
        return self._parse(self.cfg.get("swing.to", self.window[1]))

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("swing.enabled", False))


def evaluate_swing(symbol: str, candles: list[Candle], levels: SessionLevels,
                   cfg) -> tuple[Setup | None, list[Setup]]:
    """The swing book's check on one symbol: (setup or None, [attempt]) —
    the same shape as evaluate_all, so the desk takes both the same way."""
    strategy = SwingBreakout(cfg)
    if not strategy.enabled:
        return None, []
    df5 = localise(ta.to_frame(candles),
                   str(cfg.get("session.timezone", "America/New_York")))
    if len(df5) < 21 or not strategy.in_window(df5):
        return None, []
    setup = strategy.evaluate(symbol, df5, ta.resample(df5, "15min"), levels)
    return (setup if setup.triggered else None), [setup]

LAST: list[type[Strategy]] = [VolatilityBreakout]   # after every other family


def register(cls: type[Strategy]) -> None:
    """Add a strategy family ahead of the ones that always run last."""
    at = next((i for i, c in enumerate(ALL) if c in LAST), len(ALL))
    ALL.insert(at, cls)


def _register_volume_profile() -> None:
    """Add the volume-profile family after the core four.

    Imported here so that anything using ALL sees all seven. If that module
    is the one being imported first it registers itself at its end instead.
    """
    try:
        from panaoptions.strategies import volume_profile_strategies as vp
        for cls in vp.STRATEGIES:
            if cls not in ALL:
                register(cls)
    except (ImportError, AttributeError):
        pass


ALL.extend(c for c in LAST if c not in ALL)
_register_volume_profile()
