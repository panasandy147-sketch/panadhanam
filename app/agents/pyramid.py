"""The Standard Pyramid (Base-50-25): add to winners, never to losers.

    Level 0  base entry     1R fractional sizing: quantity = 1R capital / stop
                            distance (the risk desk's sizing identity)
    Level 1  at +1.0R open  ADD 50% of the base quantity; the stop for the
                            WHOLE position moves to the base entry price
    Level 2  at +2.0R open  ADD 25% of the base quantity; the stop for the
                            whole position moves to the Level 1 fill price
    exit                    one take-profit at +3.0R from the BASE entry
                            liquidates the full 175% position; the stop (or the
                            square-off / breaker) takes everything otherwise

R is always the BASE trade's risk: 1R = |base entry - original stop|. After
Level 1 the worst case is -0.5R (the add, stopped at the base entry); after
Level 2 it is +0.75R locked (base +1R, Level 1 flat, Level 2 -1R x 25%).

"No averaging down": an add is only ever made in open profit, and the risk
desk refuses any add (or any new order on the symbol) while the position is
in a drawdown. Everything here is arithmetic — no model, no network — and
the state rides in the signal's payload so a restart resumes it.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_LEVELS = [
    {"at_r": 1.0, "size": 0.50, "stop": "base"},     # Level 1
    {"at_r": 2.0, "size": 0.25, "stop": "level1"},   # Level 2
]


@dataclass
class PyramidState:
    level: int                 # 0 = base only, 1, 2
    base_qty: int
    base_entry: float
    base_spot: float           # the underlying at the base entry (options)
    r_unit: float              # |base entry - original stop|, per unit
    target: float              # base entry +/- target_r x R
    avg_entry: float
    qty: int
    level1_price: float = 0.0
    level1_spot: float = 0.0
    adds: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Add:
    level: int
    quantity: int              # may be 0: the stop still moves (see plan())
    price: float               # the fill price (the mark)
    spot: float
    new_stop: float            # for the WHOLE position, in the row's price terms
    new_underlying_stop: float  # options: the same level on the underlying
    avg_entry: float           # blended after the add
    total_qty: int
    open_r: float
    note: str


def enabled(cfg: Any) -> bool:
    return bool(cfg.get("risk.pyramid.enabled", False))


def levels(cfg: Any) -> list[dict[str, Any]]:
    return list(cfg.get("risk.pyramid.levels") or DEFAULT_LEVELS)


def start(cfg: Any, row: dict[str, Any], unit: int = 1) -> PyramidState:
    """The state for a freshly opened position (Level 0)."""
    entry, stop = float(row["entry"]), float(row["stop_loss"])
    sign = 1.0 if row["side"] == "BUY" else -1.0
    r_unit = abs(entry - stop)
    target_r = float(cfg.get("risk.pyramid.target_r", 3.0))
    return PyramidState(level=0, base_qty=int(row["quantity"] or 0), base_entry=entry,
                        base_spot=float(row.get("entry_spot") or 0.0), r_unit=r_unit,
                        target=round(entry + sign * target_r * r_unit, 4),
                        avg_entry=entry, qty=int(row["quantity"] or 0))


def open_r(state: PyramidState, mark: float, is_long: bool) -> float:
    """Open profit in units of the BASE trade's risk, from the base entry."""
    if state.r_unit <= 0:
        return 0.0
    sign = 1.0 if is_long else -1.0
    return (mark - state.base_entry) * sign / state.r_unit


def plan(cfg: Any, state: PyramidState, mark: float, spot: float | None, is_long: bool,
         unit: int = 1) -> Add | None:
    """The next add, when the trade has reached its level — else None.

    Sizes are rounded down to whole lots/units; when the rounded add is zero
    (50% of one lot) the level is still taken: the stop moves up, no size is
    added, and the note says so.
    """
    steps = levels(cfg)
    if state.level >= len(steps):
        return None
    step = steps[state.level]
    r_now = open_r(state, mark, is_long)
    if r_now + 1e-9 < float(step["at_r"]):
        return None
    unit = max(int(unit or 1), 1)
    raw = state.base_qty * float(step["size"])
    qty = int(math.floor(raw / unit)) * unit
    level = state.level + 1
    if str(step.get("stop")) == "level1" and state.level1_price:
        new_stop, new_ustop = state.level1_price, state.level1_spot
    else:
        new_stop, new_ustop = state.base_entry, state.base_spot
    total = state.qty + qty
    avg = ((state.avg_entry * state.qty + mark * qty) / total) if total else state.avg_entry
    what = f"Level {level} at {r_now:+.2f}R: "
    note = (what + f"added {qty} ({float(step['size']):.0%} of the {state.base_qty} base) at "
            f"{mark:,.2f}; stop for all {total} moved to {new_stop:,.2f}"
            if qty else
            what + f"{float(step['size']):.0%} of {state.base_qty} rounds to 0 at a "
                   f"{unit}-unit lot — no size added; stop moved to {new_stop:,.2f}")
    return Add(level=level, quantity=qty, price=round(mark, 4), spot=float(spot or 0.0),
               new_stop=round(new_stop, 4), new_underlying_stop=round(new_ustop or 0.0, 4),
               avg_entry=round(avg, 4), total_qty=total, open_r=round(r_now, 2), note=note)


def apply(state: PyramidState, add: Add) -> PyramidState:
    state.level = add.level
    state.qty = add.total_qty
    state.avg_entry = add.avg_entry
    if add.level == 1:
        state.level1_price, state.level1_spot = add.price, add.spot
    state.adds.append({"level": add.level, "quantity": add.quantity, "price": add.price,
                       "stop": add.new_stop, "open_r": add.open_r})
    return state


def base_r_multiple(state: PyramidState, pnl: float) -> float:
    """The whole position's result in units of the BASE risk (1R)."""
    risk = state.base_qty * state.r_unit
    return round(pnl / risk, 3) if risk else 0.0


def walk(bars: list[Any], is_long: bool, entry: float, stop: float, cfg: Any,
         pyramid_on: bool = True) -> dict[str, Any]:
    """Replay one trade over the bars AFTER its entry bar (already cut to the
    session: the caller ends the list at the square-off).

    pyramid_on=False is the plain trade: one unit, stop, the target_r target,
    else the last bar's close. pyramid_on=True runs Base-50-25: adds at the
    level prices (+1R, +2R), the stop for the whole position stepping to the
    base entry then the Level 1 price, one target at target_r from the base.
    Within a bar the stop is checked first (the conservative reading of OHLC).
    Returns {"r": result in BASE R, "outcome", "bars", "adds"}.
    """
    sign = 1.0 if is_long else -1.0
    r_unit = abs(entry - stop)
    if r_unit <= 0 or not bars:
        return {"r": 0.0, "outcome": "NONE", "bars": 0, "adds": 0}
    target = entry + sign * float(cfg.get("risk.pyramid.target_r", 3.0)) * r_unit
    steps = levels(cfg) if pyramid_on else []
    qty, avg, cur_stop, level, l1_price = 1.0, entry, stop, 0, 0.0
    for i, b in enumerate(bars, 1):
        worst = b.low if is_long else b.high
        best = b.high if is_long else b.low
        if (worst - cur_stop) * sign <= 0:
            pnl = (cur_stop - avg) * sign * qty
            return {"r": round(pnl / r_unit, 3), "outcome": "STOP" if level == 0 else
                    f"STOP_L{level}", "bars": i, "adds": level}
        if (best - target) * sign >= 0:
            pnl = (target - avg) * sign * qty
            return {"r": round(pnl / r_unit, 3), "outcome": "TARGET", "bars": i, "adds": level}
        # Adds at their level prices, in order, as far as this bar reached.
        while level < len(steps):
            step = steps[level]
            price = entry + sign * float(step["at_r"]) * r_unit
            if (best - price) * sign < 0:
                break
            add = float(step["size"])
            avg = (avg * qty + price * add) / (qty + add)
            qty += add
            level += 1
            if level == 1:
                l1_price = price
            cur_stop = l1_price if str(step.get("stop")) == "level1" and l1_price else entry
    last = float(bars[-1].close)
    pnl = (last - avg) * sign * qty
    return {"r": round(pnl / r_unit, 3), "outcome": "SQUARE_OFF", "bars": len(bars),
            "adds": level}
