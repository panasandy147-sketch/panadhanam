"""Previous-day F&O confluence: where a reversal is allowed to be traded.

A 5-minute reversal is common; one at the level the institutions defended
yesterday, with fresh positioning behind it, is not. With `fno.confluence`
on, a reversal setup (Tweezer Bottom / swing-low reversal, and their mirror
images) passes only here:

  LONG_CALL   the pattern's low swept the Previous Day Low (at or through it,
              within `touch_atr` x ATR) and price closed back above it —
              AND call open interest is rising.
  LONG_PUT    the pattern's high tested the Previous Day High (at or through
              it, within `touch_atr` x ATR) and price closed back below it —
              AND put open interest is rising.

"Rising" is fno.read_oi(): intraday where the chain updates OI (NSE),
otherwise the previous session's change. With no OI in the chain (India on
estimated prices), `when_oi_unknown` decides: "block" (the default — the
rule is strict) or "allow" (judge on the price level alone, and say so).
"""
from __future__ import annotations

from typing import Any

from panaoptions.models import Direction


def applies(setup: Any, cfg: Any) -> bool:
    if not bool(cfg.get("fno.confluence.enabled", False)):
        return False
    wanted = {str(s).lower() for s in (cfg.get("fno.confluence.strategies")
                                       or ["candlestick_at_level", "liquidity_sweep"])}
    return (setup.strategy.name.lower() in wanted
            or str(setup.strategy.value).lower() in wanted)


def sweep_check(setup: Any, levels: Any, frame: Any, cfg: Any) -> tuple[str, dict | None]:
    """The two-candle sweep rule on the 5-minute tape: a candle swept the PDL
    (calls) / PDH (puts) by no more than `proximity_pct` (0.25%) and the next
    candle closed back inside yesterday's range. ('', found) or (why not, None)."""
    from panaoptions.engine.strategies import sweep_of_previous_day
    band = float(cfg.get("fno.confluence.proximity_pct", 0.25))
    found = sweep_of_previous_day(frame, levels, band,
                                  int(cfg.get("fno.confluence.lookback_bars", 3)),
                                  bool(cfg.get("strategies.pd_liquidity_sweep."
                                               "first_test_only", True)))
    want = 1 if setup.direction is Direction.LONG else -1
    name = "previous-day low (PDL)" if want > 0 else "previous-day high (PDH)"
    if found is None or found["direction"] != want:
        return (f"{setup.pattern or setup.strategy.value}: no sweep of the {name} within "
                f"{band:g}% with the next candle closing back inside yesterday's range"), None
    return "", found


def check(setup: Any, levels: Any, oi: Any, cfg: Any,
          frame: Any = None) -> tuple[str, list[str]]:
    """('' , confirmations) when the setup passes; (why not, []) otherwise.

    With `fno.confluence.mode: sweep` and the 5-minute frame, the level test
    is the two-candle sweep (sweep_check); otherwise the pattern's extreme
    within `touch_atr` of the level and a close back inside.
    """
    if not applies(setup, cfg):
        return "", []
    long = setup.direction is Direction.LONG
    if str(cfg.get("fno.confluence.mode", "sweep")) == "sweep" and frame is not None:
        why, found = sweep_check(setup, levels, frame, cfg)
        if why:
            return why, []
        what = "PDL" if long else "PDH"
        where = (f"swept the {what} {found['level']:,.2f} (wick {found['wick']:,.2f}) and "
                 f"the next candle closed back inside at {found['reclaim']:,.2f}")
        side = "call" if long else "put"
        rising = oi.calls_rising if long else oi.puts_rising
        if rising is None:
            if str(cfg.get("fno.confluence.when_oi_unknown", "block")).lower() == "allow":
                return "", [where, f"{side} OI unknown ({oi.line()}) — allowed on the "
                                   f"sweep alone (fno.confluence.when_oi_unknown)"]
            return (f"{where}, but {side} open interest is unknown ({oi.line()}) — the "
                    f"rule needs rising {side} OI"), []
        if not rising:
            return f"{where}, but {side} OI is not rising: {oi.line()}", []
        return "", [where, f"{side} OI rising: {oi.line()}", f"sweep wick {found['wick']}"]
    level = float(getattr(levels, "previous_low" if long else "previous_high", 0.0) or 0.0)
    name = "previous-day low (PDL)" if long else "previous-day high (PDH)"
    if level <= 0:
        return f"no {name} on the tape to confirm against", []
    atr = float(getattr(setup.indicators, "atr", 0.0) or 0.0)
    close = float(setup.indicators.close)
    tol = max(atr * float(cfg.get("fno.confluence.touch_atr", 0.15)), close * 0.0005)
    extreme = float(setup.underlying_support or 0.0)       # the pattern's low / high
    if long:
        touched = extreme and extreme <= level + tol
        held = close > level
        where = (f"the pattern's low {extreme:,.2f} swept the {name} {level:,.2f} "
                 f"and price closed back above it ({close:,.2f})")
        miss = (f"{setup.pattern or setup.strategy.value} is not at the {name} "
                f"{level:,.2f}: low {extreme:,.2f}, close {close:,.2f} — calls are "
                f"taken only on a sweep-and-reject of the PDL")
    else:
        touched = extreme and extreme >= level - tol
        held = close < level
        where = (f"the pattern's high {extreme:,.2f} tested the {name} {level:,.2f} "
                 f"and price closed back below it ({close:,.2f})")
        miss = (f"{setup.pattern or setup.strategy.value} is not at the {name} "
                f"{level:,.2f}: high {extreme:,.2f}, close {close:,.2f} — puts are "
                f"taken only on a test-and-reject of the PDH")
    if not (touched and held):
        return miss, []

    side = "call" if long else "put"
    rising = oi.calls_rising if long else oi.puts_rising
    if rising is None:
        if str(cfg.get("fno.confluence.when_oi_unknown", "block")).lower() == "allow":
            return "", [where, f"{side} OI unknown ({oi.line()}) — allowed on the "
                               f"price level alone (fno.confluence.when_oi_unknown)"]
        return (f"{where}, but {side} open interest is unknown ({oi.line()}) — the "
                f"rule needs rising {side} OI"), []
    if not rising:
        return f"{where}, but {side} OI is not rising: {oi.line()}", []
    return "", [where, f"{side} OI rising: {oi.line()}"]
