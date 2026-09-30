"""Institutional guardrails the Risk Management Officer (agents/risk.py) enforces.

Pure, deterministic helpers — no model, no prompt, no network — so every rule
here is a number that can be read, tested and audited:

  CooldownBlacklist     a symbol that has just closed a trade is blacklisted
                        for `risk.reentry_cooldown_minutes` (60). Stops the
                        desk stacking sequential positions on one ticker
                        during a choppy session (the XOM / AVGO pattern).
  daily_loss_limit      the hard circuit breaker: `risk.max_daily_loss_pct`
                        of capital (3% = $120 on $4,000), realised + open;
                        saved, so it holds for the day across restarts.
  spread_rejection      an option whose bid-ask spread is wider than
                        `risk.max_spread_pct_of_mid` (7%) of the mid is refused.
  deployment_cap        premium per trade: `risk.max_capital_deployed_pct`
                        (20% = $800), flexed to `risk.index_max_capital_
                        deployed_pct` (25% = $1,000) for the high-notional
                        index ETFs (SPY, QQQ, DIA — "DJI" means DIA).
  underlying_stop       the stop lives on the UNDERLYING's structure — the
                        5-minute swing low/high plus 2 ticks, or 1.5x ATR when
                        there is no usable swing — never on option premium.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_INDEX_SYMBOLS = ("SPY", "QQQ", "DIA")


# --------------------------------------------------------------------------- #
# 1. Anti-stacking: the cooldown blacklist
# --------------------------------------------------------------------------- #
class CooldownBlacklist:
    """Symbols that closed a trade recently and may not be re-entered yet.

    Held in memory and backed by the database (`loader`, the symbol's last
    exit time), so a restart does not wipe the blacklist and let the desk buy
    straight back into the name it just left.
    """

    def __init__(self, minutes: float = 60.0,
                 loader: Callable[[str], str | None] | None = None) -> None:
        self.minutes = float(minutes)
        self._loader = loader
        self._closed: dict[str, datetime] = {}

    def add(self, symbol: str, closed_at: datetime | None = None) -> None:
        """Blacklist `symbol` from `closed_at` (now by default)."""
        self._closed[symbol.upper()] = closed_at or datetime.now()

    def _last_close(self, symbol: str) -> datetime | None:
        """The later of what this process saw close and what the database holds.

        The database is re-read every time (one indexed query): a close booked
        by another path — a restart, the outcome tracker without a risk
        manager attached — must still start the clock.
        """
        latest = self._closed.get(symbol.upper())
        if self._loader is not None:
            try:
                stamp = self._loader(symbol)
                loaded = datetime.fromisoformat(str(stamp)) if stamp else None
            except Exception:                          # noqa: BLE001 - db absent
                loaded = None
            if loaded is not None:
                if latest is not None and (loaded.tzinfo is None) != (latest.tzinfo is None):
                    loaded = loaded.replace(tzinfo=latest.tzinfo)
                if latest is None or loaded > latest:
                    latest = loaded
        return latest

    def minutes_left(self, symbol: str, now: datetime | None = None) -> float:
        """Minutes until `symbol` may be traded again; 0 when it is free."""
        if self.minutes <= 0:
            return 0.0
        closed = self._last_close(symbol)
        if closed is None:
            return 0.0
        now = now or datetime.now()
        if closed.tzinfo is not None and now.tzinfo is None:
            closed = closed.replace(tzinfo=None)
        elif closed.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        waited = (now - closed).total_seconds() / 60.0
        if waited < 0:
            return 0.0
        return round(max(0.0, self.minutes - waited), 1)

    def blocked(self, symbol: str, now: datetime | None = None) -> bool:
        return self.minutes_left(symbol, now) > 0

    def reason(self, symbol: str, now: datetime | None = None) -> str:
        """Why `symbol` is blocked, or "" when it is free."""
        left = self.minutes_left(symbol, now)
        if left <= 0:
            return ""
        return (f"{symbol} is on the {self.minutes:g}-minute cooldown blacklist — it "
                f"closed a trade {self.minutes - left:.0f} min ago; no re-entry for "
                f"another {left:.0f} min (anti-stacking)")

    def active(self, now: datetime | None = None) -> dict[str, float]:
        """Every symbol currently blacklisted, with minutes left."""
        out = {}
        for symbol in list(self._closed):
            left = self.minutes_left(symbol, now)
            if left > 0:
                out[symbol] = left
        return out


# --------------------------------------------------------------------------- #
# 2. The daily hard-loss circuit breaker
# --------------------------------------------------------------------------- #
def daily_loss_limit(cfg: Any, capital: float) -> float:
    """The day's loss budget in currency: 3% of $4,000 = $120."""
    return round(capital * float(cfg.get("risk.max_daily_loss_pct", 10.0)) / 100.0, 2)


def breaker_tripped(daily_pnl: float, limit: float) -> bool:
    """True once realised + open P&L reaches -limit."""
    return limit > 0 and daily_pnl <= -limit


# --------------------------------------------------------------------------- #
# 3. Spread
# --------------------------------------------------------------------------- #
def spread_pct(bid: float | None, ask: float | None) -> float | None:
    """Bid-ask spread as a percentage of the mid, or None without a quote."""
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return round((ask - bid) / mid * 100.0, 2)


def spread_rejection(cfg: Any, bid: float | None, ask: float | None,
                     label: str = "the contract") -> str:
    """A refusal when the spread is wider than the limit, else "".

    A leg with no two-sided quote cannot be measured. It is refused only when
    `risk.reject_unquoted_spread` is true; otherwise it passes with the gap
    noted by the caller — several free feeds send no bid/ask at all.
    """
    limit = float(cfg.get("risk.max_spread_pct_of_mid", 7.0))
    pct = spread_pct(bid, ask)
    if pct is None:
        if bool(cfg.get("risk.reject_unquoted_spread", False)):
            return f"{label} has no two-sided quote — the spread cannot be checked"
        return ""
    if pct > limit:
        return (f"{label} bid-ask spread is {pct:.1f}% of the mid, over the "
                f"{limit:g}% limit — the fill alone would cost too much")
    return ""


# --------------------------------------------------------------------------- #
# 4. Capital deployment, with the index flex
# --------------------------------------------------------------------------- #
def index_symbols(cfg: Any) -> set[str]:
    """High-notional index ETFs that get the larger cap. "DJI" means DIA."""
    names = {str(s).upper() for s in (cfg.get("risk.index_symbols")
                                      or DEFAULT_INDEX_SYMBOLS)}
    if names & {"DJI", "^DJI"}:
        names.add("DIA")
    return names


def deployment_cap_pct(cfg: Any, symbol: str) -> float:
    """20% of capital per trade, or 25% on SPY / QQQ / DIA."""
    standard = float(cfg.get("risk.max_capital_deployed_pct", 20.0))
    if symbol.upper() in index_symbols(cfg):
        return max(standard, float(cfg.get("risk.index_max_capital_deployed_pct", 25.0)))
    return standard


def deployment_cap(cfg: Any, capital: float, symbol: str) -> float:
    """The most one trade on `symbol` may spend on premium, in currency."""
    return round(capital * deployment_cap_pct(cfg, symbol) / 100.0, 2)


# --------------------------------------------------------------------------- #
# 5. Stops on the underlying's structure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UnderlyingStop:
    """Where the idea is wrong, on the underlying, and how that was chosen."""
    level: float
    method: str          # "structure", "swing", "atr" or "percent"
    note: str


def _swing(candles: list[Any], bullish: bool, lookback: int) -> float | None:
    """The 5-minute swing extreme over the last `lookback` completed bars."""
    seq = list(candles or [])
    # Completed bars only: the last one is still forming.
    bars = seq[-(lookback + 1):-1] or seq[-lookback:]
    if not bars:
        return None
    return (min(float(c.low) for c in bars) if bullish
            else max(float(c.high) for c in bars))


def _sane(level: float, spot: float, bullish: bool, atr: float) -> bool:
    if level <= 0 or spot <= 0:
        return False
    if bullish and level >= spot:
        return False
    if not bullish and level <= spot:
        return False
    distance = abs(spot - level)
    if distance / spot > 0.05:
        return False
    return not (atr > 0 and distance > 5 * atr)


def stop_floor(cfg: Any, price: float, atr: float) -> tuple[float, str]:
    """The nearest a stop may sit: max(risk.min_stop_atr x ATR,
    risk.min_stop_pct % of the price). (0.0, '') when both are off."""
    floor_atr = float(cfg.get("risk.min_stop_atr", 0) or 0)
    floor_pct = float(cfg.get("risk.min_stop_pct", 0) or 0)
    by_atr = floor_atr * atr if atr > 0 else 0.0
    by_pct = floor_pct / 100.0 * price if price > 0 else 0.0
    if by_atr <= 0 and by_pct <= 0:
        return 0.0, ""
    if by_pct > by_atr:
        return by_pct, f"{floor_pct:g}% of the price ({price:,.2f})"
    return by_atr, f"{floor_atr:g}x ATR ({atr:.2f})"


def underlying_stop(cfg: Any, *, spot: float, bullish: bool, atr: float,
                    candles_5m: list[Any] | None = None, tick: float = 0.01,
                    named_level: float | None = None) -> UnderlyingStop:
    """The stop on the UNDERLYING, in this order of preference:

      1. a structural level an analyst named (the candlestick's invalidation),
      2. the 5-minute swing low (long) / high (short) over the last
         `risk.swing_lookback_bars` bars,
         either one placed `risk.structural_stop_ticks` (2) ticks BEYOND it,
      3. `risk.atr_stop_multiplier` (1.5) x ATR from the price,
      4. a percentage fallback when there is no ATR either.

    Then never inside the noise: at least max(`risk.min_stop_atr` x ATR,
    `risk.min_stop_pct` % of the price) away (stop_floor).
    """
    ticks = int(cfg.get("risk.structural_stop_ticks", 2))
    offset = float(tick or 0.01) * ticks
    atr_mult = float(cfg.get("risk.atr_stop_multiplier", 1.5))
    lookback = int(cfg.get("risk.swing_lookback_bars", 6))

    stop: UnderlyingStop | None = None
    for method, level in (("structure", named_level),
                          ("swing", _swing(candles_5m or [], bullish, lookback))):
        if level and _sane(float(level), spot, bullish, atr):
            placed = float(level) - offset if bullish else float(level) + offset
            what = ("named structural level" if method == "structure"
                    else f"5m swing {'low' if bullish else 'high'}")
            stop = UnderlyingStop(round(placed, 4), method,
                                  f"{what} {float(level):.2f} ± {ticks} ticks → {placed:.2f}")
            break
    if stop is None and atr > 0:
        placed = spot - atr_mult * atr if bullish else spot + atr_mult * atr
        stop = UnderlyingStop(round(placed, 4), "atr",
                              f"{atr_mult:g}x ATR ({atr:.2f}) from {spot:.2f} → {placed:.2f}")
    if stop is None:
        pct = float(cfg.get("risk.max_stop_distance_pct", 3.0)) / 3
        placed = spot * (1 - pct / 100) if bullish else spot * (1 + pct / 100)
        stop = UnderlyingStop(round(placed, 4), "percent",
                              f"{pct:.2f}% fallback → {placed:.2f}")

    floor, what = stop_floor(cfg, spot, atr)
    if floor > 0 and abs(spot - stop.level) < floor:
        placed = spot - floor if bullish else spot + floor
        stop = UnderlyingStop(round(placed, 4), stop.method,
                              stop.note + f", widened to {what} — inside normal noise")
    return stop


def premium_at_stop(premium: float, delta: float | None, spot: float,
                    level: float, is_call: bool) -> float:
    """The option's mark when the underlying reaches `level`.

    The same delta approximation the outcome tracker marks positions with, so
    the stop, the R-multiple and the exit all agree.
    """
    d = abs(delta if delta is not None else 0.5) or 0.5
    move = (level - spot) * d * (1 if is_call else -1)
    return max(round(premium + move, 4), 0.01)


def underlying_stop_hit(spot: float, level: float, is_call_or_long: bool) -> bool:
    """Has the underlying crossed its stop? Calls/longs below, puts/shorts above."""
    return spot <= level if is_call_or_long else spot >= level
