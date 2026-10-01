"""What the setups the desk did NOT take went on to do — and a coach's read.

A day with no trades teaches nothing unless the setups that were refused are
followed. After the close, every fired setup the audit log recorded as
REFUSED (confluence, committee, contract, …) or SKIP (a hard risk gate) is
walked forward on the day's closed bars from the moment it was refused:

  target first          -> +reward/risk R (the plan's own target)
  stop first            -> -1R (a bar that touches both counts as the stop)
  neither by square-off -> the R it stood at on the last bar

One setup counts once a day (its first refusal, per symbol, side and
strategy), so a setup refused every minute is not twenty missed winners.
The R is on the underlying, the same measure as the r_multiple exit plan;
an option's premium would have moved by its delta, not one for one.

The totals by gate say what each gate COST (missed winners) and SAVED
(dodged losers). That is evidence, not a decision: a gate is loosened only
after the 20-session walk-forward agrees (python run.py --backtest).

Then, when `learning.daily_coach` is on, the local Ollama model reads the
day — trades taken, the missed setups by gate, the commonest reasons each
strategy did not fire — and writes at most three suggestions, each naming a
setting. They are written down and NEVER applied: no stop, size or risk
limit changes because a model said so.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from panaoptions.logging import get_logger

log = get_logger("missed")

COACH = (
    "You coach a paper-trading options desk in a trading competition. You get "
    "one session: the trades it took and, for every setup it REFUSED, what "
    "that setup went on to do on the underlying in R (target first = a win, "
    "stop first = -1R). Name which gate cost the most missed R and which "
    "saved the most, and suggest at most three concrete changes to TEST, "
    "each naming one setting. Never suggest more risk per trade, a wider "
    "daily loss limit or removing a stop. One day is a small sample: say so "
    "when the numbers are thin. Answer ONLY with JSON: "
    '{"summary": "<two or three sentences citing the numbers>", '
    '"suggestions": [{"setting": "<key>", "change": "<what>", "why": "<numbers>"}]}'
)


def _local(ts: Any, tz: ZoneInfo) -> datetime:
    if ts.tzinfo is None:
        from datetime import UTC
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(tz).replace(tzinfo=None)


def setups(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The day's refused setups with a usable plan, first refusal of each
    (symbol, side, strategy); later refusals add their gate to `gates`."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in events:
        if e.get("event") not in ("REFUSED", "SKIP"):
            continue
        entry = e.get("entry") or e.get("spot")
        stop, target = e.get("stop"), e.get("target")
        if not (entry and stop and target) or entry == stop:
            continue
        key = (str(e.get("symbol")), str(e.get("side")), str(e.get("strategy")))
        gate = str(e.get("gate") or e.get("event"))
        if key in out:
            if gate not in out[key]["gates"]:
                out[key]["gates"].append(gate)
            continue
        out[key] = {"time": str(e.get("market_time") or "")[:16], "symbol": key[0],
                    "side": key[1], "strategy": key[2], "pattern": e.get("pattern"),
                    "gate": gate, "gates": [gate], "reason": e.get("reason"),
                    "entry": float(entry), "stop": float(stop), "target": float(target)}
    return list(out.values())


def follow(setup: dict[str, Any], candles: list[Any], tz: str,
           square_off: str = "15:50") -> dict[str, Any]:
    """Walk one refused setup forward on the bars after it was refused."""
    zone = ZoneInfo(tz)
    entry, stop, target = setup["entry"], setup["stop"], setup["target"]
    long = target > entry
    risk = abs(entry - stop)
    try:
        start = datetime.strptime(setup["time"], "%Y-%m-%d %H:%M")
    except ValueError:
        return {**setup, "outcome": "no data", "r": None}
    hh, mm = (int(x) for x in square_off.split(":"))
    end = start.replace(hour=hh, minute=mm)
    bars = [c for c in candles if start <= _local(c.ts, zone) < end]
    if not bars:
        return {**setup, "outcome": "no data", "r": None}
    for c in bars:
        hit_stop = c.low <= stop if long else c.high >= stop
        hit_target = c.high >= target if long else c.low <= target
        when = _local(c.ts, zone).strftime("%H:%M")
        if hit_stop:
            return {**setup, "outcome": "stop", "r": -1.0, "at": when}
        if hit_target:
            return {**setup, "outcome": "target",
                    "r": round(abs(target - entry) / risk, 2), "at": when}
    last = bars[-1].close
    r = (last - entry) / risk * (1 if long else -1)
    return {**setup, "outcome": "open at close", "r": round(r, 2),
            "at": _local(bars[-1].ts, zone).strftime("%H:%M")}


def by_gate(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per gate: how many it refused, how they ended, the R it cost or saved."""
    out: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"setups": 0, "would_win": 0, "would_lose": 0, "flat": 0, "r": 0.0})
    for r in results:
        if r.get("r") is None:
            continue
        g = out[r["gate"]]
        g["setups"] += 1
        g["r"] = round(g["r"] + r["r"], 2)
        if r["outcome"] == "target" or r["r"] > 0.5:
            g["would_win"] += 1
        elif r["outcome"] == "stop" or r["r"] < -0.5:
            g["would_lose"] += 1
        else:
            g["flat"] += 1
    return dict(out)


async def review(cfg: Any, day: date, feed: Any) -> dict[str, Any]:
    """Follow the day's refused setups and, if asked, have the coach read it."""
    from panaoptions import audit

    events = audit.entries(day)
    todo = setups(events)
    tf = str(cfg.get("technical.timeframe", "5m"))
    tz = str(cfg.timezone)
    square_off = str(cfg.get("session.force_exit_at", "15:50"))
    bars: dict[str, list[Any]] = {}
    results = []
    for s in todo:
        if s["symbol"] not in bars:
            try:
                bars[s["symbol"]] = list(await feed.candles(s["symbol"], tf) or [])
            except Exception as exc:                    # noqa: BLE001
                log.debug("missed setups: no bars for %s: %s", s["symbol"], exc)
                bars[s["symbol"]] = []
        results.append(follow(s, bars[s["symbol"]], tz, square_off))
    record = {"day": day.isoformat(), "setups": results, "by_gate": by_gate(results)}
    if bool(cfg.get("learning.daily_coach", True)):
        record["coach"] = await coach(cfg, day, events, record)
    audit.record_missed(cfg, record)
    return record


def coach_payload(cfg: Any, day: date, events: list[dict[str, Any]],
                  record: dict[str, Any]) -> dict[str, Any]:
    from panaoptions import audit
    trades = []
    for t in audit.by_trade(events).values():
        b, s = t.get("buy") or {}, t.get("sell") or {}
        planned = ((b.get("card") or {}).get("sizing") or {}).get("planned_risk")
        trades.append({"strategy": b.get("strategy") or s.get("strategy"),
                       "side": b.get("side"), "pnl": s.get("pnl"),
                       "r": round(s["pnl"] / planned, 2) if planned and s.get("pnl")
                       is not None else None, "exit": s.get("exit_reason")})
    why_not: dict[str, list[str]] = {}
    try:
        from panaoptions.ledger import store
        rows = sorted(store.checks(day.isoformat()), key=lambda r: -int(r["n"]))
        for r in rows:
            if r["outcome"] != "fired":
                why_not.setdefault(r["strategy"], [])
                if len(why_not[r["strategy"]]) < 3:
                    why_not[r["strategy"]].append(f"{r['reason']} ({r['n']})")
    except Exception:                                   # noqa: BLE001
        pass
    return {"market": getattr(cfg, "market", "US"), "day": day.isoformat(),
            "trades": trades, "missed_by_gate": record["by_gate"],
            "missed": [{k: s.get(k) for k in ("time", "symbol", "side", "strategy",
                                              "gate", "outcome", "r")}
                       for s in record["setups"]][:30],
            "why_strategies_did_not_fire": why_not,
            "rules": {"risk_per_trade_pct": cfg.get("risk.max_risk_per_trade_pct"),
                      "max_daily_trades": cfg.get("risk.max_daily_trades"),
                      "committee_threshold": cfg.get("agents.approve_threshold"),
                      "sweep_band_pct": cfg.get("strategies.pd_liquidity_sweep.proximity_pct"),
                      "min_reward_risk": cfg.get("risk.min_reward_risk")}}


async def coach(cfg: Any, day: date, events: list[dict[str, Any]],
                record: dict[str, Any]) -> dict[str, Any] | None:
    """Ollama's read of the day — advice only, never applied."""
    from panaoptions.agents.base import llm_enabled
    from panaoptions.ml import llm

    if not llm_enabled(cfg):
        return None
    payload = coach_payload(cfg, day, events, record)
    try:
        async with httpx.AsyncClient(
                timeout=float(cfg.get("journal.ollama_timeout", 120))) as client:
            r = await client.post(f"{llm.host(cfg)}/api/chat", json={
                "model": llm.model(cfg), "stream": False, "format": "json",
                "messages": [{"role": "system", "content": COACH},
                             {"role": "user", "content": json.dumps(payload, default=str)}],
                "options": {"temperature": 0.2, "num_predict": 700}})
    except Exception as exc:                            # noqa: BLE001
        log.info("daily coach: Ollama unavailable (%s)", exc)
        return None
    if r.status_code != 200:
        return None
    return parse_coach((r.json().get("message") or {}).get("content", ""))


def parse_coach(text: str | None) -> dict[str, Any] | None:
    """The model's answer, defensively: a summary and up to 3 suggestions."""
    from panaoptions.learning.reflect import _extract_object, _loads
    blob = _extract_object(text or "")
    if not blob:
        return None
    try:
        data = _loads(blob)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    tips = []
    for s in data.get("suggestions") or []:
        if isinstance(s, dict) and s.get("change"):
            tips.append({k: str(s.get(k) or "")[:300] for k in ("setting", "change", "why")})
    return {"summary": str(data.get("summary") or "")[:800], "suggestions": tips[:3]}


def markdown(record: dict[str, Any]) -> list[str]:
    """The missed-setups section of the day's audit."""
    results = record.get("setups") or []
    out = ["## What the refused setups did next", "",
           "Each fired setup the desk did not take, followed on the underlying from "
           "its first refusal to square-off: target first = its planned R, stop "
           "first = -1R, neither = where it stood at the close. Evidence for the "
           "walk-forward, not a decision.", ""]
    gates = record.get("by_gate") or {}
    if not results:
        return out + ["No refused setup with a stop and target today.", ""]
    out += ["| Gate | Setups | Would have won | Would have lost | Flat | Net R |",
            "|---|---|---|---|---|---|"]
    for g, v in sorted(gates.items(), key=lambda x: -x[1]["r"]):
        out.append(f"| {g} | {v['setups']} | {v['would_win']} | {v['would_lose']} | "
                   f"{v['flat']} | {v['r']:+.2f} |")
    out += ["", "| Time | Symbol | Side | Strategy | Gate | Entry | Stop | Target | "
                "Outcome | R |", "|---|---|---|---|---|---|---|---|---|---|"]
    for s in results:
        out.append(f"| {s['time'][11:16]} | {s['symbol']} | {s['side']} | {s['strategy']} | "
                   f"{s['gate']} | {s['entry']:,.2f} | {s['stop']:,.2f} | "
                   f"{s['target']:,.2f} | {s['outcome']}"
                   + (f" {s['at']}" if s.get("at") else "") + " | "
                   + ("—" if s.get("r") is None else f"{s['r']:+.2f}") + " |")
    out.append("")
    c = record.get("coach")
    if c:
        out += ["### Coach (Ollama) — advice only, never applied", "", c.get("summary", ""), ""]
        out += [f"- **{t['setting']}**: {t['change']} — {t['why']}"
                for t in c.get("suggestions") or []]
        out.append("")
    return out
