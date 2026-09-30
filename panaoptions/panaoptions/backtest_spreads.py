"""Backtest: would the blocked setups have filled as debit spreads?

    python run.py --backtest-spreads                 last 5 blocked setups
    python run.py --backtest-spreads --limit 20      more of them
    python run.py --backtest-spreads --symbols SPY,QQQ,NVDA --days 5
                                                     any symbols, recent sessions
    python run.py --backtest-signals --symbols SPY,QQQ --days 5
                                                     replay the scanner bar by bar:
                                                     every LONG_CALL and LONG_PUT
                                                     setup, how it would execute

Two sources of setups:

  the desk's own log   every setup the desk refused for want of an affordable
                       contract is in signals_seen ("over the $X budget",
                       "dearer than", "Nothing fits", "SKIPPED — hard risk").
  --symbols            for symbols with no log: each session's 10:00 bar in the
                       last --days, long, at today's per-trade budget.

For each one the chain AT THAT MOMENT is rebuilt: the underlying's price from
the historical 5-minute bars (Yahoo keeps 60 days), and its implied
volatility from what the desk recorded that day (iv_history), else 20-day
realised volatility x 1.10. Black-Scholes prices every strike and expiry in
the window with a typical spread. Then the SAME picker the desk runs today
decides: primary contract, shorter expiry, debit spread, 0.30-0.39 delta, or
skip. When it fills, the position is marked again at the session's last bar
before square-off, to show what it would have made.

Honest limits: free sources keep no historical option quotes, so the prices
are a model of the chain, not the chain. Open interest is unknown, so the
liquidity rule cannot be tested (the spread rule is). The result says whether
the ARCHITECTURE finds a tradeable structure for these setups; it is not a
fill report.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from panaoptions.data.greeks import delta as bs_delta
from panaoptions.data.greeks import price as bs_price
from panaoptions.engine import contracts
from panaoptions.logging import get_logger
from panaoptions.models import Direction, OptionContract, OptionRight, option_side

log = get_logger("backtest")

# Refused for PRICE: the contract, or every fallback, cost more than the
# budget or the per-contract ceiling. Not a committee vote, not the screen.
BLOCKED = re.compile(r"over the \S?[\d,]+(?:\.\d+)? budget|dearer than|Nothing fits|"
                     r"cannot both hold|SKIPPED — hard risk failure|"
                     r"over the \d+(?:\.\d+)?% cap|Not even one contract fits",
                     re.IGNORECASE)
DAILY_EXPIRY = {"SPY", "QQQ", "IWM"}


@dataclass
class Blocked:
    symbol: str
    ts: datetime
    direction: str                     # LONG / SHORT
    reason: str = ""
    budget: float = 0.0
    source: str = "log"
    pattern: str = ""
    strategy: str = ""
    stop: float = 0.0          # underlying invalidation (scan: the setup's)
    target: float = 0.0        # underlying target


@dataclass
class Result:
    symbol: str
    ts: str
    direction: str
    spot: float = 0.0
    iv: float = 0.0
    budget: float = 0.0
    naked: str = ""
    naked_cost: float = 0.0
    tier: str = ""                     # primary / shorter_expiry / debit_spread / secondary_delta / ""
    contract: str = ""
    cost: float = 0.0
    max_profit: float = 0.0
    exit_value: float | None = None
    pnl: float | None = None
    exit_at: str = ""                  # HH:MM the P&L was measured at
    session_over: bool = True          # False: measured mid-session, "so far"
    exit_reason: str = ""              # STOP / TARGET / SQUARE_OFF (scan replays)
    r_multiple: float | None = None
    note: str = ""
    was_blocked: str = ""
    legs: list[str] = field(default_factory=list)
    side: str = ""                     # LONG_CALL / LONG_PUT
    execution: str = ""                # OUTRIGHT_LONG_CALL ... / *_DEBIT_SPREAD / SKIPPED_HARD_RISK
    pattern: str = ""
    strategy: str = ""
    # For sizing in validate.py: the underlying stop and target, the chosen
    # contract's premium, (net) delta and lot, and when the trade ended.
    stop: float = 0.0
    target: float = 0.0
    mid: float = 0.0
    delta: float = 0.0
    lot: int = 0
    exit_ts: str = ""
    # The same fill under the R-multiple exit plan (risk.exit_style
    # "r_multiple"): per contract when half can be sold at +1.5R, and for a
    # single contract (breakeven + trail only); when the last piece closed.
    pnl_r: float | None = None
    pnl_r1: float | None = None
    exit_ts_r: str = ""
    exit_ts_r1: str = ""
    exit_reason_r: str = ""

    @property
    def filled(self) -> bool:
        return bool(self.tier)


# --------------------------------------------------------------------------- #
# Reading the log
# --------------------------------------------------------------------------- #
def blocked_from_log(limit: int = 5, since: str | None = None,
                     cooldown_minutes: int = 30) -> list[Blocked]:
    """The most recent setups the desk refused for want of an affordable
    contract. The desk re-logs a live setup every cycle; a repeat of the same
    symbol and side inside `cooldown_minutes` is the same setup, counted once."""
    from panaoptions.ledger import store

    q = ("SELECT ts, symbol, direction, reason, payload FROM signals_seen "
         "WHERE taken = 0")
    params: list[Any] = []
    if since:
        q += " AND ts >= ?"
        params.append(since)
    q += " ORDER BY ts DESC"
    out: list[Blocked] = []
    try:
        rows = store.get_conn().execute(q, params).fetchall()
    except Exception as exc:                         # noqa: BLE001
        log.warning("could not read the setup log: %s", exc)
        return []
    # Oldest first, so a setup re-logged every minute keeps its FIRST sighting.
    rows = list(reversed(rows))
    for ts, symbol, direction, reason, payload in rows:
        if not BLOCKED.search(str(reason or "")) or direction not in {"LONG", "SHORT"}:
            continue
        try:
            extra = json.loads(payload or "{}")
        except ValueError:
            extra = {}
        budget = float(extra.get("budget") or 0.0)
        if not budget:
            m = re.search(r"over the \S?([\d,]+(?:\.\d+)?) budget", str(reason))
            budget = float(m.group(1).replace(",", "")) if m else 0.0
        when = datetime.fromisoformat(str(ts))
        if any(b.symbol == str(symbol) and b.direction == str(direction)
               and abs((b.ts - when).total_seconds()) < cooldown_minutes * 60 for b in out):
            continue
        out.append(Blocked(symbol=str(symbol), ts=when, direction=str(direction),
                           reason=str(reason), budget=budget,
                           pattern=str(extra.get("pattern") or ""),
                           strategy=str(extra.get("strategy") or "")))
    return out[-limit:] if limit else out


def sessions_for(symbols: list[str], days: int, cfg: Any, now: datetime,
                 at: str = "10:00") -> list[Blocked]:
    """One long setup per symbol per recent session, at `at` exchange time."""
    tz = ZoneInfo(cfg.timezone)
    hh, mm = (int(x) for x in at.split(":"))
    out: list[Blocked] = []
    day = now.astimezone(tz).date()
    seen = 0
    while seen < days:
        day -= timedelta(days=1)
        if day.weekday() >= 5:
            continue
        seen += 1
        for s in symbols:
            out.append(Blocked(symbol=s.upper(), direction="LONG", source="symbols",
                               ts=datetime.combine(day, time(hh, mm), tzinfo=tz)))
    return sorted(out, key=lambda b: (b.ts, b.symbol))


# --------------------------------------------------------------------------- #
# Rebuilding the chain
# --------------------------------------------------------------------------- #
def realised_vol(daily: list[Any], before: date, days: int = 20) -> float:
    closes = [c.close for c in daily if c.ts.date() < before and c.close > 0][-(days + 1):]
    if len(closes) < 5:
        return 0.0
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0]
    if len(rets) < 4:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def strike_step(symbol: str, spot: float) -> float:
    if symbol.upper() in DAILY_EXPIRY:
        return 1.0
    # Near-the-money weeklies list $1 strikes on most liquid names under $250.
    for limit, step in ((25, 0.5), (250, 1.0), (500, 2.5), (1000, 5.0)):
        if spot < limit:
            return step
    return 10.0


def expiries(symbol: str, day: date, horizon: int) -> list[date]:
    out = []
    for i in range(horizon + 1):
        d = day + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        if symbol.upper() in DAILY_EXPIRY or d.weekday() == 4:
            out.append(d)
    return out


def opening_widen(cfg: Any, when: datetime) -> float:
    """How much wider quotes are than usual at `when`: `backtest.opening_widen`
    (2.5x) at the open, easing to 1x by the end of the opening window
    (contracts.opening_spread, else the first 45 minutes). Real opening
    candles quote wide; a model with one flat spread could never show what
    the 12% opening allowance changes."""
    peak = float(cfg.get("backtest.opening_widen", 2.5))
    if peak <= 1:
        return 1.0
    opening = cfg.get("contracts.opening_spread") or {}
    start = str(cfg.get("session.market_open", "09:30"))
    sh, sm = (int(x) for x in start.split(":"))
    end = str(opening.get("to") or "")
    if end:
        eh, em = (int(x) for x in end.split(":"))
    else:
        eh, em = divmod(sh * 60 + sm + 45, 60)
    t = when.hour * 60 + when.minute
    a, b = sh * 60 + sm, eh * 60 + em
    if t < a or t >= b or b <= a:
        return 1.0
    return peak - (peak - 1.0) * (t - a) / (b - a)


def _grid(symbol: str, spot: float, when: datetime, cfg: Any, max_dte: int):
    """(expiry dates, strike step, spread %) for the market."""
    if str(getattr(cfg, "market", "US")).upper() == "IN":
        from panaoptions.data import nse
        est = cfg.get("data.estimated") or {}
        index = symbol.upper() in {str(x).upper() for x in cfg.get("data.index_symbols") or []}
        exps = nse.expiries(symbol, when.date(),
                            {str(x).upper() for x in est.get("weekly_symbols") or ["NIFTY"]},
                            int(est.get("expiry_weekday", 1)), max_dte + 7)
        step = nse.strike_step(symbol, spot, {str(k).upper(): float(v) for k, v in
                                              (est.get("strike_steps") or {}).items()})
        spread = float(est.get("index_spread_pct" if index else "stock_spread_pct",
                               1.5 if index else 3.0))
        return exps, step, spread
    index = symbol.upper() in DAILY_EXPIRY
    spread = float(cfg.get("backtest.index_spread_pct" if index
                           else "backtest.stock_spread_pct", 3.0 if index else 5.0))
    return expiries(symbol, when.date(), max_dte), strike_step(symbol, spot), spread


def model_chain(symbol: str, spot: float, iv: float, when: datetime, cfg: Any,
                lot: int, strikes_each_side: int = 30) -> list[OptionContract]:
    """Every strike and expiry in the desk's window, priced by Black-Scholes,
    on the market's own expiry calendar and strike grid, with quotes widened
    in the opening minutes (opening_widen)."""
    min_dte = int(cfg.get("contracts.min_dte", 0))
    max_dte = int(cfg.get("contracts.max_dte", 14))
    exps, step, spread_pct = _grid(symbol, spot, when, cfg, max_dte)
    spread_pct *= opening_widen(cfg, when)
    atm = round(spot / step) * step
    close = cfg.get("session.market_close", "16:00")
    ch, cm = (int(x) for x in str(close).split(":"))
    out = []
    for exp in exps:
        dte = (exp - when.date()).days
        if not min_dte <= dte <= max_dte:
            continue
        expiry_at = datetime.combine(exp, time(ch, cm), tzinfo=when.tzinfo)
        days = max((expiry_at - when).total_seconds() / 86400.0, 1 / 24)
        for i in range(-strikes_each_side, strikes_each_side + 1):
            k = round(atm + i * step, 2)
            if k <= 0:
                continue
            for right in (OptionRight.CALL, OptionRight.PUT):
                call = right is OptionRight.CALL
                mid = bs_price(spot, k, days, iv, call)
                if mid < 0.05:
                    continue
                half = max(mid * spread_pct / 200.0, 0.01)
                out.append(OptionContract(
                    symbol=symbol, right=right, strike=k, expiry=exp.isoformat(), dte=dte,
                    bid=round(mid - half, 2), ask=round(mid + half, 2),
                    delta=round(bs_delta(spot, k, days, iv, call), 3),
                    implied_volatility=iv, multiplier=lot, estimated=True))
    return out


def value_at(c: OptionContract, spot: float, iv: float, when: datetime, cfg: Any) -> float:
    """The position's model value per share at `when`."""
    close = cfg.get("session.market_close", "16:00")
    ch, cm = (int(x) for x in str(close).split(":"))

    def one(leg: OptionContract) -> float:
        exp = datetime.combine(date.fromisoformat(leg.expiry), time(ch, cm),
                               tzinfo=when.tzinfo)
        days = max((exp - when).total_seconds() / 86400.0, 1 / 1440)
        return bs_price(spot, leg.strike, days, iv, leg.right is OptionRight.CALL)

    if c.is_spread:
        return max(one(c.long_leg) - one(c.short_leg), 0.0)
    return one(c)


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def _bar_at(bars: list[Any], when: datetime) -> Any | None:
    before = [b for b in bars if b.ts <= when]
    return before[-1] if before else None


def _exit_bar(bars: list[Any], when: datetime, cfg: Any) -> Any | None:
    tz = when.tzinfo
    hh, mm = (int(x) for x in str(cfg.get("session.force_exit_at", "15:45")).split(":"))
    last = datetime.combine(when.date(), time(hh, mm), tzinfo=tz)
    same_day = [b for b in bars if b.ts.astimezone(tz).date() == when.date()
                and when < b.ts <= last]
    return same_day[-1] if same_day else None


def _walk(bars: list[Any], when: datetime, cfg: Any, direction: Direction,
          stop: float, target: float) -> tuple[datetime, float, str] | None:
    """(time, underlying price, STOP|TARGET|SQUARE_OFF|LAST_BAR) — the first
    level the day's later bars touch. A bar touching both counts as the stop."""
    tz = when.tzinfo
    hh, mm = (int(x) for x in str(cfg.get("session.force_exit_at", "15:45")).split(":"))
    last = datetime.combine(when.date(), time(hh, mm), tzinfo=tz)
    path = [b for b in bars if b.ts.astimezone(tz).date() == when.date()
            and when <= b.ts.astimezone(tz) + timedelta(minutes=5) and b.ts.astimezone(tz) < last]
    long = direction is Direction.LONG
    for b in path:
        at = b.ts.astimezone(tz)
        if (b.low <= stop) if long else (b.high >= stop):
            return at, stop, "STOP"
        if (b.high >= target) if long else (b.low <= target):
            return at, target, "TARGET"
    if not path:
        return None
    b = path[-1]
    over = (b.ts.astimezone(tz) + timedelta(minutes=10)) >= last
    return b.ts.astimezone(tz), float(b.close), "SQUARE_OFF" if over else "LAST_BAR"


def _walk_r(bars: list[Any], when: datetime, cfg: Any, direction: Direction,
            entry: float, stop: float, split: bool
            ) -> list[tuple[datetime, float, float, str]]:
    """The R-multiple plan on the underlying: [(time, price, fraction, why)].

    Stop at the original stop; at +scale_out_r sell half (`split`) and move
    the stop to breakeven; then trail runner_trail_r behind the best level
    reached; whatever is left goes at the square-off. Within a bar the stop
    is checked first (the conservative reading of an OHLC bar)."""
    tz = when.tzinfo
    hh, mm = (int(x) for x in str(cfg.get("session.force_exit_at", "15:45")).split(":"))
    last = datetime.combine(when.date(), time(hh, mm), tzinfo=tz)
    path = [b for b in bars if b.ts.astimezone(tz).date() == when.date()
            and when <= b.ts.astimezone(tz) + timedelta(minutes=5)
            and b.ts.astimezone(tz) < last]
    risk = abs(entry - stop)
    if not path or risk <= 0:
        return []
    sign = 1.0 if direction is Direction.LONG else -1.0
    scale_r = float(cfg.get("risk.scale_out_r", 1.5) or 0.0) or float("inf")   # 0 = off
    trail_r = float(cfg.get("risk.runner_trail_r", 1.0))
    share = float(cfg.get("risk.take_profit_1_size_pct", 50.0)) / 100.0 if split else 0.0
    left, best, armed, cur_stop = 1.0, 0.0, False, stop
    legs: list[tuple[datetime, float, float, str]] = []
    for b in path:
        at = b.ts.astimezone(tz) + timedelta(minutes=5)
        worst = b.low if sign > 0 else b.high
        top = b.high if sign > 0 else b.low
        # The protective level for this bar: the stop, or the trail once armed.
        guard = cur_stop
        if armed:
            trail = entry + sign * (best - trail_r) * risk
            guard = max(guard, trail) if sign > 0 else min(guard, trail)
        if (worst - guard) * sign <= 0:
            why = "STOP" if not armed else ("BREAKEVEN" if guard == entry else "TRAIL")
            legs.append((at, guard, left, why))
            return legs
        best = max(best, (top - entry) * sign / risk)
        if not armed and best >= scale_r:
            armed, cur_stop = True, entry
            if share:
                legs.append((at, entry + sign * scale_r * risk, share, "TARGET_1"))
                left -= share
    b = path[-1]
    legs.append((b.ts.astimezone(tz) + timedelta(minutes=5), float(b.close), left,
                 "SQUARE_OFF"))
    return legs


async def replay(items: list[Blocked], feed: Any, cfg: Any,
                 budget_now: float | None = None) -> list[Result]:
    from panaoptions.ledger import store

    lot_default = int(cfg.multiplier)
    cache: dict[str, tuple[list[Any], list[Any]]] = {}
    out: list[Result] = []
    for b in items:
        tz = ZoneInfo(cfg.timezone)
        when = b.ts if b.ts.tzinfo else b.ts.replace(tzinfo=tz)
        when = when.astimezone(tz)
        r = Result(symbol=b.symbol, ts=when.isoformat(timespec="minutes"),
                   direction=b.direction, was_blocked=b.reason[:200],
                   side=option_side(Direction.LONG if b.direction == "LONG"
                                    else Direction.SHORT),
                   pattern=b.pattern, strategy=b.strategy)
        if b.symbol not in cache:
            try:
                intraday = await feed.candles(b.symbol, "5m")
                daily = await feed.candles(b.symbol, "1d")
            except Exception as exc:                 # noqa: BLE001
                intraday, daily = [], []
                r.note = f"no history: {exc}"
            cache[b.symbol] = (intraday or [], daily or [])
        intraday, daily = cache[b.symbol]
        bar = _bar_at(intraday, when)
        if bar is None or bar.ts.astimezone(tz).date() != when.date():
            r.note = r.note or ("no 5-minute bar for that session (Yahoo keeps 60 days "
                                "of intraday history)")
            out.append(r)
            continue
        r.spot = round(bar.close, 2)
        iv = 0.0
        try:
            row = store.get_conn().execute(
                "SELECT iv FROM iv_history WHERE symbol=? AND day=?",
                (b.symbol, when.date().isoformat())).fetchone()
            iv = float(row[0]) if row else 0.0
        except Exception:                            # noqa: BLE001
            iv = 0.0
        if iv <= 0:
            iv = realised_vol(daily, when.date()) * 1.10
        if iv <= 0:
            r.note = "no volatility history to price the chain"
            out.append(r)
            continue
        r.iv = round(iv, 4)
        lot = int(cfg.lot_size(b.symbol)) if hasattr(cfg, "lot_size") else lot_default
        chain = model_chain(b.symbol, bar.close, iv, when, cfg, lot)
        from panaoptions.risk.gatekeeper import cap_pct
        r.budget = round(b.budget or budget_now
                         or cfg.capital * cap_pct(cfg, b.symbol) / 100.0, 2)
        direction = Direction.LONG if b.direction == "LONG" else Direction.SHORT
        want = OptionRight.CALL if direction is Direction.LONG else OptionRight.PUT
        lo, hi = float(cfg.get("contracts.min_delta", 0.40)), float(cfg.get("contracts.max_delta", 0.50))
        naked = [c for c in chain if c.right is want and lo <= abs(c.delta) <= hi]
        if naked:
            n = min(naked, key=lambda c: (c.dte, abs(abs(c.delta) - (lo + hi) / 2)))
            r.naked, r.naked_cost = n.label, n.cost(lot)
        search = contracts.choose(b.symbol, chain, direction, cfg, budget=r.budget, now=when)
        if search.chosen is None:
            r.note = search.note[:300]
            r.execution = "SKIPPED_HARD_RISK"
            out.append(r)
            continue
        c = search.chosen
        from panaoptions.models import execution_kind
        r.tier, r.contract, r.cost = search.tier, c.label, c.cost(lot)
        r.execution = execution_kind(c, direction)
        r.stop, r.target, r.mid, r.lot = b.stop, b.target, c.mid, c.multiplier or lot
        r.delta = round(abs(c.long_leg.delta) - abs(c.short_leg.delta) if c.is_spread
                        else abs(c.delta), 4)
        # The same fill under the R-multiple exit plan, both ways: with half
        # sold at +1.5R, and as one contract (breakeven and trail only).
        if b.stop:
            lot_n = c.multiplier or lot
            for split in (True, False):
                legs = _walk_r(intraday, when, cfg, direction, float(bar.close), b.stop, split)
                if not legs:
                    continue
                pnl = round(sum(frac * (value_at(c, price, iv, at, cfg) - c.mid) * lot_n
                                for at, price, frac, _ in legs), 2)
                done = legs[-1][0].isoformat(timespec="minutes")
                if split:
                    r.pnl_r, r.exit_ts_r, r.exit_reason_r = pnl, done, legs[-1][3]
                else:
                    r.pnl_r1, r.exit_ts_r1 = pnl, done
        if c.is_spread:
            r.legs = [f"BUY {c.long_leg.label} ({abs(c.long_leg.delta):.2f}Δ)",
                      f"SELL {c.short_leg.label} ({abs(c.short_leg.delta):.2f}Δ)"]
            r.max_profit = round((c.width - c.mid) * (c.multiplier or lot), 2)
        # With the setup's own stop and target, walk the tape: out at the
        # first one touched, else at the square-off.
        if b.stop and b.target:
            hit = _walk(intraday, when, cfg, direction, b.stop, b.target)
            if hit is not None:
                at, price, reason = hit
                value = value_at(c, price, iv, at, cfg)
                r.exit_value = round(value, 4)
                lot_n = c.multiplier or lot
                r.pnl = round((value - c.mid) * lot_n, 2)
                r.r_multiple = round((value - c.mid) / c.mid, 2) if c.mid else None
                r.exit_at, r.exit_reason = (at + timedelta(minutes=5)).strftime("%H:%M"), reason
                r.exit_ts = (at + timedelta(minutes=5)).isoformat(timespec="minutes")
                r.session_over = reason != "LAST_BAR"
                r.note = search.note[:300]
                out.append(r)
                continue
        exit_bar = _exit_bar(intraday, when, cfg)
        if exit_bar is not None:
            at = exit_bar.ts.astimezone(tz)
            value = value_at(c, exit_bar.close, iv, at, cfg)
            r.exit_value = round(value, 4)
            r.pnl = round((value - c.mid) * (c.multiplier or lot), 2)
            r.exit_at = (at + timedelta(minutes=5)).strftime("%H:%M")
            r.exit_ts = (at + timedelta(minutes=5)).isoformat(timespec="minutes")
            hh, mm = (int(x) for x in str(cfg.get("session.force_exit_at", "15:45")).split(":"))
            r.session_over = (at + timedelta(minutes=5)).time() >= time(hh, mm)
        r.note = search.note[:300]
        out.append(r)
    return out


# Symbols the last scan_history call got no price history for.
missing: list[str] = []


async def scan_history(symbols: list[str], days: int, feed: Any, cfg: Any,
                       now: datetime, cooldown_minutes: int = 30,
                       only: set[str] | None = None) -> list[Blocked]:
    """Replay each of the last `days` sessions bar by bar through the desk's
    own strategies, and return every setup that fired — calls and puts.

    `only` limits it to some strategies (e.g. {"candlestick_at_level"}). A
    repeat of the same symbol, side and pattern inside `cooldown_minutes` is
    one setup, as it is on the desk.
    """
    from panaoptions.engine import levels as levels_mod
    from panaoptions.engine import strategies

    tz = ZoneInfo(cfg.timezone)
    wanted_days: list[date] = []
    day = now.astimezone(tz).date()
    while len(wanted_days) < days:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            wanted_days.append(day)
    found: list[Blocked] = []
    missing.clear()
    refused.clear()
    for symbol in symbols:
        try:
            bars = await feed.candles(symbol, str(cfg.get("technical.timeframe", "5m")))
        except Exception as exc:                     # noqa: BLE001
            log.warning("no history for %s: %s", symbol, exc)
            bars = []
        bars = sorted(bars or [], key=lambda b: b.ts)
        if not bars:
            missing.append(symbol)
            continue
        for d in sorted(wanted_days):
            idx = [i for i, b in enumerate(bars) if b.ts.astimezone(tz).date() == d]
            if not idx:
                continue
            session = levels_mod.compute(
                bars[:idx[-1] + 1], cfg.timezone, d,
                str(cfg.get("session.market_open", "09:30")),
                str(cfg.get("session.market_close", "16:00")))
            last_at: dict[tuple, datetime] = {}
            for i in idx:
                tape = bars[max(0, i - 299):i + 1]          # ~4 sessions of 5m bars
                winner, _ = strategies.evaluate_all(symbol, tape, session, cfg)
                if winner is None or (only and winner.strategy.name.lower() not in only
                                      and winner.strategy.value not in only):
                    continue
                at = bars[i].ts.astimezone(tz) + timedelta(minutes=5)   # the bar's close
                key = (symbol, winner.direction.value, winner.pattern or winner.strategy.value)
                if key in last_at and (at - last_at[key]).total_seconds() < cooldown_minutes * 60:
                    continue
                last_at[key] = at
                why = _gate(winner, session, tape, cfg)
                if why:
                    refused.append({"symbol": symbol, "ts": at.isoformat(timespec="minutes"),
                                    "side": option_side(winner.direction),
                                    "pattern": winner.pattern or winner.strategy.value,
                                    "why": why})
                    continue
                found.append(Blocked(
                    symbol=symbol, ts=at, direction=winner.direction.value,
                    reason=f"{option_side(winner.direction)} {winner.pattern or ''} "
                           f"{winner.key_level_source or ''}".strip(),
                    source="scan", pattern=winner.pattern,
                    strategy=winner.strategy.value,
                    stop=float(winner.underlying_support or 0.0),
                    target=float(winner.underlying_target or 0.0)))
    return sorted(found, key=lambda b: (b.ts, b.symbol))


# Setups the desk's gates refused during the last scan_history call.
refused: list[dict[str, Any]] = []


def _gate(setup: Any, session: Any, tape: list[Any], cfg: Any) -> str:
    """The desk's pre-contract gates on a historical setup: the two-candle
    PDH/PDL sweep (open interest is not in the history, so only the price
    half of the F&O confluence rule can be checked) and the 1:3 projection.
    Sets the setup's stop/target as the desk would. '' when it passes."""
    from panaoptions.engine import confluence, reward
    from panaoptions.engine import indicators as ta
    from panaoptions.engine.strategies import localise
    from panaoptions.models import SetupType
    is_sweep = setup.strategy is SetupType.PD_LIQUIDITY_SWEEP
    if confluence.applies(setup, cfg) and str(cfg.get("fno.confluence.mode", "sweep")) == "sweep":
        frame = localise(ta.to_frame(tape), cfg.timezone)
        why, found = confluence.sweep_check(setup, session, frame, cfg)
        if why:
            return f"F&O confluence: {why}"
        is_sweep = True
        tick = float(cfg.get("strategies.pd_liquidity_sweep.tick", 0.01))
        ticks = int(cfg.get("strategies.pd_liquidity_sweep.stop_ticks", 2))
        long = setup.direction is Direction.LONG
        beyond = found["wick"] - ticks * tick if long else found["wick"] + ticks * tick
        setup.underlying_support = (min(setup.underlying_support or beyond, beyond) if long
                                    else max(setup.underlying_support or beyond, beyond))
    target, _, why, _ = reward.project(setup, session, cfg, sweep=is_sweep)
    if why:
        return f"reward:risk: {why}"
    if target:
        setup.underlying_target = target
    return ""


def summarise(results: list[Result]) -> dict[str, Any]:
    by_tier: dict[str, int] = {}
    for r in results:
        by_tier[r.tier or "skipped"] = by_tier.get(r.tier or "skipped", 0) + 1
    priced = [r for r in results if r.spot]
    spreads = [r for r in results if r.tier == "debit_spread"]
    with_pnl = [r for r in results if r.pnl is not None]
    by_side: dict[str, dict[str, Any]] = {}
    for r in results:
        slot = by_side.setdefault(r.side or "?", {"setups": 0, "filled": 0, "outright": 0,
                                                  "debit_spread": 0, "skipped": 0,
                                                  "winners": 0, "pnl": 0.0})
        slot["setups"] += 1
        if r.filled:
            slot["filled"] += 1
            slot["debit_spread" if r.tier == "debit_spread" else "outright"] += 1
            slot["winners"] += 1 if (r.pnl or 0) > 0 else 0
            slot["pnl"] = round(slot["pnl"] + (r.pnl or 0.0), 2)
        else:
            slot["skipped"] += 1
    by_execution: dict[str, int] = {}
    for r in results:
        if r.spot:
            by_execution[r.execution or "SKIPPED_HARD_RISK"] = (
                by_execution.get(r.execution or "SKIPPED_HARD_RISK", 0) + 1)
    closed = [r for r in results if r.filled and r.pnl is not None]
    winners = [r for r in closed if (r.pnl or 0) > 0]
    by_exit: dict[str, int] = {}
    for r in closed:
        if r.exit_reason:
            by_exit[r.exit_reason] = by_exit.get(r.exit_reason, 0) + 1
    extra = {"closed": len(closed), "winners": len(winners),
             "win_rate": round(len(winners) / len(closed) * 100, 1) if closed else 0.0,
             "avg_win": round(sum(r.pnl for r in winners) / len(winners), 2) if winners else 0.0,
             "avg_loss": round(sum(r.pnl for r in closed if r.pnl <= 0)
                               / max(1, len(closed) - len(winners)), 2),
             "by_exit": by_exit, "refused_by_gates": len(refused)}
    return {**extra, "setups": len(results), "priced": len(priced),
            "filled": sum(1 for r in results if r.filled),
            "as_debit_spread": len(spreads), "by_tier": by_tier,
            "by_side": by_side, "by_execution": by_execution,
            "spread_pnl": round(sum(r.pnl or 0.0 for r in spreads), 2),
            "spread_winners": sum(1 for r in spreads if (r.pnl or 0) > 0),
            "total_pnl": round(sum(r.pnl or 0.0 for r in with_pnl), 2)}


def to_markdown(results: list[Result], summary: dict[str, Any], cfg: Any,
                title: str = "Backtest — blocked setups through the debit-spread ladder"
                ) -> str:
    cur = getattr(cfg, "currency", "$")
    sides = summary.get("by_side") or {}
    side_lines = [f"- **{k}**: {v['setups']} setups — {v['outright']} outright, "
                  f"{v['debit_spread']} debit spread, {v['skipped']} skipped; "
                  f"{v['winners']} of {v['filled']} green at the close, "
                  f"{cur}{v['pnl']:+,.2f}" for k, v in sorted(sides.items())]
    lines = [f"# {title}", "", *side_lines, "",
             f"**{summary['filled']} of {summary['setups']}** setups would have filled "
             f"({summary['as_debit_spread']} as a debit spread); by rung: "
             + ", ".join(f"{k} {v}" for k, v in sorted(summary["by_tier"].items())) + ".",
             f"Debit spreads at the square-off (or the last bar, mid-session): "
             f"{summary['spread_winners']} of "
             f"{summary['as_debit_spread']} green, {cur}{summary['spread_pnl']:+,.2f}.", "",
             "_Model prices (Black-Scholes on the historical underlying and its recorded "
             "or realised volatility), not historical option quotes. Liquidity is not "
             "tested; the spread rule is._", "",
             "| When | Symbol | Side | Pattern | Spot | Budget | Naked 0.40-0.50 | "
             "Executed as | Cost | Max profit | P&L (at) | Originally | Note |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        naked = f"{r.naked} ({cur}{r.naked_cost:,.0f})" if r.naked else "—"
        filled = (f"{r.execution}: {r.contract}" if r.tier else "SKIPPED")
        lines.append(
            f"| {r.ts} | {r.symbol} | {r.side or r.direction} | {r.pattern or r.strategy or '—'} | "
            f"{r.spot or '—'} | {cur}{r.budget:,.0f} | "
            f"{naked} | {filled} | {cur}{r.cost:,.0f} | "
            f"{(cur + format(r.max_profit, ',.0f')) if r.max_profit else '—'} | "
            f"{'—' if r.pnl is None else cur + format(r.pnl, '+,.2f') + ' (' + r.exit_at + (')' if r.session_over else ', so far)')} | "
            f"{r.was_blocked.replace('|', '/')[:160] or '—'} | "
            f"{r.note.replace('|', '/')[:160]} |")
    return "\n".join(lines) + "\n"


def save(results: list[Result], summary: dict[str, Any], cfg: Any,
         when: datetime, kind: str = "spreads", title: str | None = None
         ) -> dict[str, str]:
    from panaoptions.journal import store as journal_store
    folder = journal_store.JOURNAL_DIR / "backtest"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{kind}-{when:%Y-%m-%d-%H%M}"
    md, js = folder / f"{stem}.md", folder / f"{stem}.json"
    md.write_text(to_markdown(results, summary, cfg, **({"title": title} if title else {})),
                  encoding="utf-8")
    js.write_text(json.dumps({"summary": summary,
                              "results": [asdict(r) for r in results]},
                             indent=2, default=str), encoding="utf-8")
    return {"markdown": str(md), "json": str(js)}


# --------------------------------------------------------------------------- #
# Old rules against new: the same history, two rule books
# --------------------------------------------------------------------------- #
# What each rule book changes, as dotted config overrides on top of the desk's
# config. "new" is the desk as configured now.
RULE_BOOKS: dict[str, dict[str, Any]] = {
    "before": {
        "strategies.pd_liquidity_sweep.enabled": False,
        "contracts.opening_spread.enabled": False,
        "contracts.budget_fallback_min_delta": 0.30,
        "contracts.single_leg_grace.enabled": False,
        "fno.confluence.mode": "touch",
    },
    "after": {},
}


def with_overrides(cfg: Any, overrides: dict[str, Any]) -> Any:
    """A copy of `cfg` with dotted keys set."""
    import copy
    out = copy.deepcopy(cfg)
    for dotted, value in overrides.items():
        node = out.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


async def compare(symbols: list[str], days: int, feed: Any, cfg: Any, now: datetime,
                  books: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run the scanner and the contract ladder over the same sessions under
    each rule book. Returns {book: {"summary", "results", "refused"}}."""
    out: dict[str, Any] = {}
    for name, overrides in (books or RULE_BOOKS).items():
        book_cfg = with_overrides(cfg, overrides)
        items = await scan_history(symbols, days, feed, book_cfg, now)
        gated = list(refused)
        results = await replay(items, feed, book_cfg)
        out[name] = {"summary": summarise(results), "results": results, "refused": gated}
    return out


def compare_markdown(runs: dict[str, Any], cfg: Any, title: str) -> str:
    cur = getattr(cfg, "currency", "$")
    rows = ["# " + title, "",
            "| Rule book | Setups | Filled | Outright | Debit spread | Grace | Skipped | "
            "Refused by gates | Win rate | Avg win | Avg loss | Total P&L |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, run in runs.items():
        sm = run["summary"]
        tiers = sm.get("by_tier") or {}
        outright = sum(v for k, v in tiers.items()
                       if k in {"primary", "shorter_expiry", "secondary_delta"})
        rows.append(
            f"| {name} | {sm['setups']} | {sm['filled']} | {outright} | "
            f"{tiers.get('debit_spread', 0)} | {tiers.get('single_leg_grace', 0)} | "
            f"{tiers.get('skipped', 0)} | {sm.get('refused_by_gates', 0)} | "
            f"{sm['win_rate']}% | {cur}{sm['avg_win']:,.2f} | {cur}{sm['avg_loss']:,.2f} | "
            f"{cur}{sm['total_pnl']:+,.2f} |")
    rows += ["", "_Same sessions, same model chain, same exits (stop, target or square-off). "
                 "Open interest is not in the history, so the confluence rule is tested on "
                 "price only. Model prices, not historical quotes._", ""]
    for name, run in runs.items():
        rows += [f"## {name}", ""]
        rows += [f"- refused: {g['ts']} {g['symbol']} {g['side']} {g['pattern']} — {g['why'][:160]}"
                 for g in run["refused"][:30]]
        rows.append("")
        rows.append(to_markdown(run["results"], run["summary"], cfg,
                                title=f"{name}: every setup").split("\n", 2)[2])
    return "\n".join(rows)
