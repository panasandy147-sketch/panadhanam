"""The audit log: every paper BUY and SELL, with everything behind it.

One append-only JSON-lines file per market day, journal/audit/YYYY-MM-DD.jsonl,
plus a readable journal/audit/YYYY-MM-DD.md rewritten after each event. It is
written at the moment of the fill and the moment of the exit, so it records
what the desk knew THEN — not a reconstruction after the result is known.

A BUY line carries: the instrument and size, entry / stop / underlying stop /
target, R:R and money at risk, the CMIO's composite and confirmations, the
counter-argument, every analyst's score and one-line reason, the
volume-profile setup if one led, the sizing note, and the code version.

A SELL line carries: the exit price, P&L, R-multiple, how it closed (target,
stop, time, square-off, circuit breaker) in a sentence, and how long it was
held.

Read by the weekly review, the Friday Ollama feedback (scripts/
ollama_feedback.py), and by you on the weekend.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.core import clock
from app.core.logging import get_logger

log = get_logger("audit")


def audit_dir() -> Path:
    from app.journal import store
    return store.JOURNAL_DIR / "audit"


def _market_day(cfg: Any) -> date:
    return clock.market_now(str(cfg.get("system.timezone", "Asia/Kolkata"))).date()


def _value(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _write(cfg: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Append one event; never raises — an audit failure must not stop trading."""
    day = _market_day(cfg)
    record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"),
              "market_time": clock.market_now(str(cfg.get("system.timezone", "Asia/Kolkata")))
              .strftime("%Y-%m-%d %H:%M:%S %Z"),
              "market": getattr(cfg, "active_market", ""), **record}
    try:
        folder = audit_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{day.isoformat()}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        (folder / f"{day.isoformat()}.md").write_text(day_markdown(day), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write the audit log: %s", exc)
    return record


# --------------------------------------------------------------------------- #
def record_buy(cfg: Any, signal: Any, order: dict[str, Any] | None = None) -> dict[str, Any]:
    """A filled entry, with the whole case for it."""
    from app.core.explain import why_bought
    from app.core.version import code_version

    inst = signal.instrument
    reports = []
    setup = None
    for r in getattr(signal, "reports", None) or []:
        reports.append({"agent": r.agent_id, "score": round(float(r.score), 3),
                        "bias": _value(r.bias), "confidence": round(float(r.confidence), 2),
                        "available": bool(r.data_available),
                        "invalidation": r.invalidation_level,
                        "reason": (r.rationale or "")[:300]})
        if r.agent_id == "volume_profile" and (r.extra or {}).get("setup"):
            setup = {"setup": r.extra.get("setup"), "name": r.extra.get("setup_name"),
                     "level": r.extra.get("level_name"), "target": r.suggested_target}
    rationale = str(signal.rationale or "")
    side = _value(signal.side)
    why = why_bought({"side": side, "entry": signal.entry, "stop_loss": signal.stop_loss,
                      "target": signal.target, "risk_reward": signal.risk_reward,
                      "confirmations": signal.confirmations, "rationale": rationale,
                      "counter_argument": signal.counter_argument})
    return _write(cfg, {
        "event": "BUY" if side == "BUY" else "SELL_SHORT",
        "signal_id": signal.id, "code_version": signal.code_version or code_version(),
        "symbol": inst.symbol, "tradingsymbol": inst.tradingsymbol,
        "instrument_type": _value(inst.instrument_type), "side": side,
        "bias": _value(signal.bias), "quantity": signal.quantity,
        "unit": signal.unit_label, "entry": signal.entry, "stop_loss": signal.stop_loss,
        "underlying_stop": signal.underlying_stop,
        "underlying_stop_note": signal.underlying_stop_note,
        "target": signal.target, "risk_reward": signal.risk_reward,
        "total_risk": signal.total_risk, "notional": signal.notional,
        "capital_at_risk_pct": signal.capital_at_risk_pct,
        "entry_spot": signal.entry_spot, "entry_delta": signal.entry_delta,
        "composite_score": signal.composite_score,
        "confirmations": list(signal.confirmations or []),
        "counter_argument": signal.counter_argument,
        "why": why, "sizing": rationale.partition(" | Sizing: ")[2],
        "volume_profile": setup, "analysts": reports,
        "regime": _value(signal.regime) if signal.regime else None,
        "order": order or {},
    })


def record_sell(cfg: Any, row: dict[str, Any], *, exit_price: float, pnl: float,
                r_multiple: float, status: str, detail: str,
                exit_reason: str) -> dict[str, Any]:
    """A closed position: what it made, and how it ended."""
    held = None
    try:
        opened = datetime.fromisoformat(str(row.get("ts")))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        held = round((datetime.now(UTC) - opened).total_seconds() / 60.0, 1)
    except (TypeError, ValueError):
        pass
    return _write(cfg, {
        "event": "SELL" if row.get("side") == "BUY" else "BUY_TO_COVER",
        "signal_id": row.get("id"), "symbol": row.get("symbol"),
        "tradingsymbol": row.get("tradingsymbol"),
        "instrument_type": row.get("instrument_type"),
        "quantity": row.get("quantity"), "entry": row.get("entry"),
        "stop_loss": row.get("stop_loss"), "target": row.get("target"),
        "exit_price": round(float(exit_price), 4), "pnl": round(float(pnl), 2),
        "r_multiple": round(float(r_multiple), 3), "status": status,
        "exit_detail": detail, "why_sold": exit_reason, "held_minutes": held,
    })


# --------------------------------------------------------------------------- #
def entries(day: date | None = None, since: date | None = None,
            until: date | None = None) -> list[dict[str, Any]]:
    """Audit events for one day, or for a date range, in order."""
    folder = audit_dir()
    if not folder.is_dir():
        return []
    days = [day] if day else sorted(
        date.fromisoformat(p.stem) for p in folder.glob("*.jsonl")
        if (since is None or p.stem >= since.isoformat())
        and (until is None or p.stem <= until.isoformat()))
    out: list[dict[str, Any]] = []
    for d in days:
        path = folder / f"{d.isoformat()}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def by_signal(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{signal_id: {"buy": {...}, "sell": {...}}} — a trade's two halves."""
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        half = "sell" if e.get("event") in {"SELL", "BUY_TO_COVER"} else "buy"
        out.setdefault(str(e.get("signal_id")), {})[half] = e
    return out


def day_markdown(day: date) -> str:
    """The day's audit, readable: one block per trade, entry then exit."""
    trades = by_signal(entries(day))
    lines = [f"# Audit — {day.isoformat()}", "",
             f"{len(trades)} trade(s). Every entry is written at the fill, every "
             f"exit at the close — what the desk knew then.", ""]
    for sid, t in trades.items():
        b, s = t.get("buy") or {}, t.get("sell") or {}
        head = b or s
        lines.append(f"## {head.get('symbol')} {head.get('tradingsymbol') or ''} — {sid}")
        if b:
            lines += [
                f"**{b.get('event')}** {b.get('quantity')} {b.get('unit') or ''} @ "
                f"{b.get('entry')} at {b.get('market_time')} · stop {b.get('stop_loss')}"
                + (f" (underlying {b.get('underlying_stop')})" if b.get("underlying_stop") else "")
                + f" · target {b.get('target')} · R:R {b.get('risk_reward')} · "
                f"risk {b.get('total_risk')}",
                "",
                f"- **Why:** {(b.get('why') or {}).get('headline', '')}",
                f"- **Composite:** {b.get('composite_score')} · confirmations: "
                f"{'; '.join(b.get('confirmations') or []) or '—'}",
            ]
            if b.get("volume_profile"):
                vp = b["volume_profile"]
                lines.append(f"- **Volume profile:** {vp.get('name')} at the {vp.get('level')}")
            lines.append(f"- **Against it:** {b.get('counter_argument') or '—'}")
            lines.append(f"- **Stop placed:** {b.get('sizing') or b.get('underlying_stop_note') or '—'}")
            for a in b.get("analysts") or []:
                lines.append(f"  - {a['agent']} {a['score']:+.2f}"
                             + ("" if a.get("available", True) else " (abstained)")
                             + f" — {a.get('reason', '')[:160]}")
        if s:
            lines += ["", f"**{s.get('event')}** @ {s.get('exit_price')} at "
                          f"{s.get('market_time')} · {s.get('status')} · "
                          f"P&L {s.get('pnl'):+,.2f} ({s.get('r_multiple'):+.2f}R) · held "
                          f"{s.get('held_minutes')} min",
                      f"- **How it ended:** {s.get('why_sold')}"]
        else:
            lines += ["", "_Still open._"]
        lines.append("")
    return "\n".join(lines)
