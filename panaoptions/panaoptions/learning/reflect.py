"""The Friday self-reflection: the week's graded trades in, strategy weights out.

Once Friday's session has closed, the week's closed-trade journal goes to the
local Ollama model as JSON, with each trade's PROCESS grade (GOOD_WIN,
GOOD_LOSS, BAD_WIN, BAD_LOSS and the 1-10 discipline score) alongside its P&L.
The model answers with a JSON object of strategy weight adjustments, which is:

  1. parsed defensively (parse_response): code fences, prose around the
     object, trailing commas, wrong types, unknown strategies and NaN are all
     handled — anything unusable means NO change, never a guess;
  2. bounded: no weight moves more than `reflection.max_step` in a week, and
     none leaves 0.25-1.5, so one bad model answer cannot switch a strategy
     off or double its size;
  3. written to journal/reflections/<week>.json (the full record: the prompt's
     data, the raw answer, what was applied and why), and
  4. patched into config/learned.yaml, which config.py merges over the
     settings on every load. That file is git-ignored, so `git pull` never
     conflicts with what the desk learned on this machine.

The weights only scale the committee score (agents/cmio.py). They never touch
a stop, a size or a risk limit.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from panaoptions.logging import get_logger
from panaoptions.models import SetupType

log = get_logger("reflect")

STRATEGIES: tuple[str, ...] = ("pd_liquidity_sweep", "orb_vwap", "vwap_ema_pullback",
                               "liquidity_sweep",
                               "candlestick_at_level", "va_rejection",
                               "lvn_acceleration", "poc_bounce")
WEIGHT_MIN, WEIGHT_MAX = 0.25, 1.5

SYSTEM = (
    "You review one week of an options paper-trading desk and tune how much "
    "the committee trusts each strategy. Grade PROCESS over outcome: a "
    "BAD_WIN (money made while breaking a rule) is not evidence for a "
    "strategy; repeated GOOD_LOSSes with clean execution ARE evidence the "
    "strategy's edge is weak; BAD_LOSSes point at execution, not the strategy. "
    "With fewer than 3 trades for a strategy, leave it at 0.0. "
    "Answer ONLY with JSON of the form "
    '{"adjustments": {"<strategy>": <number between -0.15 and 0.15>}, '
    '"rationale": "<two sentences citing the numbers>"}. '
    "Strategies: " + ", ".join(STRATEGIES) + "."
)


# --------------------------------------------------------------------------- #
@dataclass
class ParseResult:
    """What came out of the model's answer, and whether it may be applied."""
    ok: bool
    weights: dict[str, float] = field(default_factory=dict)
    changes: dict[str, float] = field(default_factory=dict)
    rationale: str = ""
    errors: list[str] = field(default_factory=list)


def _extract_object(text: str) -> str | None:
    """The first balanced {...} in `text`, ignoring braces inside strings."""
    text = re.sub(r"```(?:json)?", "", text, flags=re.I)
    start = text.find("{")
    while start != -1:
        depth, in_str, escape = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def _loads(blob: str) -> Any:
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        # Small models like a trailing comma; nothing else is repaired.
        return json.loads(re.sub(r",\s*([}\]])", r"\1", blob))


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_response(text: str | None, current: dict[str, float],
                   max_step: float = 0.15) -> ParseResult:
    """Turn a model answer into bounded strategy weights, or refuse to.

    Accepts {"adjustments": {name: delta}} (preferred) or
    {"strategy_weights": {name: absolute}}. Unknown strategies and
    non-numbers are dropped with a note; if nothing usable remains the result
    is not ok and the caller changes nothing.
    """
    result = ParseResult(ok=False)
    if not text or not str(text).strip():
        result.errors.append("empty response")
        return result
    blob = _extract_object(str(text))
    if blob is None:
        result.errors.append("no JSON object in the response")
        return result
    try:
        data = _loads(blob)
    except json.JSONDecodeError as exc:
        result.errors.append(f"malformed JSON: {exc.msg} at char {exc.pos}")
        return result
    if not isinstance(data, dict):
        result.errors.append("the JSON is not an object")
        return result

    deltas: dict[str, float] = {}
    raw_adj = data.get("adjustments")
    raw_abs = data.get("strategy_weights")
    if isinstance(raw_adj, dict):
        source, absolute = raw_adj, False
    elif isinstance(raw_abs, dict):
        source, absolute = raw_abs, True
    else:
        result.errors.append('no "adjustments" or "strategy_weights" object')
        return result

    for name, value in source.items():
        key = str(name).strip().lower()
        if key not in STRATEGIES:
            result.errors.append(f"unknown strategy {name!r} ignored")
            continue
        number = _number(value)
        if number is None:
            result.errors.append(f"{name}: {value!r} is not a number")
            continue
        base = float(current.get(key, 1.0))
        deltas[key] = (number - base) if absolute else number

    if not deltas:
        result.errors.append("no usable adjustment")
        return result

    step = abs(float(max_step))
    weights = {k: float(current.get(k, 1.0)) for k in STRATEGIES}
    for key, delta in deltas.items():
        bounded = max(-step, min(step, delta))
        new = round(max(WEIGHT_MIN, min(WEIGHT_MAX, weights[key] + bounded)), 3)
        if new != weights[key]:
            result.changes[key] = round(new - weights[key], 3)
        weights[key] = new
    result.weights = weights
    result.rationale = str(data.get("rationale") or "").strip()[:1000]
    result.ok = True
    return result


# --------------------------------------------------------------------------- #
def strategy_key(value: str) -> str:
    """'ORB + VWAP' (as the journal stores it) -> 'orb_vwap'."""
    for member in SetupType:
        if value in (member.value, member.name, member.name.lower()):
            return member.name.lower()
    return str(value).lower()


def closed_trades(since: str, until: str) -> list[dict[str, Any]]:
    """The week's graded trades as plain JSON-ready dicts."""
    from panaoptions import audit
    from panaoptions.journal.store import entries

    trail = audit.by_trade(audit.entries(since=date.fromisoformat(since),
                                         until=date.fromisoformat(until)))
    out = []
    for row in entries(limit=1000, since=since, until=until):
        try:
            mistakes = json.loads(row.get("mistakes") or "[]")
        except (TypeError, ValueError):
            mistakes = []
        out.append({
            "date": row.get("session_date"), "symbol": row.get("symbol"),
            "strategy": strategy_key(str(row.get("strategy") or "")),
            "pattern": row.get("pattern") or "",
            "direction": row.get("direction"),
            "verdict": row.get("verdict"),
            "discipline": row.get("execution_score"),
            "pnl": round(float(row.get("pnl") or 0.0), 2),
            "return_pct": round(float(row.get("return_pct") or 0.0), 1),
            "exit": row.get("exit_reason"), "mistakes": mistakes,
            **_audit_fields(trail.get(str(row.get("trade_id")), {})),
        })
    return out


def _audit_fields(pair: dict[str, Any]) -> dict[str, Any]:
    """The audit's reasons for one trade, trimmed for the prompt."""
    buy, sell = pair.get("buy") or {}, pair.get("sell") or {}
    if not buy and not sell:
        return {}
    committee = buy.get("committee") or {}
    return {"why": "; ".join(buy.get("confirmations") or [])[:300],
            "committee_score": committee.get("score"),
            "held_minutes": sell.get("held_minutes")}


def summarise(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per strategy: count, process grades, P&L — the numbers the model cites."""
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        s = out.setdefault(t["strategy"], {"trades": 0, "pnl": 0.0, "GOOD_WIN": 0,
                                           "GOOD_LOSS": 0, "BAD_WIN": 0,
                                           "BAD_LOSS": 0, "discipline": []})
        s["trades"] += 1
        s["pnl"] = round(s["pnl"] + t["pnl"], 2)
        if t.get("verdict") in s:
            s[t["verdict"]] += 1
        if t.get("discipline") is not None:
            s["discipline"].append(int(t["discipline"]))
    for s in out.values():
        scores = s.pop("discipline")
        s["avg_discipline"] = round(sum(scores) / len(scores), 1) if scores else None
    return out


def current_weights(cfg: Any) -> dict[str, float]:
    return {k: float(cfg.get(f"strategy_weights.{k}", 1.0)) for k in STRATEGIES}


async def ask_ollama(cfg: Any, payload: dict[str, Any]) -> str | None:
    """The model's raw answer, or None when Ollama is not there."""
    from panaoptions.ml import llm

    try:
        async with httpx.AsyncClient(
                timeout=float(cfg.get("journal.ollama_timeout", 120))) as client:
            r = await client.post(f"{llm.host(cfg)}/api/chat", json={
                "model": llm.model(cfg), "stream": False, "format": "json",
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user",
                              "content": json.dumps(payload, default=str)}],
                "options": {"temperature": 0.1, "num_predict": 600}})
    except Exception as exc:                          # noqa: BLE001 - never fatal
        log.warning("reflection: Ollama unavailable (%s) — no weights changed", exc)
        return None
    if r.status_code != 200:
        log.warning("reflection: Ollama answered HTTP %s — no weights changed",
                    r.status_code)
        return None
    return (r.json().get("message") or {}).get("content", "")


def write_learned(path: Path, weights: dict[str, float], week: str,
                  rationale: str) -> None:
    """Replace config/learned.yaml atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"week": week, "updated": datetime.now().isoformat(timespec="seconds"),
            "rationale": rationale, "strategy_weights": weights}
    header = ("# Written by the panaoptions Friday reflection. Merged over\n"
              "# config/settings.yaml on every load. Safe to delete: the desk\n"
              "# goes back to the shipped weights.\n")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(header + yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


@dataclass
class Reflection:
    """What one Friday's reflection did."""
    week: str
    applied: bool
    trades: int
    weights: dict[str, float] = field(default_factory=dict)
    changes: dict[str, float] = field(default_factory=dict)
    note: str = ""
    record: str = ""


async def reflect(cfg: Any, week_start: date, week_end: date,
                  answer: str | None = None, apply: bool = True) -> Reflection:
    """Run the week's reflection. `answer` skips Ollama (tests, replays)."""
    from panaoptions import config as config_mod
    from panaoptions.journal import store as journal_store

    week = f"{week_start.isoformat()}_to_{week_end.isoformat()}"
    trades = closed_trades(week_start.isoformat(), week_end.isoformat())
    before = current_weights(cfg)
    out = Reflection(week=week, applied=False, trades=len(trades), weights=before)
    payload = {"week": week, "current_weights": before,
               "by_strategy": summarise(trades), "trades": trades}

    minimum = int(cfg.get("reflection.min_trades", 5))
    parsed: ParseResult | None = None
    if len(trades) < minimum:
        out.note = (f"{len(trades)} closed trade(s) — fewer than {minimum}, "
                    f"too few to learn from; weights unchanged")
    else:
        if answer is None:
            answer = await ask_ollama(cfg, payload)
        if answer is None:
            out.note = "Ollama did not answer; weights unchanged"
        else:
            parsed = parse_response(answer, before,
                                    float(cfg.get("reflection.max_step", 0.15)))
            if not parsed.ok:
                out.note = "unusable model answer: " + "; ".join(parsed.errors)
            else:
                out.weights, out.changes = parsed.weights, parsed.changes
                out.note = parsed.rationale or "weights adjusted"
                if apply:
                    write_learned(config_mod.LEARNED_PATH, parsed.weights, week,
                                  parsed.rationale)
                    cfg.data["strategy_weights"] = dict(parsed.weights)
                    out.applied = True

    folder = journal_store.JOURNAL_DIR / "reflections"
    folder.mkdir(parents=True, exist_ok=True)
    record = folder / f"{week}.json"
    record.write_text(json.dumps({
        "week": week, "applied": out.applied, "note": out.note,
        "weights_before": before, "weights_after": out.weights,
        "changes": out.changes,
        "parse_errors": parsed.errors if parsed else [],
        "model_answer": answer, "input": payload}, indent=2, default=str),
        encoding="utf-8")
    out.record = str(record)
    log.info("reflection %s: %s%s", week, out.note,
             f" — {out.changes}" if out.changes else "")
    return out
