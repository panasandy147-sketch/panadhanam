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
  room_reason()       The 1:3 target must have open road: no PDH (long) or
                      PDL (short) inside 3R of the underlying entry.
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
    level = prev.get("low" if long else "high")
    name = "previous-day low (PDL)" if long else "previous-day high (PDH)"
    if not level:
        return f"{pattern}: no {name} to confirm against"
    close = float(primary.get("last_close") or (ctx.quote.last_price if ctx.quote else 0.0))
    atr = float(primary.get("atr") or 0.0)
    tol = max(atr * float(cfg.get("fno_confluence.touch_atr", 0.15)), close * 0.0005)
    candles = (ctx.candles or {}).get(str(cfg.get("technical.primary_timeframe", "5m"))) or []
    recent = candles[-int(cfg.get("fno_confluence.lookback_bars", 3)):]
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


def room_reason(ctx: Any, bias: Bias, entry: float, stop: float, cfg: Any) -> str:
    """'' when the 1:N target has open road on the underlying, else why not."""
    need = float(cfg.get("risk.min_risk_reward", 3.0) or 0)
    if need <= 0 or not bool(cfg.get("fno_confluence.room_check", True)):
        return ""
    risk = abs(entry - stop)
    prev = (ctx.indicators or {}).get("previous_day") or {}
    if risk <= 0 or not prev:
        return ""
    long = bias == Bias.BULLISH
    level = prev.get("high" if long else "low")
    if not level:
        return ""
    ahead = (level - entry) if long else (entry - level)
    if ahead <= risk * 0.25:
        return ""                                   # already through it
    room = ahead / risk
    if room < need:
        label = "previous-day high" if long else "previous-day low"
        return (f"only {room:.1f}R of room before the {label} {level:,.2f} — the "
                f"1:{need:g} target sits beyond it")
    return ""
