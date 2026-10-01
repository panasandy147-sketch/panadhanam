"""The asymmetric reward-to-risk gate: at least 1:3, or no trade.

Risk is the distance from the entry trigger to the invalidation level on the
UNDERLYING — where the idea is wrong. The projected target is at least
`risk.min_reward_risk` (3) times that distance, in the trade's direction.

A projection is only honest if the road to it is open. The nearest opposing
session level — the previous day's high or low, the opening range, the
pre-market extreme — is where a move usually stalls; if it sits inside the
3R target, the 1:3 is wishful and the setup is refused, naming the level.
A strategy's own target is kept when it is already 3R or more and inside
the room.
"""
from __future__ import annotations

from typing import Any

from panaoptions.models import Direction, SetupType

_SLACK = 0.01          # R — rounding, not a looser rule

_ROOM = {
    "previous_day": (("previous_high", "previous-day high"), ("previous_low", "previous-day low")),
    "opening_range": (("opening_range_high", "opening range high"),
                      ("opening_range_low", "opening range low")),
    "premarket": (("premarket_high", "pre-market high"), ("premarket_low", "pre-market low")),
    "previous_close": (("previous_close", "previous close"),),
}


def widen_stop(setup: Any, cfg: Any) -> str:
    """The stop floor (risk.min_stop_atr x the 5m ATR, or risk.min_stop_pct %
    of the price, whichever is larger): a stop closer than that is moved out
    to it, and a strategy's own target is kept at least 1:min_reward_risk
    from the widened stop. '' when nothing changed, else what was done."""
    floor_atr = float(cfg.get("risk.min_stop_atr", 0) or 0)
    floor_pct = float(cfg.get("risk.min_stop_pct", 0) or 0)
    entry = float(setup.entry_trigger or setup.indicators.close or 0.0)
    stop = float(setup.underlying_support or 0.0)
    if not (entry and stop) or (floor_atr <= 0 and floor_pct <= 0):
        return ""
    atr = float(getattr(setup.indicators, "atr", 0.0) or 0.0)
    floor = max(floor_atr * atr, floor_pct / 100.0 * entry)
    risk = abs(entry - stop)
    if floor <= 0 or risk >= floor:
        return ""
    long = setup.direction is Direction.LONG
    sign = 1.0 if long else -1.0
    setup.underlying_support = round(entry - sign * floor, 4)
    need = float(cfg.get("risk.min_reward_risk", 3.0) or 0.0)
    own = float(setup.underlying_target or 0.0)
    if own and need and (own - entry) * sign < need * floor:
        setup.underlying_target = round(entry + sign * need * floor, 4)
    return (f"stop widened from {stop:,.2f} to {setup.underlying_support:,.2f} "
            f"(floor {floor:,.2f}: max {floor_atr:g}x ATR, {floor_pct:g}% of the price)")


def _snap(cfg: Any, room_r: float, need: float, price: float, long: bool,
          entry: float, risk: float, where: str) -> tuple[float, float, str] | None:
    """Target snapping (risk.target_snap): with room_r between min_r and the
    1:need target, take the trade with the target `ticks` inside the level."""
    snap = cfg.get("risk.target_snap") or {}
    if not snap.get("enabled", False) or room_r < float(snap.get("min_r", 2.2)):
        return None
    tick = float(cfg.get("strategies.pd_liquidity_sweep.tick", 0.01) or 0.01)
    target = price - int(snap.get("ticks", 2)) * tick if long \
        else price + int(snap.get("ticks", 2)) * tick
    r = (target - entry) * (1 if long else -1) / risk
    return (round(target, 4), round(r, 2),
            f"target snapped {int(snap.get('ticks', 2))} ticks inside the {where} → "
            f"{target:,.2f} ({r:.2f}R; {room_r:.1f}R of room, under 1:{need:g})")


def project(setup: Any, levels: Any, cfg: Any,
            sweep: bool = False) -> tuple[float, float, str, str]:
    """(target, reward:risk, why not, note). `why not` is '' when it passes.

    `sweep`: a Previous Day Liquidity Sweep. Its stop is the sweep wick and
    its target VWAP or 3R; the far side of yesterday's range is where a
    failed breakout rotates to, so the room check does not apply."""
    need = float(cfg.get("risk.min_reward_risk", 3.0) or 0.0)
    # The volatility breakout may skip the room check too
    # (strategies.volatility_breakout.room_check: false): it is meant to run
    # through the opening-range and previous-day highs/lows.
    if setup.strategy is SetupType.VOLATILITY_BREAKOUT and not bool(
            cfg.get("strategies.volatility_breakout.room_check", True)):
        sweep = True
    # A swing setup's exit is the first profitable open, not a target, so
    # the same-day room check never applies to it.
    if getattr(setup, "swing", False):
        sweep = True
    entry = float(setup.entry_trigger or setup.indicators.close)
    stop = float(setup.underlying_support or 0.0)
    risk = abs(entry - stop)
    if need <= 0:
        own = float(setup.underlying_target or 0.0)
        return own, (abs(own - entry) / risk if risk and own else 0.0), "", ""
    if risk <= 0:
        return 0.0, 0.0, "no distance between the entry and the invalidation level", ""
    long = setup.direction is Direction.LONG
    sign = 1.0 if long else -1.0
    projected = entry + sign * need * risk

    # The nearest opposing level in the trade's direction.
    names = cfg.get("risk.reward_room_levels") or ["previous_day", "opening_range", "premarket"]
    blockers: list[tuple[float, str, float]] = []
    for group in names:
        for attr, label in _ROOM.get(str(group), ()):
            price = float(getattr(levels, attr, 0.0) or 0.0)
            if price and ((price > entry + risk * 0.25) if long else (price < entry - risk * 0.25)):
                blockers.append((abs(price - entry), f"{label} {price:,.2f}", price))
    room_r = (float("inf") if sweep
              else min((d / risk for d, _, _ in blockers), default=float("inf")))
    if room_r < need:
        _, where, level = min(blockers)
        snapped = _snap(cfg, room_r, need, level, long, entry, risk, where)
        if snapped:
            return snapped[0], snapped[1], "", snapped[2]
        return 0.0, room_r, (f"only {room_r:.1f}R of room before the {where} — the "
                             f"1:{need:g} target ({projected:,.2f}) sits beyond it"), ""

    own = float(setup.underlying_target or 0.0)
    own_r = (own - entry) * sign / risk if own else 0.0
    # Strategies whose 1:3 must be VERIFIED by their own target (the POC for
    # a value-area rejection, VWAP-or-3R for the PD sweep) — a projected 3R
    # past a nearer natural target is not accepted for them.
    strict = {str(s).lower() for s in (cfg.get("risk.own_target_strategies") or [])}
    if strict & {setup.strategy.name.lower(), str(setup.strategy.value).lower()}:
        # 0.01R of slack: the strategy rounds its stop and target to 4
        # decimals, and an exact 3R target read 2.9999R and was refused as
        # "only 3.0R ... needs 1:3".
        if own_r + _SLACK < need:
            return 0.0, round(own_r, 2), (
                f"the strategy's own target {own:,.2f} is only {own_r:.1f}R on a risk "
                f"of {risk:,.2f} — {setup.strategy.value} needs a verified "
                f"1:{need:g}"), ""
        if own_r > room_r:
            _, where, level = min(blockers)
            snapped = _snap(cfg, room_r, need, level, long, entry, risk, where)
            if snapped:
                return snapped[0], snapped[1], "", snapped[2]
            return 0.0, room_r, (f"only {room_r:.1f}R of room before the {where}, short "
                                 f"of its own {own_r:.1f}R target {own:,.2f}"), ""
    if own_r + _SLACK >= need and own_r <= room_r:
        return own, round(own_r, 2), "", f"target {own:,.2f} = {own_r:.1f}R (the strategy's own)"
    return (round(projected, 4), need, "",
            f"target {projected:,.2f} = 1:{need:g} on a risk of {risk:,.2f} to "
            f"{stop:,.2f}" + ("" if room_r == float("inf") else f"; {room_r:.1f}R of room"))
