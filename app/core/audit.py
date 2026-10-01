"""The audit log: every paper BUY and SELL, with everything behind it.

One append-only JSON-lines file per market and market day,
journal/audit/<us|in>/YYYY-MM-DD.jsonl, plus a readable .md beside it
rewritten after each event. India and the US never share a file: their
sessions, currency and instruments differ, and so does the review. It is
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


def audit_root() -> Path:
    from app.journal import store
    return store.JOURNAL_DIR / "audit"


def audit_dir(market: str = "US") -> Path:
    return audit_root() / str(market or "US").lower()


def _folders(market: str | None) -> list[Path]:
    """One market's folder, or every market's (and the pre-split root)."""
    root = audit_root()
    if market:
        return [audit_dir(market)]
    if not root.is_dir():
        return []
    return [root] + sorted(p for p in root.iterdir() if p.is_dir())


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
    market = str(record.get("market") or "US")
    try:
        folder = audit_dir(market)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{day.isoformat()}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        (folder / f"{day.isoformat()}.md").write_text(day_markdown(day, market),
                                                      encoding="utf-8")
    except OSError as exc:
        log.warning("could not write the audit log: %s", exc)
    return record


# --------------------------------------------------------------------------- #
def _r(x: Any, nd: int = 2) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else round(v, nd)             # NaN -> None


def _local(ts: Any, tz: str) -> str:
    from zoneinfo import ZoneInfo
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    except (AttributeError, ValueError):
        return str(ts)


_CARD_SECTIONS = ("The vote", "What it buys, and the stop and target")


def card_rules(cfg: Any, setup: str = "") -> list[dict[str, Any]]:
    """The rules page's sections behind an entry — the vote, the stop and
    target, and the named strategy's own section when one led — with the
    values the live config sets."""
    try:
        from app.core import rules
        out = []
        for section in rules.build(cfg).get("sections") or []:
            title = str(section.get("title") or "")
            if not (title.startswith(_CARD_SECTIONS)
                    or (setup and title.startswith("Strategy:")
                        and "sweep" in setup.lower() and "Sweep" in title)):
                continue
            out.append({"title": title,
                        "rules": [{"text": r.get("text"), "value": r.get("value")}
                                  for r in section.get("rules") or []]})
        return out
    except Exception as exc:                            # noqa: BLE001
        log.debug("rules for the trade card: %s", exc)
        return []


def trade_card(cfg: Any, signal: Any, ctx: Any = None) -> dict[str, Any]:
    """The full case for one entry, for a contest review: the candles the
    analysts read (the last is the trigger bar), the levels and figures, the
    patterns found, the rules it had to pass, the stop and target in R, and
    the sizing arithmetic against the risk cap."""
    tz = str(cfg.get("system.timezone", "Asia/Kolkata"))
    ind = dict(getattr(ctx, "indicators", None) or {})
    tf = str(ind.get("primary_timeframe") or cfg.get("technical.primary_timeframe", "5m"))
    card: dict[str, Any] = {"timeframe": tf, "currency": str(getattr(
        getattr(cfg, "market", None), "currency_symbol", "") or "")}
    candles = (getattr(ctx, "candles", None) or {}).get(tf) or []
    n = int(cfg.get("audit.card_candles", 6) or 6)
    card["candles"] = [{"time": _local(c.ts, tz), "open": _r(c.open), "high": _r(c.high),
                        "low": _r(c.low), "close": _r(c.close), "volume": _r(c.volume, 0),
                        "colour": "green" if c.close > c.open else
                        "red" if c.close < c.open else "doji"} for c in candles[-n:]]
    prim = ind.get("primary") or {}
    prev = ind.get("previous_day") or {}
    card["levels"] = {"previous_high": _r(prev.get("high")),
                      "previous_low": _r(prev.get("low")),
                      "previous_close": _r(prev.get("close")),
                      "swing_high": _r(prim.get("high")), "swing_low": _r(prim.get("low"))}
    card["pd_sweep"] = ind.get("pd_sweep") or None
    card["indicators"] = {
        "close": _r(prim.get("last_close")), "vwap": _r(prim.get("vwap")),
        **{k: _r(prim.get(k)) for k in prim if str(k).startswith("ema")
           and str(k)[3:].isdigit()},
        "atr": _r(prim.get("atr"), 3), "atr_pct": _r(prim.get("atr_pct"), 3),
        "rsi": _r(prim.get("rsi"), 1), "volume_surge": _r(prim.get("volume_surge")),
        "macd_hist": _r(prim.get("macd_hist"), 4), "supertrend": prim.get("supertrend"),
        "regime": prim.get("regime"),
        "timeframes_agree": (ind.get("mtf_alignment") or {}).get("aligned"),
    }
    card["patterns"] = {t: [p.get("name") for p in (snap.get("patterns") or [])][:6]
                        for t, snap in (ind.get("by_timeframe") or {}).items()
                        if snap.get("patterns")}

    entry, stop, target = (float(signal.entry or 0), float(signal.stop_loss or 0),
                           float(signal.target or 0))
    risk_u = abs(entry - stop)
    card["plan"] = {
        "entry": _r(entry), "stop": _r(stop), "target": _r(target),
        "risk_per_unit": _r(risk_u, 4), "reward_per_unit": _r(abs(target - entry), 4),
        "reward_risk": _r(abs(target - entry) / risk_u) if risk_u else None,
        "entry_spot": _r(signal.entry_spot), "underlying_stop": _r(signal.underlying_stop),
        "underlying_stop_note": signal.underlying_stop_note or None,
        "spot_risk_atr": (_r(abs(signal.entry_spot - signal.underlying_stop)
                             / prim["atr"])
                          if signal.entry_spot and signal.underlying_stop
                          and prim.get("atr") else None),
    }
    capital = float(cfg.get("risk.total_capital", 0) or 0)
    risk_pct = float(cfg.get("risk.risk_per_trade_pct", 0) or 0)
    cap = capital * risk_pct / 100.0
    card["sizing"] = {
        "capital": _r(capital), "risk_cap_pct": risk_pct or None,
        "risk_cap": _r(cap) if cap else None,
        "risk_per_unit": _r(signal.risk_per_unit or risk_u, 4),
        "units_at_cap": int(cap // risk_u) if cap and risk_u else None,
        "quantity": signal.quantity, "lots": signal.lots, "unit_size": signal.unit_size,
        "unit": signal.unit_label, "planned_risk": _r(signal.total_risk),
        "planned_risk_pct": _r(signal.capital_at_risk_pct, 3),
        "notional": _r(signal.notional),
        "note": str(signal.rationale or "").partition(" | Sizing: ")[2] or None,
    }
    card["rules"] = card_rules(cfg, str(getattr(signal, "setup", "") or ""))
    return card


def record_buy(cfg: Any, signal: Any, order: dict[str, Any] | None = None,
               ctx: Any = None) -> dict[str, Any]:
    """A filled entry, with the whole case for it."""
    from app.core.explain import why_bought
    from app.core.version import code_version

    try:
        card = trade_card(cfg, signal, ctx)
    except Exception as exc:                            # noqa: BLE001
        log.warning("could not build the trade card: %s", exc)
        card = None

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
        "event": "BUY" if side == "BUY" else "SELL_SHORT", "order_tag": "BASE_ENTRY",
        "signal_id": signal.id, "code_version": signal.code_version or code_version(),
        "setup": getattr(signal, "setup", "") or None,
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
        "card": card,
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


def record_add(cfg: Any, row: dict[str, Any], *, level: int, quantity: int, price: float,
               new_stop: float, avg_entry: float, total_qty: int, open_r: float,
               note: str, refused: str = "") -> dict[str, Any]:
    """A pyramid add to a winning position (or the stop step alone)."""
    return _write(cfg, {
        "event": "ADD", "order_tag": "PYRAMID_ADD", "signal_id": row.get("id"), "symbol": row.get("symbol"),
        "tradingsymbol": row.get("tradingsymbol"), "side": row.get("side"),
        "level": level, "quantity": quantity, "price": round(float(price), 4),
        "stop_loss": round(float(new_stop), 4), "avg_entry": round(float(avg_entry), 4),
        "total_quantity": total_qty, "open_r": open_r, "note": note,
        "refused": refused or None,
    })


# --------------------------------------------------------------------------- #
def entries(day: date | None = None, since: date | None = None,
            until: date | None = None, market: str | None = None) -> list[dict[str, Any]]:
    """Audit events for one day or a date range, in time order — for one
    market, or for every market when `market` is None."""
    out: list[dict[str, Any]] = []
    for folder in _folders(market):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.jsonl")):
            stem = path.stem
            if day and stem != day.isoformat():
                continue
            if since and stem < since.isoformat():
                continue
            if until and stem > until.isoformat():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return sorted(out, key=lambda e: str(e.get("ts") or ""))


def days(since: date, until: date, market: str | None = None) -> list[tuple[str, str]]:
    """(market, day) pairs that have an audit file in the range."""
    out = set()
    for folder in _folders(market):
        if folder.is_dir():
            for p in folder.glob("*.jsonl"):
                if since.isoformat() <= p.stem <= until.isoformat():
                    code = folder.name.upper() if folder != audit_root() else "US"
                    out.add((code, p.stem))
    return sorted(out, key=lambda x: (x[1], x[0]))


_OPENS = {"BUY", "SELL_SHORT"}


def _event_date(e: dict[str, Any]) -> str:
    return str(e.get("market_time") or e.get("ts") or "")[:10]


def _why_text(why: Any) -> str:
    """explain.why_bought's dict (or an older string) as one line."""
    if isinstance(why, dict):
        parts = [why.get("headline") or "",
                 ("(" + ", ".join(why.get("confirmations") or []) + ")")
                 if why.get("confirmations") else "",
                 why.get("vote") or ""]
        return " ".join(p for p in parts if p).strip()
    return str(why or "")


def by_day(since: date, until: date, market: str | None = None) -> list[dict[str, Any]]:
    """Every buy and sell from `since` to `until`, grouped by market day, in
    order — what the weekly review shows date by date, mid-week included."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in entries(since=since, until=until, market=market):
        grouped.setdefault(_event_date(e), []).append(e)
    out = []
    for day in sorted(grouped):
        rows = []
        for e in grouped[day]:
            opening = e.get("event") in _OPENS
            rows.append({
                "time": str(e.get("market_time") or "")[11:16],
                "event": e.get("event"), "market": e.get("market"),
                "symbol": e.get("symbol"),
                "instrument": e.get("tradingsymbol") or e.get("symbol"),
                "quantity": e.get("quantity"),
                "price": e.get("entry") if opening else e.get("exit_price"),
                "stop": e.get("stop_loss") if opening else None,
                "target": e.get("target") if opening else None,
                "amount": e.get("notional") if opening else None,
                "risk": e.get("total_risk") if opening else None,
                "pnl": None if opening else e.get("pnl"),
                "r_multiple": None if opening else e.get("r_multiple"),
                "held_minutes": None if opening else e.get("held_minutes"),
                "reason": (_why_text(e.get("why")) if opening else
                           e.get("exit_detail") or e.get("why_sold")) or "",
                "counter_argument": e.get("counter_argument") if opening else None,
                "setup": ((e.get("volume_profile") or {}).get("name")
                          if opening else "") or "",
                "signal_id": e.get("signal_id"),
            })
        sells = [r for r in rows if r["pnl"] is not None]
        wd = date.fromisoformat(day).strftime("%a") if len(day) == 10 else ""
        out.append({"date": day, "weekday": wd, "events": rows,
                    "buys": len(rows) - len(sells), "sells": len(sells),
                    "wins": sum(1 for r in sells if (r["pnl"] or 0) > 0),
                    "losses": sum(1 for r in sells if (r["pnl"] or 0) < 0),
                    "pnl": round(sum(r["pnl"] or 0.0 for r in sells), 2)})
    return out


def by_signal(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{signal_id: {"buy": {...}, "sell": {...}}} — a trade's two halves."""
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        half = "sell" if e.get("event") in {"SELL", "BUY_TO_COVER"} else "buy"
        out.setdefault(str(e.get("signal_id")), {})[half] = e
    return out


def day_markdown(day: date, market: str = "US") -> str:
    """The day's audit, readable: one block per trade, entry then exit."""
    trades = by_signal(entries(day, market=market))
    lines = [f"# Audit — {market.upper()} — {day.isoformat()}", "",
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
            lines += _card_markdown(b)
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


def _num(v: Any, nd: int = 2) -> str:
    return "—" if v is None else f"{v:,.{nd}f}"


def _card_markdown(b: dict[str, Any]) -> list[str]:
    """The BUY's trade card, readable."""
    card = b.get("card") or {}
    if not card:
        return []
    out = ["", "<details open><summary><b>Trade card</b></summary>", ""]
    bars = card.get("candles") or []
    if bars:
        out += [f"**Candles** ({card.get('timeframe', '5m')}, closed; the last row is "
                "the trigger bar)", "",
                "| Time | Open | High | Low | Close | Volume | |",
                "|---|---|---|---|---|---|---|"]
        for i, c in enumerate(bars):
            mark = " **trigger**" if i == len(bars) - 1 else ""
            out.append(f"| {c['time']} | {_num(c['open'])} | {_num(c['high'])} | "
                       f"{_num(c['low'])} | {_num(c['close'])} | {_num(c['volume'], 0)} | "
                       f"{c['colour']}{mark} |")
        out.append("")
    lv = card.get("levels") or {}
    out.append(f"**Levels** — PDH {_num(lv.get('previous_high'))} · PDL "
               f"{_num(lv.get('previous_low'))} · PDC {_num(lv.get('previous_close'))} · "
               f"recent swing {_num(lv.get('swing_low'))}–{_num(lv.get('swing_high'))}")
    sw = card.get("pd_sweep") or {}
    if sw:
        out.append("**PD sweep** — " + ", ".join(f"{k} {v}" for k, v in sw.items()
                                                 if v not in (None, "", [], {})))
    ind = card.get("indicators") or {}
    emas = " / ".join(f"{_num(ind[k])}" for k in sorted(
        (k for k in ind if k.startswith("ema")), key=lambda k: int(k[3:])))
    ema_names = "/".join(k[3:] for k in sorted(
        (k for k in ind if k.startswith("ema")), key=lambda k: int(k[3:])))
    out.append(f"**Figures at entry** — close {_num(ind.get('close'))} · VWAP "
               f"{_num(ind.get('vwap'))} · EMA {ema_names} {emas} · ATR "
               f"{_num(ind.get('atr'), 3)} ({_num(ind.get('atr_pct'), 3)}%) · RSI "
               f"{_num(ind.get('rsi'), 1)} · volume {_num(ind.get('volume_surge'))}x avg · "
               f"regime {ind.get('regime') or '—'} · timeframes agree: "
               f"{'yes' if ind.get('timeframes_agree') else 'no'}")
    pats = card.get("patterns") or {}
    if pats:
        out.append("**Patterns found** — " + "; ".join(
            f"{tf}: {', '.join(p for p in names if p)}" for tf, names in pats.items()))
    p = card.get("plan") or {}
    out += ["", f"**Stop and target** — entry {_num(p.get('entry'))}, stop "
                f"{_num(p.get('stop'))} (1R = {_num(p.get('risk_per_unit'), 4)} a unit), "
                f"target {_num(p.get('target'))} ({_num(p.get('reward_risk'))}R)"
                + (f"; underlying {_num(p.get('entry_spot'))} → stop "
                   f"{_num(p.get('underlying_stop'))}"
                   + (f" ({p['spot_risk_atr']:.2f} ATR)" if p.get("spot_risk_atr") else "")
                   if p.get("underlying_stop") else "")]
    if p.get("underlying_stop_note"):
        out.append(f"- {p['underlying_stop_note']}")
    z = card.get("sizing") or {}
    cur = card.get("currency") or ""
    out += ["", f"**Sizing** — capital {cur}{_num(z.get('capital'), 0)}"
                + (f"; the {_num(z.get('risk_cap_pct'), 1)}% risk cap is "
                   f"{cur}{_num(z.get('risk_cap'))} = {z.get('units_at_cap')} units at "
                   f"{_num(z.get('risk_per_unit'), 4)} a unit" if z.get("risk_cap") else "")
                + f". Bought **{z.get('quantity')}**"
                + (f" ({z.get('lots')} {z.get('unit')}{'' if z.get('lots') == 1 else 's'} "
                   f"of {z.get('unit_size')})" if (z.get("unit_size") or 1) > 1 else "")
                + f", notional {cur}{_num(z.get('notional'))} → planned risk "
                f"{cur}{_num(z.get('planned_risk'))} ({_num(z.get('planned_risk_pct'), 2)}% "
                "of capital)."]
    for sec in card.get("rules") or []:
        out += ["", f"**Rules — {sec['title']}**"]
        out += [f"- {r['text']}" + (f" — **{r['value']}**" if r.get("value") else "")
                for r in sec.get("rules") or []]
    out += ["", "</details>"]
    return out
