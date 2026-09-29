"""Which strategies get the day's scarce trade slots.

With four trades a day, first-come-first-served hands the day to whatever
fires earliest: the 10-session validation filled its slots with morning VWAP
pullbacks and POC bounces, and the opening-range breakouts and sweeps that
came later found the day already full. So the slots are ranked:

  edge       each strategy's backtested expectancy (R per fill, before the
             throttles) from the last `python run.py --backtest` on this
             market — strategies with fewer than `ranking.min_fills` fills
             count as unknown
  fallback   no validation yet: the order of `ranking.priority`

Two rules use it:
  * reserved slots  the last `risk.reserved_slots` of the day's
                    `max_daily_trades` are kept for PREFERRED strategies —
                    edge above `ranking.min_edge_r`, or (no edge yet) the
                    first `ranking.preferred_top` of the priority list
  * cycle order     when several symbols fire in the same cycle, the best
                    edge is taken first
"""
from __future__ import annotations

from typing import Any

from panaoptions.models import SetupType


def key(strategy: Any) -> str:
    """'orb_vwap' for SetupType.ORB_VWAP, 'ORB + VWAP' or 'orb_vwap'."""
    if isinstance(strategy, SetupType):
        return strategy.name.lower()
    text = str(strategy or "")
    for st in SetupType:
        if text in (st.value, st.name, st.name.lower()):
            return st.name.lower()
    return text.lower()


def live_edge(cfg: Any) -> dict[str, float]:
    """The edge from this market's last validation, or {} when there is none."""
    from panaoptions import validate
    last = validate.latest() or {}
    return edge_from(last.get("edge") or {}, cfg)


def edge_from(raw: dict[str, Any], cfg: Any) -> dict[str, float]:
    need = int(cfg.get("ranking.min_fills", 5))
    return {key(name): float(v.get("expectancy_r", 0.0)) for name, v in raw.items()
            if int(v.get("fills", 0)) >= need}


def _priority(cfg: Any) -> list[str]:
    return [key(s) for s in (cfg.get("ranking.priority") or [])]


def score(cfg: Any, strategy: Any, edge: dict[str, float] | None = None) -> float:
    """Higher is better. The edge in R when known; else a small score from the
    priority list, below any positive edge."""
    k = key(strategy)
    if edge and k in edge:
        return edge[k]
    order = _priority(cfg)
    return (len(order) - order.index(k)) / 1000.0 if k in order else 0.0


def preferred(cfg: Any, strategy: Any, edge: dict[str, float] | None = None) -> bool:
    k = key(strategy)
    if edge:
        return edge.get(k, float("-inf")) > float(cfg.get("ranking.min_edge_r", 0.0))
    return k in _priority(cfg)[:int(cfg.get("ranking.preferred_top", 2))]


def slot_refusal(cfg: Any, strategy: Any, taken_today: int,
                 edge: dict[str, float] | None = None) -> str:
    """Why this strategy may not take today's next trade, or ''."""
    max_daily = int(cfg.get("risk.max_daily_trades", 0) or 0)
    reserved = int(cfg.get("risk.reserved_slots", 0) or 0)
    if not max_daily or not reserved or preferred(cfg, strategy, edge):
        return ""
    if taken_today >= max_daily - reserved:
        return (f"the last {reserved} of today's {max_daily} trades are kept for the "
                f"strategies with the best backtested edge")
    return ""
