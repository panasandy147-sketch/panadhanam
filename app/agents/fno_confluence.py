"""Previous-day F&O confluence and the 1:3 room check — the risk desk's gates.

  previous_day()      PDH / PDL / PDC (and the close before, for the day's
                      change) from the daily bars, or the 5-minute tape.
  picture()           that, plus call/put open interest and their change since
                      the previous close (NSE's changeinOpenInterest), and the
                      build-up: Long Buildup (price up, OI up), Short Buildup
                      (down, up), Short Covering (up, down), Long Unwinding
                      (down, down). Stored once a day per symbol (fno_daily).
  confluence_reason() A trade DRIVEN by a reversal pattern (tweezer / double
                      rejection / hammer / engulfing / star, in the trade's
                      direction) is taken only
                        bullish: after a sweep-and-reject of the PDL, with
                                 call open interest rising;
                        bearish: after a test-and-reject of the PDH, with
                                 put open interest rising.
                      No open interest in the chain: fno_confluence.
                      when_oi_unknown decides (block by default).
                      By REGIME (fno_confluence.regime_rules):
                        rangebound (and volatile): the pattern's extreme
                                 within rangebound_proximity_pct (0.25%) of
                                 the PDL (longs) / PDH (shorts), rejected;
                        trending_up long / trending_down short: no PDH/PDL
                                 needed — a PULLBACK within
                                 trend_pullback_pct (0.30%) of the intraday
                                 VWAP, the session POC or the 9/20 EMA, with
                                 the close back on the trend side.
                      A reversal AGAINST the trend keeps the PDH/PDL rule.
  room_snap()         The 1:3 target must have open road to the PDH (long) /
                      PDL (short). 3R or more of room: the 3R target. From
                      risk.target_snap.min_r (2.2R) to 3R: approved, with the
                      target snapped `ticks` (2) inside the level. Less: refused.
  room_reason()       room_snap()'s refusal alone.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.core.models import AgentReport, Bias


def _local_date(ts: Any, tz: str) -> date:
    from zoneinfo import ZoneInfo
    try:
        return ts.astimezone(ZoneInfo(tz)).date() if ts.tzinfo else ts.date()
    except Exception:                                   # noqa: BLE001
        return ts.date()


def previous_day(candles: dict[str, list[Any]], tz: str, today: date) -> dict[str, Any]:
    """The last completed session's high, low and close — from '1d' bars when
    fetched, else from the intraday tape."""
    daily = [c for c in candles.get("1d") or [] if _local_date(c.ts, tz) < today]
    if daily:
        prev = daily[-1]
        before = daily[-2].close if len(daily) > 1 else 0.0
        return {"day": _local_date(prev.ts, tz).isoformat(), "high": float(prev.high),
                "low": float(prev.low), "close": float(prev.close),
                "close_before": float(before)}
    for tf in ("5m", "15m", "1m"):
        bars = [c for c in candles.get(tf) or [] if _local_date(c.ts, tz) < today]
        if not bars:
            continue
        last_day = _local_date(bars[-1].ts, tz)
        session = [c for c in bars if _local_date(c.ts, tz) == last_day]
        earlier = [c for c in bars if _local_date(c.ts, tz) < last_day]
        return {"day": last_day.isoformat(), "high": max(c.high for c in session),
                "low": min(c.low for c in session), "close": float(session[-1].close),
                "close_before": float(earlier[-1].close) if earlier else 0.0}
    return {}


def buildup(price_change: float | None, oi_change: float | None) -> str:
    if price_change is None or oi_change is None:
        return "unknown"
    if price_change == 0 or oi_change == 0:
        return "neutral"
    if price_change > 0:
        return "Long Buildup" if oi_change > 0 else "Short Covering"
    return "Short Buildup" if oi_change > 0 else "Long Unwinding"


def picture(symbol: str, prev: dict[str, Any], deriv: dict[str, Any] | None) -> dict[str, Any]:
    deriv = deriv or {}
    known = bool(deriv.get("oi_known"))
    price = None
    if prev.get("close") and prev.get("close_before"):
        price = round((prev["close"] - prev["close_before"]) / prev["close_before"] * 100, 2)
    total = ((deriv.get("call_oi_change", 0) or 0) + (deriv.get("put_oi_change", 0) or 0)
             if known else None)
    return {"symbol": symbol, "pdh": prev.get("high"), "pdl": prev.get("low"),
            "pdc": prev.get("close"), "price_change_pct": price,
            "call_oi": deriv.get("call_oi") if known else None,
            "put_oi": deriv.get("put_oi") if known else None,
            "call_oi_change": deriv.get("call_oi_change") if known else None,
            "put_oi_change": deriv.get("put_oi_change") if known else None,
            "bias": buildup(price, total), "oi_known": known}


def reversal_pattern(reports: list[AgentReport], bias: Bias) -> str:
    """The reversal pattern driving this trade, or '' when none is."""
    from app.indicators.patterns import REVERSALS
    want = 1 if bias == Bias.BULLISH else -1
    for r in reports:
        if r.agent_id != "candlestick":
            continue
        for name in (r.extra or {}).get("patterns") or []:
            if name in REVERSALS[want]:
                return name
    return ""


def confluence_reason(ctx: Any, bias: Bias, reports: list[AgentReport], cfg: Any) -> str:
    """'' when the trade may go ahead, else why the confluence rule refuses it."""
    if not bool(cfg.get("fno_confluence.enabled", False)):
        return ""
    pattern = reversal_pattern(reports, bias)
    if not pattern:
        return ""
    # A confirmed Previous Day Liquidity Sweep IS the level condition; it
    # needs rising OI too only if pd_sweep.require_rising_oi says so.
    from app.strategies.pd_sweep import SETUP_NAME
    if any(r.agent_id == "candlestick" and (r.extra or {}).get("setup") == SETUP_NAME
           for r in reports) and not bool(cfg.get("pd_sweep.require_rising_oi", False)):
        return ""
    ind = ctx.indicators or {}
    prev = ind.get("previous_day") or {}
    primary = ind.get("primary") or {}
    long = bias == Bias.BULLISH
    close = float(primary.get("last_close") or (ctx.quote.last_price if ctx.quote else 0.0))
    candles = (ctx.candles or {}).get(str(cfg.get("technical.primary_timeframe", "5m"))) or []
    recent = candles[-int(cfg.get("fno_confluence.lookback_bars", 3)):]
    regime = str(primary.get("regime") or getattr(getattr(ctx, "regime", None), "value", "")
                 or "")
    regime_rules = bool(cfg.get("fno_confluence.regime_rules", False))
    if regime_rules and ((long and regime == "trending_up")
                         or (not long and regime == "trending_down")):
        return _pullback_reason(ctx, pattern, long, regime, close, recent, reports, cfg)
    level = prev.get("low" if long else "high")
    name = "previous-day low (PDL)" if long else "previous-day high (PDH)"
    if not level:
        return f"{pattern}: no {name} to confirm against"
    atr = float(primary.get("atr") or 0.0)
    if regime_rules:
        # Rangebound (or a reversal against the trend): at the level, within
        # rangebound_proximity_pct of it (or through it).
        tol = level * float(cfg.get("fno_confluence.rangebound_proximity_pct", 0.25)) / 100
    else:
        tol = max(atr * float(cfg.get("fno_confluence.touch_atr", 0.15)), close * 0.0005)
    if long:
        extreme = min((c.low for c in recent), default=close)
        touched, held = extreme <= level + tol, close > level
        verb = "swept and rejected"
    else:
        extreme = max((c.high for c in recent), default=close)
        touched, held = extreme >= level - tol, close < level
        verb = "tested and rejected"
    if not (touched and held):
        return (f"{pattern} is not at the {name} {level:,.2f} (extreme {extreme:,.2f}, "
                f"close {close:,.2f}) — reversals are taken only where the {name} is {verb}")
    deriv = ind.get("derivatives") or {}
    side = "call" if long else "put"
    if not deriv.get("oi_known"):
        if str(cfg.get("fno_confluence.when_oi_unknown", "block")).lower() == "allow":
            return ""
        return (f"{pattern} at the {name}, but {side} open interest is unknown (no "
                f"real chain) — the rule needs rising {side} OI")
    change = float(deriv.get(f"{side}_oi_change") or 0)
    if change <= 0:
        return (f"{pattern} at the {name}, but {side} OI is not rising "
                f"({change:+,.0f} since the previous close)")
    return ""


def _pullback_reason(ctx: Any, pattern: str, long: bool, regime: str, close: float,
                     recent: list[Any], reports: list[AgentReport], cfg: Any) -> str:
    """A reversal pattern WITH the trend: valid on a pullback to the intraday
    VWAP, the session POC or the 9/20 EMA (within trend_pullback_pct), the
    close back on the trend side. No PDH/PDL, no open-interest condition."""
    primary = (ctx.indicators or {}).get("primary") or {}
    levels: dict[str, float] = {}
    for key, label in (("vwap", "VWAP"), ("ema9", "9 EMA"), ("ema20", "20 EMA")):
        if primary.get(key):
            levels[label] = float(primary[key])
    if "20 EMA" not in levels and primary.get("ema21"):
        levels["20 EMA"] = float(primary["ema21"])
    for r in reports:
        profiles = (r.extra or {}).get("profiles") if r.agent_id == "volume_profile" else None
        if profiles:
            prof = profiles.get("current") or profiles.get("prior") or {}
            if prof.get("poc"):
                levels["session POC"] = float(prof["poc"])
    if not levels:
        return f"{pattern} in a {regime} tape, but there is no VWAP / POC / EMA to pull back to"
    pct = float(cfg.get("fno_confluence.trend_pullback_pct", 0.30))
    extreme = (min((c.low for c in recent), default=close) if long
               else max((c.high for c in recent), default=close))
    near = []
    for label, lvl in levels.items():
        gap = abs(extreme - lvl) / lvl * 100 if lvl else 99.0
        on_side = close > lvl if long else close < lvl
        if gap <= pct and on_side:
            return ""
        near.append(f"{label} {lvl:,.2f} ({gap:.2f}% away{'' if on_side else ', wrong side'})")
    return (f"{pattern} in a {regime} tape is taken on a pullback within {pct:g}% of the "
            f"VWAP, session POC or 9/20 EMA with the close back on the trend side — "
            f"{'long' if long else 'short'} extreme {extreme:,.2f}, close {close:,.2f}: "
            + "; ".join(near))


def room_snap(ctx: Any, bias: Bias, entry: float, stop: float, cfg: Any,
              tick: float = 0.01) -> tuple[str, float | None, float]:
    """(why refused, snapped underlying target or None, room in R).

    3R or more of room to the PDH (long) / PDL (short): the standard target
    (None). Between risk.target_snap.min_r (2.2R) and 3R: approved, the target
    snapped `ticks` inside the level. Less: refused."""
    need = float(cfg.get("risk.min_risk_reward", 3.0) or 0)
    if need <= 0 or not bool(cfg.get("fno_confluence.room_check", True)):
        return "", None, 0.0
    risk = abs(entry - stop)
    prev = (ctx.indicators or {}).get("previous_day") or {}
    if risk <= 0 or not prev:
        return "", None, 0.0
    long = bias == Bias.BULLISH
    level = prev.get("high" if long else "low")
    if not level:
        return "", None, 0.0
    ahead = (level - entry) if long else (entry - level)
    if ahead <= risk * 0.25:
        return "", None, 0.0                            # already through it
    room = ahead / risk
    if room >= need:
        return "", None, room
    label = "previous-day high" if long else "previous-day low"
    snap_min = float(cfg.get("risk.target_snap.min_r", 2.2) or 0)
    if bool(cfg.get("risk.target_snap.enabled", False)) and snap_min and room >= snap_min:
        ticks = int(cfg.get("risk.target_snap.ticks", 2))
        target = level - ticks * tick if long else level + ticks * tick
        return "", round(target, 4), round(((target - entry) if long else (entry - target))
                                           / risk, 3)
    floor = (f" (a target between {snap_min:g}R and {need:g}R is snapped inside it)"
             if bool(cfg.get("risk.target_snap.enabled", False)) else "")
    return (f"only {room:.1f}R of room before the {label} {level:,.2f} — the "
            f"1:{need:g} target sits beyond it{floor}"), None, room


def room_reason(ctx: Any, bias: Bias, entry: float, stop: float, cfg: Any) -> str:
    """'' when the 1:N target has open road on the underlying, else why not."""
    return room_snap(ctx, bias, entry, stop, cfg)[0]
