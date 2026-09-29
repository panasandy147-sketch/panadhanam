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


def check(setup: Any, levels: Any, oi: Any, cfg: Any) -> tuple[str, list[str]]:
    """('' , confirmations) when the setup passes; (why not, []) otherwise."""
    if not applies(setup, cfg):
        return "", []
    long = setup.direction is Direction.LONG
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
