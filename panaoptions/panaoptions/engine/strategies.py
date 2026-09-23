"""The three entry strategies, each with its own session window and its own
invalidation level on the UNDERLYING.

Why three rather than one general rule: a breakout and a pullback are not the
same trade, they work at different times of day, and lumping them together
makes the journal unable to answer "which of these actually pays". Every setup
is tagged with the strategy that produced it.

Why each carries an underlying invalidation rather than a premium percentage:
a fixed -20% on the contract is at the mercy of an implied-volatility shift or
a wide spread, and says nothing about whether the trade was wrong. The level
below says exactly what "wrong" means for that entry.

All three avoid the 09:30-09:45 opening chop, where spreads are widest and the
first prints are noise.
"""
from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

import pandas as pd

from panaoptions.engine import indicators as ta
from panaoptions.engine import patterns
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


def _volume_ok(snapshot, multiple: float) -> bool:
    return snapshot.avg_volume > 0 and snapshot.volume >= snapshot.avg_volume * multiple


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
        multiple = float(self.cfg.get("strategies.orb_vwap.volume_multiple", 1.5))
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
            setup.confirmations.append(
                f"volume {snapshot.volume:,.0f} is {snapshot.rvol:.1f}x the average")
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
        ]
        if _volume_ok(snapshot, float(self.cfg.get(
                "strategies.vwap_ema_pullback.volume_multiple", 1.0))):
            setup.confirmations.append(f"volume {snapshot.rvol:.1f}x average")

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
        multiple = float(self.cfg.get("strategies.liquidity_sweep.volume_multiple", 1.5))

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
            setup.confirmations.append(
                f"volume {snapshot.rvol:.1f}x average on the reclaim")
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

    for cls in ALL:
        strategy = cls(cfg)
        if not strategy.enabled or not strategy.in_window(df5):
            continue
        setup = strategy.evaluate(symbol, df5, df15, levels)
        attempts.append(setup)
        if winner is None and setup.triggered:
            winner = setup
    return winner, attempts
