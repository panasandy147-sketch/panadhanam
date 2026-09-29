"""The audit log: every paper BUY and SELL, with everything behind it.

One append-only JSON-lines file per session, journal/audit/YYYY-MM-DD.jsonl,
and a readable journal/audit/YYYY-MM-DD.md rewritten after each event.
Written at the fill and at the exit, so it holds what the desk knew THEN.

BUY: the contract, size, premium, disaster stop and targets, the strategy
and pattern, the underlying invalidation and target, the alpha signal's
five numbers, the committee's votes and reasons, the Risk Gatekeeper's
checks, and the options-flow and volume-profile reads.
SELL: each fill of the exit (scale-outs included), the realised P&L, how it
ended, and how long it was held.

Read by the weekly review, the Friday reflection and by you on the weekend.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from panaoptions import clock
from panaoptions.logging import get_logger

log = get_logger("audit")


def audit_dir() -> Path:
    from panaoptions.journal import store
    return store.JOURNAL_DIR / "audit"


def _write(cfg: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Append one event. Never raises: an audit failure must not stop trading."""
    now = clock.now(cfg.timezone)
    record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"),
              "market_time": now.strftime("%Y-%m-%d %H:%M:%S %Z"), **record}
    try:
        folder = audit_dir()
        folder.mkdir(parents=True, exist_ok=True)
        day = now.date().isoformat()
        with (folder / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        (folder / f"{day}.md").write_text(day_markdown(now.date()), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write the audit log: %s", exc)
    return record


def record_buy(cfg: Any, trade: Any, signal: Any, setup: Any,
               candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    """A filled entry and the whole case for it."""
    from panaoptions import alpha

    alpha_signal = alpha.from_setup(setup)
    verdict = (candidate or {}).get("verdict") or {}
    c = signal.contract
    return _write(cfg, {
        "event": "BUY", "trade_id": trade.id, "signal_id": signal.id,
        "symbol": signal.symbol, "contract": c.label,
        "right": c.right.value, "strike": c.strike, "expiry": c.expiry, "dte": c.dte,
        "delta": c.delta, "iv": c.implied_volatility, "bid": c.bid, "ask": c.ask,
        "spread_pct": c.spread_pct_of_mid, "quantity": trade.quantity,
        "estimated_prices": bool(getattr(c, "estimated", False)),
        "structure": c.structure,
        "legs": ([{"side": "BUY", "contract": c.long_leg.label,
                   "delta": c.long_leg.delta, "mid": c.long_leg.mid},
                  {"side": "SELL", "contract": c.short_leg.label,
                   "delta": c.short_leg.delta, "mid": c.short_leg.mid}]
                 if c.is_spread else None),
        "max_value": c.width or None,
        "tier": getattr(trade, "tier", "") or None,
        "entry": trade.entry_price, "cost": signal.cost(cfg.multiplier),
        "market": getattr(cfg, "market", "US"),
        "currency": getattr(cfg, "currency", "$"),
        "disaster_stop": trade.stop_price, "target_1": trade.target_1,
        "target_2": trade.target_2, "strategy": setup.strategy.value,
        "pattern": setup.pattern, "underlying": setup.indicators.close,
        "underlying_stop": setup.underlying_support,
        "underlying_target": setup.underlying_target or None,
        "invalidation_note": setup.invalidation_note,
        "key_level": setup.key_level or None, "key_level_source": setup.key_level_source,
        "confirmations": list(setup.confirmations),
        "reasoning": list(setup.reasoning),
        "signal": alpha_signal.to_dict() if alpha_signal else None,
        "committee": {k: verdict.get(k) for k in ("score", "committee", "weight",
                                                  "threshold", "votes", "gate")}
        if verdict else None,
    })


def record_sell(cfg: Any, trade: Any) -> dict[str, Any]:
    """A closed trade: every exit fill, the P&L and how it ended."""
    held = None
    if trade.closed_at and trade.opened_at:
        try:
            held = round((trade.closed_at - trade.opened_at).total_seconds() / 60.0, 1)
        except TypeError:
            held = None
    exits = [f for f in trade.fills if f.reason != "ENTRY"]
    return _write(cfg, {
        "event": "SELL", "trade_id": trade.id, "signal_id": trade.signal_id,
        "symbol": trade.symbol, "contract": trade.contract_label,
        "strategy": trade.strategy.value, "pattern": trade.pattern,
        "market": getattr(cfg, "market", "US"),
        "currency": getattr(cfg, "currency", "$"),
        "entry": trade.entry_price,
        "exits": [{"ts": f.ts, "quantity": f.quantity, "price": f.price,
                   "reason": f.reason} for f in exits],
        "exit_reason": trade.exit_reason.value if trade.exit_reason else "",
        "estimated_prices": bool(getattr(trade, "estimated", False)),
        "pnl": round(trade.realised_pnl, 2), "held_minutes": held,
        "invalidation_note": trade.invalidation_note,
    })


def entries(day: date | None = None, since: date | None = None,
            until: date | None = None) -> list[dict[str, Any]]:
    folder = audit_dir()
    if not folder.is_dir():
        return []
    days = [day] if day else sorted(
        date.fromisoformat(p.stem) for p in folder.glob("*.jsonl")
        if (since is None or p.stem >= since.isoformat())
        and (until is None or p.stem <= until.isoformat()))
    out = []
    for d in days:
        path = folder / f"{d.isoformat()}.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def by_day(since: date, until: date) -> list[dict[str, Any]]:
    """Every buy and sell from `since` to `until`, grouped by session date, in
    order — what the weekly review shows date by date, mid-week included.
    (Each market keeps its own folder, so this is one market's.)"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in entries(since=since, until=until):
        grouped.setdefault(str(e.get("market_time") or e.get("ts") or "")[:10],
                           []).append(e)
    out = []
    for day in sorted(grouped):
        rows = []
        for e in sorted(grouped[day], key=lambda x: str(x.get("ts") or "")):
            buy = e.get("event") == "BUY"
            exits = e.get("exits") or []
            qty = sum(int(x.get("quantity") or 0) for x in exits) if not buy else e.get("quantity")
            price = e.get("entry") if buy else (
                round(sum(float(x.get("price") or 0) * int(x.get("quantity") or 0)
                          for x in exits) / qty, 4) if qty else None)
            rows.append({
                "time": str(e.get("market_time") or "")[11:16],
                "event": e.get("event"), "trade_id": e.get("trade_id"),
                "symbol": e.get("symbol"), "contract": e.get("contract"),
                "strategy": e.get("strategy"), "pattern": e.get("pattern"),
                "quantity": qty, "price": price,
                "cost": e.get("cost") if buy else None,
                "stop": e.get("underlying_stop") if buy else None,
                "option_stop": e.get("disaster_stop") if buy else None,
                "target": e.get("target_1") if buy else None,
                "pnl": None if buy else e.get("pnl"),
                "held_minutes": None if buy else e.get("held_minutes"),
                "reason": ("; ".join(e.get("confirmations") or [])
                           or e.get("invalidation_note") or "") if buy
                          else str(e.get("exit_reason") or ""),
                "estimated": bool(e.get("estimated_prices")),
            })
        sells = [r for r in rows if r["pnl"] is not None]
        wd = date.fromisoformat(day).strftime("%a") if len(day) == 10 else ""
        out.append({"date": day, "weekday": wd, "events": rows,
                    "buys": len(rows) - len(sells), "sells": len(sells),
                    "wins": sum(1 for r in sells if (r["pnl"] or 0) > 0),
                    "losses": sum(1 for r in sells if (r["pnl"] or 0) < 0),
                    "pnl": round(sum(r["pnl"] or 0.0 for r in sells), 2)})
    return out


def by_trade(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        out.setdefault(str(e.get("trade_id")), {})[
            "sell" if e.get("event") == "SELL" else "buy"] = e
    return out


def day_markdown(day: date) -> str:
    trades = by_trade(entries(day))
    lines = [f"# Audit — {day.isoformat()}", "",
             f"{len(trades)} trade(s), written at the fill and at the exit.", ""]
    for tid, t in trades.items():
        b, s = t.get("buy") or {}, t.get("sell") or {}
        head = b or s
        lines.append(f"## {head.get('contract')} — {head.get('strategy')} "
                     f"({head.get('pattern')}) · {tid}")
        if b:
            lines += [
                ("> **Estimated prices** — NSE refused; bought and marked on a "
                 "model price, not a market quote." if b.get("estimated_prices") else ""),
                f"**BUY** {b.get('quantity')} @ {b.get('entry')} "
                f"({b.get('currency', '$')}{b.get('cost')}) at "
                f"{b.get('market_time')} · {b.get('delta')} delta, {b.get('dte')} DTE, "
                f"spread {b.get('spread_pct')}%",
                f"- **Wrong if:** {b.get('invalidation_note')} (stock stop "
                f"{b.get('underlying_stop')}; option backstop {b.get('disaster_stop')})",
                f"- **Why:** {'; '.join(b.get('confirmations') or [])}",
            ]
            committee = b.get("committee") or {}
            for v in committee.get("votes") or []:
                lines.append(f"  - {v['agent']} {v['score']:.2f}"
                             + (" VETO" if v.get("veto") else "")
                             + f" — {'; '.join(v.get('reasons') or [])[:200]}")
        if s:
            lines += ["", f"**SELL** {s.get('exit_reason')} · P&L {s.get('pnl'):+,.2f} · "
                          f"held {s.get('held_minutes')} min"]
            lines += [f"- {x['quantity']} @ {x['price']} ({x['reason']})"
                      for x in s.get("exits") or []]
        else:
            lines += ["", "_Still open._"]
        lines.append("")
    return "\n".join(lines)
