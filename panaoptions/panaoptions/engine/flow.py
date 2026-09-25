"""Unusual options activity: option volume far above open interest.

When a contract trades several times its open interest in a session, new
positions are being opened in size — someone is paying for a view. That is
a catalyst the stock's own gap and volume can miss (CCC's 12,400-lot call
sweep against 3,800 open interest moved the options before the stock).

What this can see: each contract's day volume and open interest, from the
same chain snapshot the contract picker uses. What it cannot: whether the
volume traded at the ask (bought) or the bid (sold) — that needs individual
trade prints, which no free feed provides. So it reports WHERE money is
going, not which side of the trade it took.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from panaoptions.models import OptionRight


@dataclass
class Flow:
    unusual: list[Any] = field(default_factory=list)
    call_volume: int = 0
    put_volume: int = 0
    unusual_call_volume: int = 0
    unusual_put_volume: int = 0

    @property
    def found(self) -> bool:
        return bool(self.unusual)

    @property
    def bias(self) -> int:
        """+1 when unusual call volume is at least twice the put, -1 the other
        way, 0 when neither side dominates."""
        c, p = self.unusual_call_volume, self.unusual_put_volume
        if c >= 2 * max(p, 1) and c:
            return 1
        if p >= 2 * max(c, 1) and p:
            return -1
        return 0

    def headline(self) -> str:
        if not self.unusual:
            return "no unusual options activity"
        top = self.unusual[0]
        side = {1: "calls", -1: "puts", 0: "both sides"}[self.bias]
        return (f"unusual options activity in {side}: {top.label} traded "
                f"{top.volume:,} vs {top.open_interest:,} open interest "
                f"({len(self.unusual)} contract(s) at {self.ratio_of(top):.1f}x+)")

    @staticmethod
    def ratio_of(c) -> float:
        return c.volume / c.open_interest if c.open_interest else float(c.volume)


def scan(chain: list[Any], cfg) -> Flow:
    ratio = float(cfg.get("flow.min_volume_to_oi", 3.0))
    floor = int(cfg.get("flow.min_volume", 1000))
    out = Flow()
    for c in chain or []:
        vol = int(getattr(c, "volume", 0) or 0)
        if c.right is OptionRight.CALL:
            out.call_volume += vol
        else:
            out.put_volume += vol
        oi = int(getattr(c, "open_interest", 0) or 0)
        if vol >= floor and vol >= ratio * max(oi, 1):
            out.unusual.append(c)
            if c.right is OptionRight.CALL:
                out.unusual_call_volume += vol
            else:
                out.unusual_put_volume += vol
    out.unusual.sort(key=lambda c: c.volume, reverse=True)
    return out
