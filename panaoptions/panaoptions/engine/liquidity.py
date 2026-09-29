"""Liquidity: is a contract tradeable, judged on more than one snapshot?

Two checks.

  liquid()        open interest or volume enough that a paper fill at the
                  mid is believable. Applied to the 0.30-0.39 delta fallback
                  tier and to both legs of a debit spread — the places where
                  the desk reaches past its first choice.

  SpreadTracker   the bid-ask spread as a rolling 1-minute volume-weighted
                  average, instead of the single quote the chain happened to
                  return. At the open a quote can flash 12% wide for a few
                  seconds and settle at 3%; judged on that one snapshot, a
                  valid entry is refused for a spike that was already gone.

                  Every chain the desk reads is recorded. Each sample is
                  weighted by the volume that traded since the previous one
                  (at least 1), so the spread while contracts were actually
                  changing hands counts for more than the spread in a lull.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any

from panaoptions.models import OptionContract


def liquidity_problem(c: OptionContract, cfg: Any) -> str:
    """'' when the contract is liquid enough, else why not.

    Model-priced (estimated) contracts carry no open interest or volume, so
    only their spread can be judged — they pass this check and say so
    elsewhere.
    """
    if c.estimated:
        return ""
    min_oi = int(cfg.get("contracts.liquidity.min_open_interest", 100) or 0)
    min_vol = int(cfg.get("contracts.liquidity.min_volume", 50) or 0)
    if not min_oi and not min_vol:
        return ""
    if (min_oi and c.open_interest >= min_oi) or (min_vol and c.volume >= min_vol):
        return ""
    return (f"thin: open interest {c.open_interest} (< {min_oi}) and volume "
            f"{c.volume} (< {min_vol})")


class SpreadTracker:
    """Rolling volume-weighted bid-ask spread per contract."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window = float(window_seconds)
        self._samples: dict[str, deque[tuple[datetime, float, int, float]]] = {}

    def record(self, chain: list[OptionContract], now: datetime) -> None:
        for c in chain:
            if c.is_spread or c.mid <= 0 or not (c.bid and c.ask):
                continue
            q = self._samples.setdefault(c.label, deque())
            last_volume = q[-1][2] if q else None
            traded = max(int(c.volume or 0) - last_volume, 0) if last_volume is not None else 0
            q.append((now, float(c.spread_pct_of_mid), int(c.volume or 0), float(max(traded, 1))))
            self._trim(q, now)

    def _trim(self, q: deque, now: datetime) -> None:
        while q and (now - q[0][0]).total_seconds() > self.window:
            q.popleft()

    def rolling(self, label: str, now: datetime) -> float | None:
        q = self._samples.get(label)
        if not q:
            return None
        self._trim(q, now)
        if not q:
            return None
        weight = sum(s[3] for s in q)
        return round(sum(s[1] * s[3] for s in q) / weight, 2) if weight else None

    def samples(self, label: str) -> int:
        return len(self._samples.get(label) or ())

    def annotate(self, chain: list[OptionContract], now: datetime) -> list[OptionContract]:
        """Record this read, then give each contract its rolling spread."""
        self.record(chain, now)
        for c in chain:
            if not c.is_spread:
                c.rolling_spread_pct = self.rolling(c.label, now)
        return chain

    def clear(self) -> None:
        self._samples.clear()
