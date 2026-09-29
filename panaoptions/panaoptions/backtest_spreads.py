"""Backtest: would the blocked setups have filled as debit spreads?

    python run.py --backtest-spreads                 last 5 blocked setups
    python run.py --backtest-spreads --limit 20      more of them
    python run.py --backtest-spreads --symbols SPY,QQQ,NVDA --days 5
                                                     any symbols, recent sessions

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
from panaoptions.models import Direction, OptionContract, OptionRight

log = get_logger("backtest")

BLOCKED = re.compile(r"over the \$|dearer than|Nothing fits|SKIPPED|budget|cannot both hold",
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
    note: str = ""
    was_blocked: str = ""
    legs: list[str] = field(default_factory=list)

    @property
    def filled(self) -> bool:
        return bool(self.tier)


# --------------------------------------------------------------------------- #
# Reading the log
# --------------------------------------------------------------------------- #
def blocked_from_log(limit: int = 5, since: str | None = None) -> list[Blocked]:
    """The most recent setups the desk refused for want of a contract."""
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
    for ts, symbol, direction, reason, payload in rows:
        if not BLOCKED.search(str(reason or "")) or direction not in {"LONG", "SHORT"}:
            continue
        try:
            extra = json.loads(payload or "{}")
        except ValueError:
            extra = {}
        budget = float(extra.get("budget") or 0.0)
        if not budget:
            m = re.search(r"\$([\d,]+(?:\.\d+)?) budget", str(reason))
            budget = float(m.group(1).replace(",", "")) if m else 0.0
        out.append(Blocked(symbol=str(symbol), ts=datetime.fromisoformat(str(ts)),
                           direction=str(direction), reason=str(reason), budget=budget))
        if len(out) >= limit:
            break
    return list(reversed(out))


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


def model_chain(symbol: str, spot: float, iv: float, when: datetime, cfg: Any,
                lot: int, strikes_each_side: int = 30) -> list[OptionContract]:
    """Every strike and expiry in the desk's window, priced by Black-Scholes."""
    min_dte = int(cfg.get("contracts.min_dte", 0))
    max_dte = int(cfg.get("contracts.max_dte", 14))
    index = symbol.upper() in DAILY_EXPIRY
    spread_pct = float(cfg.get("backtest.index_spread_pct" if index
                               else "backtest.stock_spread_pct", 3.0 if index else 5.0))
    step = strike_step(symbol, spot)
    atm = round(spot / step) * step
    close = cfg.get("session.market_close", "16:00")
    ch, cm = (int(x) for x in str(close).split(":"))
    out = []
    for exp in expiries(symbol, when.date(), max_dte):
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
                   direction=b.direction, was_blocked=b.reason[:200])
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
        search = contracts.choose(b.symbol, chain, direction, cfg, budget=r.budget)
        if search.chosen is None:
            r.note = search.note[:300]
            out.append(r)
            continue
        c = search.chosen
        r.tier, r.contract, r.cost = search.tier, c.label, c.cost(lot)
        if c.is_spread:
            r.legs = [f"BUY {c.long_leg.label} ({abs(c.long_leg.delta):.2f}Δ)",
                      f"SELL {c.short_leg.label} ({abs(c.short_leg.delta):.2f}Δ)"]
            r.max_profit = round((c.width - c.mid) * (c.multiplier or lot), 2)
        exit_bar = _exit_bar(intraday, when, cfg)
        if exit_bar is not None:
            value = value_at(c, exit_bar.close, iv, exit_bar.ts.astimezone(tz), cfg)
            r.exit_value = round(value, 4)
            r.pnl = round((value - c.mid) * (c.multiplier or lot), 2)
        r.note = search.note[:300]
        out.append(r)
    return out


def summarise(results: list[Result]) -> dict[str, Any]:
    by_tier: dict[str, int] = {}
    for r in results:
        by_tier[r.tier or "skipped"] = by_tier.get(r.tier or "skipped", 0) + 1
    priced = [r for r in results if r.spot]
    spreads = [r for r in results if r.tier == "debit_spread"]
    with_pnl = [r for r in results if r.pnl is not None]
    return {"setups": len(results), "priced": len(priced),
            "filled": sum(1 for r in results if r.filled),
            "as_debit_spread": len(spreads), "by_tier": by_tier,
            "spread_pnl": round(sum(r.pnl or 0.0 for r in spreads), 2),
            "spread_winners": sum(1 for r in spreads if (r.pnl or 0) > 0),
            "total_pnl": round(sum(r.pnl or 0.0 for r in with_pnl), 2)}


def to_markdown(results: list[Result], summary: dict[str, Any], cfg: Any) -> str:
    cur = getattr(cfg, "currency", "$")
    lines = ["# Backtest — blocked setups through the debit-spread ladder", "",
             f"**{summary['filled']} of {summary['setups']}** setups would have filled "
             f"({summary['as_debit_spread']} as a debit spread); by rung: "
             + ", ".join(f"{k} {v}" for k, v in sorted(summary["by_tier"].items())) + ".",
             f"Debit spreads at the close: {summary['spread_winners']} of "
             f"{summary['as_debit_spread']} green, {cur}{summary['spread_pnl']:+,.2f}.", "",
             "_Model prices (Black-Scholes on the historical underlying and its recorded "
             "or realised volatility), not historical option quotes. Liquidity is not "
             "tested; the spread rule is._", "",
             "| When | Symbol | Side | Spot | Budget | Naked 0.40-0.50 | Filled as | Cost | "
             "Max profit | P&L at close | Note |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        naked = f"{r.naked} ({cur}{r.naked_cost:,.0f})" if r.naked else "—"
        filled = (f"{r.tier}: {r.contract}" if r.tier else "SKIPPED")
        lines.append(
            f"| {r.ts} | {r.symbol} | {r.direction} | {r.spot or '—'} | {cur}{r.budget:,.0f} | "
            f"{naked} | {filled} | {cur}{r.cost:,.0f} | "
            f"{(cur + format(r.max_profit, ',.0f')) if r.max_profit else '—'} | "
            f"{'—' if r.pnl is None else cur + format(r.pnl, '+,.2f')} | "
            f"{r.note.replace('|', '/')[:160]} |")
    return "\n".join(lines) + "\n"


def save(results: list[Result], summary: dict[str, Any], cfg: Any,
         when: datetime) -> dict[str, str]:
    from panaoptions.journal import store as journal_store
    folder = journal_store.JOURNAL_DIR / "backtest"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"spreads-{when:%Y-%m-%d-%H%M}"
    md, js = folder / f"{stem}.md", folder / f"{stem}.json"
    md.write_text(to_markdown(results, summary, cfg), encoding="utf-8")
    js.write_text(json.dumps({"summary": summary,
                              "results": [asdict(r) for r in results]},
                             indent=2, default=str), encoding="utf-8")
    return {"markdown": str(md), "json": str(js)}
