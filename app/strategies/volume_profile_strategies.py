"""Three institutional volume-profile strategies for buying calls and puts.

  1. Value Area Boundary Rejection (failed auction)
       PUT   price pokes above the prior or current session's VAH, fails to
             hold (a rejection wick or a bearish engulfing) and closes back
             inside value. Target: the POC. Wrong above the poke's high.
       CALL  price drops to the VAL, tests it, and rejects with a bullish
             candle. Target: the POC. Wrong below the test's low.
  2. Low Volume Node (LVN) Pocket Acceleration
       After a consolidation shelf, a 5-minute candle closes cleanly INTO an
       LVN — a price pocket nobody traded — on relative volume >= 1.5x. Thin
       volume means little to trade against, so price tends to travel through
       it fast. CALL on an upside break, PUT on a downside one. Target: the
       far edge of the pocket. Wrong back inside the shelf.
  3. Point of Control (POC) Magnet / Bounce
       Price trends at least 1 ATR away from the POC, comes back to retest it,
       and prints a clean rejection candle there. CALL off the POC from above,
       PUT off it from below. Target: the swing it came back from. Wrong on a
       close through the POC.

The detectors are pure: bars in, a `VPSignal` or None out. The Volume
Profile analyst (agents/volume_profile.py) runs them each cycle, and the CMIO
uses `level_alignment` / `hvn_wall` on every trade (agents/consensus.py).
panaoptions carries the same detectors; the two apps share no code.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.indicators.volume_profile import VolumeProfile, Zone, session_profiles


# --------------------------------------------------------------------------- #
# Bars and candles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def as_bar(obj: Any) -> Bar:
    get = obj.get if isinstance(obj, dict) else (lambda k, d=0.0: getattr(obj, k, d))
    return Bar(float(get("open", 0.0)), float(get("high", 0.0)), float(get("low", 0.0)),
               float(get("close", 0.0)), float(get("volume", 0.0) or 0.0))


def _range(b: Bar) -> float:
    return max(b.high - b.low, 1e-9)


def bullish_rejection(prev: Bar | None, bar: Bar) -> str:
    """The bullish rejection this candle prints, or ""."""
    lower_wick = min(bar.open, bar.close) - bar.low
    body = abs(bar.close - bar.open)
    if prev is not None and prev.close < prev.open and bar.close > bar.open \
            and bar.close >= prev.open and bar.open <= prev.close and body > 0:
        return "bullish engulfing"
    upper_wick = bar.high - max(bar.open, bar.close)
    # One-sided: a long lower wick and little above. A doji with equal wicks
    # is indecision, not a rejection in either direction.
    if lower_wick >= 0.5 * _range(bar) and upper_wick <= 0.2 * _range(bar):
        return "hammer / rejection wick"
    return ""


def bearish_rejection(prev: Bar | None, bar: Bar) -> str:
    """The bearish rejection this candle prints, or ""."""
    upper_wick = bar.high - max(bar.open, bar.close)
    body = abs(bar.close - bar.open)
    if prev is not None and prev.close > prev.open and bar.close < bar.open \
            and bar.close <= prev.open and bar.open >= prev.close and body > 0:
        return "bearish engulfing"
    lower_wick = min(bar.open, bar.close) - bar.low
    if upper_wick >= 0.5 * _range(bar) and lower_wick <= 0.2 * _range(bar):
        return "shooting star / rejection wick"
    return ""


def relative_volume(bars: Sequence[Bar], lookback: int = 20) -> float:
    """The last bar's volume against the average of the bars before it."""
    if len(bars) < 2:
        return 0.0
    prior = [b.volume for b in bars[-(lookback + 1):-1] if b.volume > 0]
    return bars[-1].volume / (sum(prior) / len(prior)) if prior else 0.0


# --------------------------------------------------------------------------- #
# The signal
# --------------------------------------------------------------------------- #
@dataclass
class VPSignal:
    """A volume-profile trigger on the underlying."""
    strategy: str                 # "va_rejection" | "lvn_acceleration" | "poc_bounce"
    direction: int                # +1 buy a CALL, -1 buy a PUT
    name: str                     # the human label
    trigger: float                # the entry price (the signal bar's close)
    invalidation: float           # where the idea is wrong, on the underlying
    target: float                 # where it is expected to go
    level: float                  # the profile level it traded off
    level_name: str               # e.g. "prior VAH"
    confirmations: list[str] = field(default_factory=list)

    @property
    def right(self) -> str:
        return "CALL" if self.direction > 0 else "PUT"


def _ordered(profiles: dict[str, VolumeProfile]) -> list[tuple[str, VolumeProfile]]:
    return [(k, profiles[k]) for k in ("prior", "current") if k in profiles]


# --------------------------------------------------------------------------- #
# 1. Value Area Boundary Rejection
# --------------------------------------------------------------------------- #
def detect_value_area_rejection(bars: Sequence[Bar], profiles: dict[str, VolumeProfile],
                                atr: float, *, lookback: int = 2,
                                test_atr: float = 0.10) -> VPSignal | None:
    """A failed auction at the VAH (put) or a held test of the VAL (call)."""
    if len(bars) < 2:
        return None
    bar, prev = bars[-1], bars[-2]
    recent = bars[-(lookback + 1):]
    tol = max(atr * test_atr, 1e-9)
    for which, prof in _ordered(profiles):
        # A poke has to CLEAR the level — by a tenth of an ATR, or the half
        # bin the VAH stands for — not graze it by a tick.
        clear = max(tol, prof.bin_size / 2.0)
        # PUT: above the VAH, could not stay, back inside value.
        poke = max(b.high for b in recent)
        rejection = bearish_rejection(prev, bar)
        if poke >= prof.vah + clear and bar.close < prof.vah and rejection \
                and prof.poc < bar.close:
            return VPSignal(
                "va_rejection", -1, "VAH rejection (failed auction)", bar.close,
                invalidation=poke, target=prof.poc, level=prof.vah,
                level_name=f"{which} VAH",
                confirmations=[
                    f"pushed to {poke:.2f}, above the {which} session VAH {prof.vah:.2f}",
                    f"{rejection} closed back inside value at {bar.close:.2f}",
                    f"target the POC {prof.poc:.2f}"])
        # CALL: down to the VAL, tested it, held.
        probe = min(b.low for b in recent)
        rejection = bullish_rejection(prev, bar)
        if probe <= prof.val + tol and bar.close > prof.val and rejection \
                and prof.poc > bar.close:
            return VPSignal(
                "va_rejection", +1, "VAL bounce (failed auction)", bar.close,
                invalidation=probe, target=prof.poc, level=prof.val,
                level_name=f"{which} VAL",
                confirmations=[
                    f"tested {probe:.2f} against the {which} session VAL {prof.val:.2f}",
                    f"{rejection} held it, close {bar.close:.2f}",
                    f"target the POC {prof.poc:.2f}"])
    return None


# --------------------------------------------------------------------------- #
# 2. LVN Pocket Acceleration
# --------------------------------------------------------------------------- #
def detect_lvn_acceleration(bars: Sequence[Bar], profiles: dict[str, VolumeProfile],
                            atr: float, *, min_rvol: float = 1.5, shelf_bars: int = 3,
                            shelf_atr: float = 2.0,
                            rvol: float | None = None) -> VPSignal | None:
    """A close from a consolidation shelf into a volume pocket, with volume."""
    if len(bars) < shelf_bars + 1:
        return None
    bar = bars[-1]
    shelf = bars[-(shelf_bars + 1):-1]
    measured = relative_volume(bars) if rvol is None else rvol
    width = max(b.high for b in shelf) - min(b.low for b in shelf)
    tight = atr <= 0 or width <= shelf_atr * atr
    for which, prof in _ordered(profiles):
        for pocket in prof.lvns:
            if not pocket.low < bar.close < pocket.high:
                continue
            up = all(b.close <= pocket.low for b in shelf) and bar.close > shelf[-1].close
            down = all(b.close >= pocket.high for b in shelf) and bar.close < shelf[-1].close
            if not (up or down) or not tight or measured < min_rvol:
                continue
            direction = 1 if up else -1
            edge = pocket.low if up else pocket.high
            return VPSignal(
                "lvn_acceleration", direction,
                f"LVN pocket break {'up' if up else 'down'}", bar.close,
                invalidation=min(bar.low, edge) if up else max(bar.high, edge),
                target=pocket.high if up else pocket.low, level=edge,
                level_name=f"{which} LVN {pocket.low:.2f}-{pocket.high:.2f}",
                confirmations=[
                    f"{shelf_bars}-bar shelf {min(b.low for b in shelf):.2f}-"
                    f"{max(b.high for b in shelf):.2f} broken",
                    f"5m close {bar.close:.2f} inside the {which} session's volume "
                    f"pocket {pocket.low:.2f}-{pocket.high:.2f}",
                    f"relative volume {measured:.2f}x ≥ {min_rvol:g}x",
                    f"target the far edge {pocket.high if up else pocket.low:.2f}"])
    return None


# --------------------------------------------------------------------------- #
# 3. POC Magnet / Bounce
# --------------------------------------------------------------------------- #
def detect_poc_bounce(bars: Sequence[Bar], profiles: dict[str, VolumeProfile],
                      atr: float, *, lookback: int = 12, away_atr: float = 1.0,
                      touch_atr: float = 0.20) -> VPSignal | None:
    """A retest of the POC after a move away, rejected at the POC."""
    if len(bars) < 3 or atr <= 0:
        return None
    which, prof = ("current", profiles["current"]) if "current" in profiles else \
        ("prior", profiles["prior"]) if "prior" in profiles else (None, None)
    if prof is None:
        return None
    bar, prev = bars[-1], bars[-2]
    window = bars[-(lookback + 1):-1]
    poc, tol = prof.poc, touch_atr * atr
    # From above: went up at least away_atr x ATR, came back to the POC, held.
    peak = max(b.high for b in window)
    rejection = bullish_rejection(prev, bar)
    if peak - poc >= away_atr * atr and bar.low <= poc + tol and bar.close > poc \
            and rejection:
        return VPSignal(
            "poc_bounce", +1, "POC bounce from above", bar.close,
            invalidation=min(bar.low, poc - tol), target=peak, level=poc,
            level_name=f"{which} POC",
            confirmations=[f"ran to {peak:.2f}, {(peak - poc) / atr:.1f} ATR above the "
                           f"{which} POC {poc:.2f}",
                           f"came back and printed a {rejection} at the POC",
                           f"target the swing high {peak:.2f}"])
    trough = min(b.low for b in window)
    rejection = bearish_rejection(prev, bar)
    if poc - trough >= away_atr * atr and bar.high >= poc - tol and bar.close < poc \
            and rejection:
        return VPSignal(
            "poc_bounce", -1, "POC rejection from below", bar.close,
            invalidation=max(bar.high, poc + tol), target=trough, level=poc,
            level_name=f"{which} POC",
            confirmations=[f"fell to {trough:.2f}, {(poc - trough) / atr:.1f} ATR below the "
                           f"{which} POC {poc:.2f}",
                           f"came back and printed a {rejection} at the POC",
                           f"target the swing low {trough:.2f}"])
    return None


DETECTORS = {"va_rejection": detect_value_area_rejection,
             "lvn_acceleration": detect_lvn_acceleration,
             "poc_bounce": detect_poc_bounce}


# --------------------------------------------------------------------------- #
# Confluence, for any signal from any strategy
# --------------------------------------------------------------------------- #
def level_alignment(price: float, direction: int, profiles: dict[str, VolumeProfile],
                    atr: float, tol_atr: float = 0.25) -> str:
    """The profile level this entry trades off, or "".

    A call bought at a VAL or POC (support), or a put bought at a VAH or POC
    (resistance), is where the volume says the auction turns.
    """
    tol = max(atr * tol_atr, price * 0.0005)
    for which, prof in _ordered(profiles):
        levels = ((f"{which} VAL", prof.val), (f"{which} POC", prof.poc)) if direction > 0 \
            else ((f"{which} VAH", prof.vah), (f"{which} POC", prof.poc))
        for name, level in levels:
            if abs(price - level) <= tol:
                return f"{name} {level:.2f}"
    return ""


def hvn_wall(price: float, direction: int, profiles: dict[str, VolumeProfile],
             atr: float, target: float | None = None, near_atr: float = 1.0,
             veto_atr: float = 0.25, min_share: float = 0.10) -> tuple[str, Zone | None]:
    """("veto" | "penalty" | "", zone): a thick High Volume Node straight ahead.

    Ahead means in the trade's direction and not yet reached. A node that holds
    the trade's own target is where it is MEANT to go (a VAH put aiming at the
    POC), so it is not a wall. Within `veto_atr` ATR it is a veto — buying
    straight into accepted inventory — within `near_atr` a penalty. Only a
    node holding at least `min_share` of its session's volume is a wall; a
    young session's profile is full of slivers that are not.
    """
    if atr <= 0:
        return "", None
    closest: tuple[float, Zone] | None = None
    for _, prof in _ordered(profiles):
        for zone in prof.hvns:
            if zone.volume < min_share * prof.total_volume:
                continue
            gap = zone.low - price if direction > 0 else price - zone.high
            if gap <= 0:
                continue                       # behind, or already inside it
            if target is not None and (zone.low <= target <= zone.high or
                                       (direction > 0 and target <= zone.low) or
                                       (direction < 0 and target >= zone.high)):
                continue                       # it is the destination
            if closest is None or gap < closest[0]:
                closest = (gap, zone)
    if closest is None:
        return "", None
    gap, zone = closest
    if gap <= veto_atr * atr:
        return "veto", zone
    if gap <= near_atr * atr:
        return "penalty", zone
    return "", None


def profiles_for(candles: Sequence[Any], cfg: Any) -> dict[str, VolumeProfile]:
    """Prior and current RTH session profiles, in the active market's hours."""
    g = cfg.get
    return session_profiles(
        candles, str(g("system.timezone", "America/New_York")),
        str(g("volume_profile.rth_open") or g("system.market_open", "09:30")),
        str(g("volume_profile.rth_close") or g("system.market_close", "16:00")),
        bins=int(g("volume_profile.bins", 40)),
        value_area_pct=float(g("volume_profile.value_area_pct", 0.70)),
        lvn_ratio=float(g("volume_profile.lvn_ratio", 0.30)),
        hvn_ratio=float(g("volume_profile.hvn_ratio", 1.5)))


def evaluate(candles: Sequence[Any], cfg: Any, atr: float
             ) -> tuple[VPSignal | None, dict[str, VolumeProfile]]:
    """Run the three detectors in order; the first to trigger wins."""
    profiles = profiles_for(candles, cfg)
    if not profiles:
        return None, profiles
    bars = [as_bar(c) for c in list(candles)[-40:]]
    g = cfg.get
    enabled = lambda key: bool(g(f"volume_profile.strategies.{key}", True))  # noqa: E731
    checks = (
        ("va_rejection", lambda: detect_value_area_rejection(
            bars, profiles, atr, lookback=int(g("volume_profile.va_lookback_bars", 2)))),
        ("lvn_acceleration", lambda: detect_lvn_acceleration(
            bars, profiles, atr, min_rvol=float(g("volume_profile.lvn_min_rvol", 1.5)),
            shelf_bars=int(g("volume_profile.lvn_shelf_bars", 3)))),
        ("poc_bounce", lambda: detect_poc_bounce(
            bars, profiles, atr, away_atr=float(g("volume_profile.poc_away_atr", 1.0)))),
    )
    for key, check in checks:
        if enabled(key):
            found = check()
            if found is not None:
                return found, profiles
    return None, profiles
