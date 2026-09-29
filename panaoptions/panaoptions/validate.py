"""Backtest validation: does the rule book earn its keep before it trades?

    python run.py --backtest [--market US|IN] [--symbols SPY,QQQ] [--days 10]

Replays the last `days` sessions through the desk's own strategies, gates and
contract picker (backtest_spreads.scan_history + replay), then puts every
fill through the account rules the desk trades under:

  sizing       max_capital_deployed_pct caps the premium; max_risk_per_trade_pct
               caps the loss at the first stop (guardrails.planned_loss)
  throttles    max_open_trades at once, max_daily_trades a day, one position
               per symbol
  the breaker  once a day's realised loss reaches daily_loss_limit_pct, nothing
               else is taken that day

and judges the result:

  expectancy   the mean R per trade, where R = the trade's P&L / the loss
               planned at its stop (what the sizing risked)
  drawdown     the worst peak-to-trough fall of the account, % of the peak

PASS needs expectancy >= backtest.validation.min_expectancy_r (0.5R) AND max
drawdown <= backtest.validation.max_drawdown_pct (5%) over at least
min_trades trades. Fewer trades is INCONCLUSIVE, never a pass.

Two limits, stated on every report: the option prices are Black-Scholes on the
historical underlying (no free source keeps option quotes), and historical
open interest is not available, so the OI half of the F&O confluence rule is
not tested.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from panaoptions.logging import get_logger

log = get_logger("validate")


@dataclass
class Taken:
    symbol: str
    entry: str
    exit: str
    side: str
    strategy: str
    pattern: str
    contract: str
    quantity: int
    risk: float            # the planned loss at the stop, whole position
    pnl: float
    r: float
    exit_reason: str


@dataclass
class Verdict:
    verdict: str                      # PASS / FAIL / INCONCLUSIVE
    reasons: list[str]
    market: str
    symbols: list[str]
    days: int
    capital: float
    trades: int
    wins: int
    win_rate: float
    expectancy_r: float
    avg_win_r: float
    avg_loss_r: float
    profit_factor: float
    total_pnl: float
    return_pct: float
    max_drawdown_pct: float
    thresholds: dict[str, float]
    rules: dict[str, Any]
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_day: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    locked_days: list[str] = field(default_factory=list)
    trades_list: list[dict[str, Any]] = field(default_factory=list)
    # Each strategy's expectancy over EVERY fill of the whole period (before
    # the throttles): what the live desk ranks its daily slots by.
    edge: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The sessions the verdict is judged on (walk-forward: the second half).
    period: str = ""
    at: str = ""


def _when(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def r_plan(cfg: Any) -> bool:
    """True when the desk exits on the R-multiple plan (ledger._manage_r)."""
    return str(cfg.get("risk.exit_style", "auto")).lower() == "r_multiple"


def outcome(r: Any, cfg: Any, qty: int = 2) -> tuple[float | None, str, str]:
    """(P&L per contract, exit time, exit reason) of a fill under the
    configured exit plan. One contract cannot be halved: under the R plan it
    only moves to breakeven and trails (pnl_r1)."""
    if r_plan(cfg):
        if qty >= 2:
            return r.pnl_r, r.exit_ts_r, r.exit_reason_r or "SQUARE_OFF"
        return r.pnl_r1, r.exit_ts_r1 or r.exit_ts_r, "TRAIL/BREAKEVEN"
    return r.pnl, r.exit_ts, r.exit_reason or "SQUARE_OFF"


def edge_of(results: list[Any], cfg: Any) -> dict[str, dict[str, Any]]:
    """Each strategy's expectancy over every fill, one contract each: R = the
    fill's P&L / the loss planned at its stop. Before any throttle, so it
    measures the strategy rather than which setups happened to fire first."""
    from panaoptions import ranking
    from panaoptions.risk.guardrails import planned_loss
    rows: dict[str, list[float]] = defaultdict(list)
    for r in results:
        pnl = outcome(r, cfg)[0] if r.filled else None
        if pnl is None or not r.mid > 0:
            continue
        per = planned_loss(cfg, r.mid, r.delta, int(r.lot or cfg.multiplier), r.spot,
                           r.stop, r.tier == "debit_spread")
        if per > 0:
            rows[ranking.key(r.strategy)].append(pnl / per)
    return {k: {"fills": len(v), "expectancy_r": round(sum(v) / len(v), 2),
                "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100, 1)}
            for k, v in sorted(rows.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))}


def simulate(results: list[Any], cfg: Any, edge: dict[str, float] | None = None
             ) -> tuple[list[Taken], Counter, set[str], float]:
    """The fills, in time order, through sizing, throttles and the breaker.
    `edge` ranks the strategies for the reserved slots and for fills at the
    same moment (ranking.py). Returns (trades taken, skips by reason,
    locked-out days, max drawdown %)."""
    from panaoptions import ranking
    from panaoptions.risk.gatekeeper import cap_pct
    from panaoptions.risk.guardrails import planned_loss

    capital = float(cfg.capital)
    g = cfg.get
    max_open = int(g("risk.max_open_trades", 1) or 1)
    max_daily = int(g("risk.max_daily_trades", 0) or 0)
    one_per_symbol = bool(g("risk.one_position_per_symbol", True))
    total_pct = float(g("risk.max_total_deployed_pct", g("risk.max_capital_deployed_pct", 20.0)))
    risk_pct = float(g("risk.max_risk_per_trade_pct", 0) or 0)
    risk_cap = capital * risk_pct / 100.0 if risk_pct > 0 else 0.0
    absolute = g("risk.daily_loss_limit")
    limit = (abs(float(absolute)) if absolute not in (None, "", 0, 0.0)
             else capital * float(g("risk.daily_loss_limit_pct", 10.0)) / 100.0)
    slip = float(g("risk.slippage_per_contract", 0.02) or 0.0)

    fills = [r for r in results if r.filled and r.mid > 0 and _when(r.ts)
             and outcome(r, cfg)[0] is not None and _when(outcome(r, cfg)[1])]
    fills.sort(key=lambda r: (_when(r.ts), -ranking.score(cfg, r.strategy, edge), r.symbol))

    taken: list[Taken] = []
    skipped: Counter = Counter()
    locked: set[str] = set()
    open_pos: list[tuple[datetime, Taken, float]] = []     # (exit, trade, deployed)
    realised: dict[str, float] = defaultdict(float)
    count: Counter = Counter()
    equity = peak = capital
    max_dd = 0.0

    def book_until(t: datetime | None) -> None:
        nonlocal equity, peak, max_dd
        open_pos.sort(key=lambda p: p[0])
        while open_pos and (t is None or open_pos[0][0] <= t):
            _, trade, _ = open_pos.pop(0)
            day = trade.entry[:10]
            equity += trade.pnl
            realised[day] += trade.pnl
            if realised[day] <= -limit:
                locked.add(day)
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak * 100 if peak else 0.0)

    for r in fills:
        start = _when(r.ts)
        book_until(start)
        day = r.ts[:10]
        if day in locked:
            skipped["daily circuit breaker (locked out)"] += 1
            continue
        if max_daily and count[day] >= max_daily:
            skipped[f"daily trade limit ({max_daily})"] += 1
            continue
        if ranking.slot_refusal(cfg, r.strategy, count[day], edge):
            skipped["reserved slots (kept for the better strategies)"] += 1
            continue
        if len(open_pos) >= max_open:
            skipped[f"max open trades ({max_open})"] += 1
            continue
        if one_per_symbol and any(p[1].symbol == r.symbol for p in open_pos):
            skipped["already holding the symbol"] += 1
            continue
        lot = int(r.lot or cfg.multiplier)
        cost = r.mid * lot
        deployed = sum(p[2] for p in open_pos)
        budget = min(capital * cap_pct(cfg, r.symbol) / 100.0,
                     capital * total_pct / 100.0 - deployed)
        qty = int(budget // cost) if cost > 0 else 0
        if qty < 1:
            skipped["premium over the deployment budget"] += 1
            continue
        per = planned_loss(cfg, r.mid, r.delta, lot, r.spot, r.stop,
                           r.tier == "debit_spread")
        if risk_cap:
            if per > risk_cap:
                skipped[f"loss at the stop over {risk_pct:g}% a trade"] += 1
                continue
            if per > 0:
                qty = min(qty, int(risk_cap // per))
        legs = 2 if r.tier == "debit_spread" else 1
        each, out_at, why = outcome(r, cfg, qty)
        if each is None:
            skipped["no exit under the exit plan"] += 1
            continue
        end = _when(out_at)
        pnl = round(each * qty - slip * legs * 2 * lot * qty, 2)
        risk = round(per * qty, 2) or round(cost * qty, 2)
        trade = Taken(symbol=r.symbol, entry=r.ts, exit=out_at, side=r.side,
                      strategy=r.strategy, pattern=r.pattern, contract=r.contract,
                      quantity=qty, risk=risk, pnl=pnl,
                      r=round(pnl / risk, 2) if risk else 0.0, exit_reason=why)
        taken.append(trade)
        count[day] += 1
        open_pos.append((end, trade, cost * qty))
    book_until(None)
    return taken, skipped, locked, round(max_dd, 2)


def judge(taken: list[Taken], skipped: Counter, locked: set[str], max_dd: float,
          cfg: Any, symbols: list[str], days: int) -> Verdict:
    g = cfg.get
    min_exp = float(g("backtest.validation.min_expectancy_r", 0.5))
    max_dd_allowed = float(g("backtest.validation.max_drawdown_pct", 5.0))
    min_trades = int(g("backtest.validation.min_trades", 20))
    capital = float(cfg.capital)

    rs = [t.r for t in taken]
    wins = [t for t in taken if t.pnl > 0]
    losses = [t for t in taken if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    expectancy = round(sum(rs) / len(rs), 2) if rs else 0.0
    total = round(sum(t.pnl for t in taken), 2)

    by_strategy: dict[str, dict[str, Any]] = {}
    for t in taken:
        s = by_strategy.setdefault(t.strategy or "?", {"trades": 0, "wins": 0, "r": 0.0,
                                                       "pnl": 0.0})
        s["trades"] += 1
        s["wins"] += 1 if t.pnl > 0 else 0
        s["r"] += t.r
        s["pnl"] = round(s["pnl"] + t.pnl, 2)
    for s in by_strategy.values():
        s["expectancy_r"] = round(s.pop("r") / s["trades"], 2)
        s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1)

    by_day: dict[str, dict[str, Any]] = {}
    for t in taken:
        d = by_day.setdefault(t.entry[:10], {"trades": 0, "pnl": 0.0})
        d["trades"] += 1
        d["pnl"] = round(d["pnl"] + t.pnl, 2)
    for d in sorted(locked):
        by_day.setdefault(d, {"trades": 0, "pnl": 0.0})["locked_out"] = True

    reasons = []
    if len(taken) < min_trades:
        verdict = "INCONCLUSIVE"
        reasons.append(f"only {len(taken)} trade(s) — at least {min_trades} are needed "
                       f"to judge; run more days or symbols")
    else:
        verdict = "PASS"
        if expectancy < min_exp:
            verdict = "FAIL"
            reasons.append(f"expectancy {expectancy:+.2f}R is below {min_exp:g}R")
        if max_dd > max_dd_allowed:
            verdict = "FAIL"
            reasons.append(f"max drawdown {max_dd:.2f}% is over {max_dd_allowed:g}%")
        if verdict == "PASS":
            reasons.append(f"expectancy {expectancy:+.2f}R ≥ {min_exp:g}R and max "
                           f"drawdown {max_dd:.2f}% ≤ {max_dd_allowed:g}%")

    return Verdict(
        verdict=verdict, reasons=reasons, market=str(getattr(cfg, "market", "US")),
        symbols=list(symbols), days=days, capital=capital, trades=len(taken),
        wins=len(wins), win_rate=round(len(wins) / len(taken) * 100, 1) if taken else 0.0,
        expectancy_r=expectancy,
        avg_win_r=round(sum(t.r for t in wins) / len(wins), 2) if wins else 0.0,
        avg_loss_r=round(sum(t.r for t in losses) / len(losses), 2) if losses else 0.0,
        profit_factor=round(gross_win / gross_loss, 2) if gross_loss else 0.0,
        total_pnl=total, return_pct=round(total / capital * 100, 2) if capital else 0.0,
        max_drawdown_pct=max_dd,
        thresholds={"min_expectancy_r": min_exp, "max_drawdown_pct": max_dd_allowed,
                    "min_trades": min_trades},
        rules={"exit_style": g("risk.exit_style"),
               "max_risk_per_trade_pct": g("risk.max_risk_per_trade_pct"),
               "max_capital_deployed_pct": g("risk.max_capital_deployed_pct"),
               "max_open_trades": g("risk.max_open_trades"),
               "max_daily_trades": g("risk.max_daily_trades"),
               "daily_loss_limit_pct": g("risk.daily_loss_limit_pct"),
               "min_reward_risk": g("risk.min_reward_risk")},
        by_strategy=dict(sorted(by_strategy.items(), key=lambda kv: -kv[1]["pnl"])),
        by_day=dict(sorted(by_day.items())), skipped=dict(skipped.most_common()),
        locked_days=sorted(locked), trades_list=[asdict(t) for t in taken],
        at=datetime.now().isoformat(timespec="seconds"))


async def run(symbols: list[str], days: int, feed: Any, cfg: Any,
              now: datetime, walk_forward: bool | None = None) -> Verdict:
    """Scan, replay and judge. Walk-forward (the default): the strategy
    ranking is learned on the first half of the sessions and the verdict is
    judged on the second half only — never on the days it was tuned on."""
    from panaoptions import backtest_spreads as bt
    items = await bt.scan_history(symbols, days, feed, cfg, now)
    results = await bt.replay(items, feed, cfg)
    return judge_results(results, cfg, symbols, days, walk_forward)


def judge_results(results: list[Any], cfg: Any, symbols: list[str], days: int,
                  walk_forward: bool | None = None) -> Verdict:
    from panaoptions import ranking
    if walk_forward is None:
        walk_forward = bool(cfg.get("backtest.validation.walk_forward", True))
    dates = sorted({r.ts[:10] for r in results if r.ts})
    period = f"{dates[0]} → {dates[-1]}" if dates else ""
    tested = results
    edge = ranking.edge_from(edge_of(results, cfg), cfg)
    if walk_forward and len(dates) >= 4:
        cut = dates[len(dates) // 2]
        learn = [r for r in results if r.ts[:10] < cut]
        tested = [r for r in results if r.ts[:10] >= cut]
        edge = ranking.edge_from(edge_of(learn, cfg), cfg)
        period = (f"judged on {cut} → {dates[-1]} ({len(dates) - len(dates) // 2} sessions), "
                  f"ranked on {dates[0]} → {dates[len(dates) // 2 - 1]} (walk-forward)")
    taken, skipped, locked, max_dd = simulate(tested, cfg, edge)
    verdict = judge(taken, skipped, locked, max_dd, cfg, symbols, days)
    verdict.edge = edge_of(results, cfg)
    verdict.period = period
    return verdict


def markdown(v: Verdict, currency: str = "$") -> str:
    th = v.thresholds
    lines = [
        f"# Backtest validation — {v.verdict}",
        "",
        f"{', '.join(v.symbols)} · last {v.days} session(s) · {v.market} · capital "
        f"{currency}{v.capital:,.0f}",
        "",
        *([f"_{v.period}_", ""] if v.period else []),
        *[f"- {r}" for r in v.reasons],
        "",
        "| Trades | Win rate | Expectancy | Avg win | Avg loss | Profit factor | P&L | "
        "Return | Max drawdown |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| {v.trades} | {v.win_rate:.1f}% | {v.expectancy_r:+.2f}R "
        f"(need ≥ {th['min_expectancy_r']:g}R) | {v.avg_win_r:+.2f}R | "
        f"{v.avg_loss_r:+.2f}R | {v.profit_factor:.2f} | {currency}{v.total_pnl:+,.2f} | "
        f"{v.return_pct:+.2f}% | {v.max_drawdown_pct:.2f}% (max {th['max_drawdown_pct']:g}%) |",
        "",
        "Rules applied: " + ", ".join(f"{k} {val}" for k, val in v.rules.items()),
        "",
        "## By strategy",
        "",
        "| Strategy | Trades | Win rate | Expectancy | P&L |",
        "|---|---|---|---|---|",
    ]
    for name, s in v.by_strategy.items():
        mark = " ✓" if s["expectancy_r"] >= th["min_expectancy_r"] else ""
        lines.append(f"| {name} | {s['trades']} | {s['win_rate']:.1f}% | "
                     f"{s['expectancy_r']:+.2f}R{mark} | {currency}{s['pnl']:+,.2f} |")
    if v.edge:
        lines += ["", "## Strategy edge — every fill, before the throttles (the live ranking)",
                  "", "| Strategy | Fills | Win rate | Expectancy |", "|---|---|---|---|"]
        lines += [f"| {k} | {e['fills']} | {e['win_rate']:.1f}% | {e['expectancy_r']:+.2f}R |"
                  for k, e in v.edge.items()]
    lines += ["", "## By day", "", "| Day | Trades | P&L | |", "|---|---|---|---|"]
    for day, d in v.by_day.items():
        lines.append(f"| {day} | {d['trades']} | {currency}{d['pnl']:+,.2f} | "
                     f"{'LOCKED OUT (3% breaker)' if d.get('locked_out') else ''} |")
    if v.skipped:
        lines += ["", "## Setups the account rules did not take", ""]
        lines += [f"- {n}× {why}" for why, n in v.skipped.items()]
    lines += ["", "_Model option prices (Black-Scholes on the historical underlying), "
                  "not historical quotes; historical open interest is not available, so "
                  "the OI half of the F&O confluence rule is not tested._"]
    return "\n".join(lines) + "\n"


def save(v: Verdict, currency: str = "$") -> str:
    from panaoptions.journal import store as journal_store
    folder = journal_store.JOURNAL_DIR / "backtest"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    md = folder / f"validation-{stamp}.md"
    md.write_text(markdown(v, currency), encoding="utf-8")
    (folder / "validation-latest.json").write_text(
        json.dumps(asdict(v), indent=2, default=str), encoding="utf-8")
    return str(md)


def latest() -> dict[str, Any] | None:
    """The last validation's verdict for the active market, if any."""
    from panaoptions.journal import store as journal_store
    path = journal_store.JOURNAL_DIR / "backtest" / "validation-latest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
