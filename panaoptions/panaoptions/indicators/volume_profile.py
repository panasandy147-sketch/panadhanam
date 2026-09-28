"""Session volume profile: where the volume traded, by price.

For one Regular Trading Hours session (09:30-16:00 ET by default) this builds
a histogram of volume by price and reads the levels institutions trade
against:

  POC   Point of Control — the single price bin with the most volume.
  VAH   Value Area High  } the band around the POC holding
  VAL   Value Area Low   } `value_area_pct` (70%) of the session's volume.
  HVN   High Volume Nodes — runs of bins at >= `hvn_ratio` x the mean bin:
        accepted prices, where the market slows and rotates.
  LVN   Low Volume Nodes — runs of bins at <= `lvn_ratio` x the mean bin,
        INSIDE the profile (the thin tails at the extremes are not pockets):
        rejected prices, where price travels fast because nobody traded there.

Two builders:

  from_arrays(prices, volumes)   one volume per price print — exact, for
                                 trade prints or tests.
  from_bars(bars)                OHLCV candles; each bar's volume is spread
                                 evenly across the bins its high-low range
                                 covers (the standard approximation when tick
                                 data is not available).

The value area expands one bin at a time from the POC toward whichever side
holds more volume (ties go up), until 70% is inside.

Pure: no config object, no feed, no account. Everything is a parameter.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Zone:
    """A contiguous run of price bins, [low, high]."""
    low: float
    high: float
    volume: float

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def to_dict(self) -> dict[str, float]:
        return {"low": round(self.low, 4), "high": round(self.high, 4),
                "volume": round(self.volume, 2)}


@dataclass(frozen=True)
class VolumeProfile:
    """One session's profile."""
    poc: float
    vah: float
    val: float
    total_volume: float
    bin_size: float
    levels: tuple[float, ...]
    volumes: tuple[float, ...]
    lvns: tuple[Zone, ...] = field(default_factory=tuple)
    hvns: tuple[Zone, ...] = field(default_factory=tuple)
    session: str = ""

    @property
    def high(self) -> float:
        return self.levels[-1] + self.bin_size / 2.0

    @property
    def low(self) -> float:
        return self.levels[0] - self.bin_size / 2.0

    def in_value(self, price: float) -> bool:
        return self.val <= price <= self.vah

    def lvn_at(self, price: float) -> Zone | None:
        return next((z for z in self.lvns if z.contains(price)), None)

    def hvn_at(self, price: float) -> Zone | None:
        return next((z for z in self.hvns if z.contains(price)), None)

    def to_dict(self) -> dict[str, Any]:
        return {"session": self.session, "poc": round(self.poc, 4),
                "vah": round(self.vah, 4), "val": round(self.val, 4),
                "total_volume": round(self.total_volume, 2),
                "bin_size": round(self.bin_size, 6),
                "lvns": [z.to_dict() for z in self.lvns],
                "hvns": [z.to_dict() for z in self.hvns]}


# --------------------------------------------------------------------------- #
# The histogram
# --------------------------------------------------------------------------- #
def _finish(levels: list[float], volumes: list[float], bin_size: float,
            value_area_pct: float, lvn_ratio: float, hvn_ratio: float,
            session: str) -> VolumeProfile | None:
    total = sum(volumes)
    if not levels or total <= 0:
        return None
    n = len(volumes)

    # POC: the heaviest bin; on a tie, the one nearest the volume-weighted
    # centre, so a flat top does not default to its lowest price.
    centre = sum(p * v for p, v in zip(levels, volumes, strict=True)) / total
    heaviest = max(volumes)
    poc_i = min((i for i, v in enumerate(volumes) if v == heaviest),
                key=lambda i: abs(levels[i] - centre))

    # Value area: grow from the POC toward the heavier neighbour.
    lo = hi = poc_i
    inside = volumes[poc_i]
    target = total * value_area_pct
    while inside < target - 1e-12 and (lo > 0 or hi < n - 1):
        up = volumes[hi + 1] if hi + 1 < n else -1.0
        down = volumes[lo - 1] if lo > 0 else -1.0
        if up >= down:
            hi += 1
            inside += up
        else:
            lo -= 1
            inside += down

    mean = total / n
    half = bin_size / 2.0

    def runs(test) -> list[tuple[int, int]]:
        out, start = [], None
        for i, v in enumerate(volumes):
            if test(v):
                start = i if start is None else start
            elif start is not None:
                out.append((start, i - 1))
                start = None
        if start is not None:
            out.append((start, n - 1))
        return out

    def zone(a: int, b: int) -> Zone:
        return Zone(levels[a] - half, levels[b] + half, sum(volumes[a:b + 1]))

    hvns = tuple(zone(a, b) for a, b in runs(lambda v: v >= hvn_ratio * mean))
    # A pocket has accepted prices on both sides; a thin tail at the edge of
    # the session's range is just where it did not go.
    lvns = tuple(zone(a, b) for a, b in runs(lambda v: v <= lvn_ratio * mean)
                 if a > 0 and b < n - 1)

    return VolumeProfile(poc=levels[poc_i], vah=levels[hi], val=levels[lo],
                         total_volume=total, bin_size=bin_size,
                         levels=tuple(levels), volumes=tuple(volumes),
                         lvns=lvns, hvns=hvns, session=session)


def from_arrays(prices: Sequence[float], volumes: Sequence[float],
                bin_size: float | None = None, *, bins: int = 40,
                value_area_pct: float = 0.70, lvn_ratio: float = 0.30,
                hvn_ratio: float = 1.5, session: str = "") -> VolumeProfile | None:
    """A profile from price prints, one volume per price.

    With no `bin_size` the range is cut into `bins` bins. Bin centres sit on
    min(prices) + k * bin_size, so prints on a regular grid land exactly on
    their own level.
    """
    pairs = [(float(p), float(v)) for p, v in zip(prices, volumes, strict=True)
             if v and v > 0 and math.isfinite(p)]
    if not pairs:
        return None
    low = min(p for p, _ in pairs)
    high = max(p for p, _ in pairs)
    if not bin_size or bin_size <= 0:
        bin_size = (high - low) / max(bins - 1, 1) if high > low else 1.0
    n = int(round((high - low) / bin_size)) + 1
    volume = [0.0] * n
    for p, v in pairs:
        volume[min(n - 1, max(0, int(round((p - low) / bin_size))))] += v
    levels = [round(low + i * bin_size, 10) for i in range(n)]
    return _finish(levels, volume, bin_size, value_area_pct, lvn_ratio,
                   hvn_ratio, session)


def from_bars(bars: Iterable[Any], bin_size: float | None = None, *,
              bins: int = 40, value_area_pct: float = 0.70,
              lvn_ratio: float = 0.30, hvn_ratio: float = 1.5,
              session: str = "") -> VolumeProfile | None:
    """A profile from OHLCV bars (objects or dicts with high/low/volume).

    Each bar's volume is spread evenly over the bins its range touches.
    """
    rows = []
    for b in bars:
        hi, lo, vol = _get(b, "high"), _get(b, "low"), _get(b, "volume")
        if vol and vol > 0 and hi >= lo > 0:
            rows.append((lo, hi, vol))
    if not rows:
        return None
    low = min(r[0] for r in rows)
    high = max(r[1] for r in rows)
    if not bin_size or bin_size <= 0:
        bin_size = (high - low) / bins if high > low else max(low * 0.0005, 0.01)
    n = max(1, int(math.ceil((high - low) / bin_size - 1e-9)))
    volume = [0.0] * n
    for lo, hi, vol in rows:
        a = min(n - 1, max(0, int((lo - low) / bin_size)))
        b = min(n - 1, max(0, int(math.ceil((hi - low) / bin_size - 1e-9)) - 1))
        b = max(a, b)
        share = vol / (b - a + 1)
        for i in range(a, b + 1):
            volume[i] += share
    levels = [round(low + (i + 0.5) * bin_size, 10) for i in range(n)]
    return _finish(levels, volume, bin_size, value_area_pct, lvn_ratio,
                   hvn_ratio, session)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def _get(bar: Any, key: str) -> float:
    value = bar.get(key) if isinstance(bar, dict) else getattr(bar, key, 0.0)
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _hhmm(value: str) -> time:
    hour, _, minute = str(value).partition(":")
    return time(int(hour), int(minute or 0))


def _local(ts: Any, zone: ZoneInfo) -> datetime | None:
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if not isinstance(ts, datetime):
        return None
    # A naive stamp is taken to be exchange-local already (the simulator).
    return ts.astimezone(zone) if ts.tzinfo else ts.replace(tzinfo=zone)


def rth_sessions(bars: Iterable[Any], tz: str = "America/New_York",
                 open_: str = "09:30", close: str = "16:00") -> dict[date, list[Any]]:
    """Bars grouped by session date, Regular Trading Hours only, in order.

    Bars need a `ts` (datetime or pandas Timestamp); a bar stamped at its
    open is inside RTH when open_ <= time < close.
    """
    zone = ZoneInfo(tz)
    start, end = _hhmm(open_), _hhmm(close)
    out: dict[date, list[Any]] = {}
    for bar in bars:
        stamp = _local(bar.get("ts") if isinstance(bar, dict) else getattr(bar, "ts", None),
                       zone)
        if stamp is None:
            continue
        if start <= stamp.time() < end:
            out.setdefault(stamp.date(), []).append(bar)
    return dict(sorted(out.items()))


def session_profiles(bars: Iterable[Any], tz: str = "America/New_York",
                     open_: str = "09:30", close: str = "16:00", *,
                     exclude_last: bool = True, min_current_bars: int = 6,
                     **kwargs: Any) -> dict[str, VolumeProfile]:
    """{"prior": ..., "current": ...} for the last two RTH sessions.

    `exclude_last` leaves the newest bar out of the current profile: that is
    the bar being judged, and it must be measured against the profile it is
    trading into, not one it has already reshaped. The current profile needs
    `min_current_bars` bars to mean anything; before that only the prior
    session's is returned.
    """
    grouped = rth_sessions(bars, tz, open_, close)
    days = list(grouped)
    out: dict[str, VolumeProfile] = {}
    if not days:
        return out
    today = grouped[days[-1]]
    building = today[:-1] if exclude_last else today
    if len(building) >= min_current_bars:
        prof = from_bars(building, session=days[-1].isoformat(), **kwargs)
        if prof:
            out["current"] = prof
    if len(days) >= 2:
        prof = from_bars(grouped[days[-2]], session=days[-2].isoformat(), **kwargs)
        if prof:
            out["prior"] = prof
    return out
