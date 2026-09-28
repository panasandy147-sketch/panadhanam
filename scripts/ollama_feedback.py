"""Friday self-reflection: the week's graded trades -> Ollama -> strategy weights.

Runs once a week after Friday's session (the scheduler calls `run()` once the
week is complete, and catches up on the next start if the desk was off), or
by hand:

    python -m scripts.ollama_feedback              # the last finished week
    python -m scripts.ollama_feedback --dry-run    # show, write nothing
    python -m scripts.ollama_feedback --answer-file reply.json   # replay

What it does:

  1. Reads the week's CLOSED trades from the journal as JSON — each with its
     process grade (GOOD_WIN, GOOD_LOSS, BAD_WIN, BAD_LOSS), discipline score
     (1-10), R-multiple and mistakes — and, from the signal's own reports,
     which analysts voted for the trade and which against it.
  2. Asks the local Ollama model to judge PROCESS over outcome and return
     weight adjustments for the four voting analysts (the "strategies" of the
     consensus pipeline: candlestick, derivatives, news_sentiment, macro_flow).
  3. Parses the answer defensively (`parse_response`): code fences, prose
     around the JSON, trailing commas, unknown names, strings, NaN and
     booleans are all handled. Anything unusable means NO change.
  4. Bounds it: at most ±`feedback.max_step` (0.15) per week, each multiplier
     kept within 0.25-1.5, so one bad answer cannot silence or double an
     analyst.
  5. Writes config/strategy_weights.json, which agents/consensus.py multiplies
     into the CMIO's vote weights on every cycle. The file is git-ignored, so
     `git pull` never conflicts with what the desk learned on this machine;
     delete it to go back to the shipped weights. A record of every run —
     the input, the raw answer and what was applied — goes to
     journal/reflections/<week>.json.

The multipliers only scale how much each analyst's vote counts. They never
touch a stop, a size, a cap or the circuit breaker.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents import consensus  # noqa: E402
from app.core.logging import get_logger  # noqa: E402

log = get_logger("scripts.ollama_feedback")

AGENTS = consensus.VOTING_AGENTS
WEIGHT_MIN, WEIGHT_MAX = consensus.MULTIPLIER_MIN, consensus.MULTIPLIER_MAX

SYSTEM = (
    "You review one week of a paper-trading desk whose trades are approved by "
    f"a vote of {len(AGENTS)} analysts: " + ", ".join(AGENTS) + ". Tune how much each "
    "analyst's vote counts. Grade PROCESS over outcome: a BAD_WIN (money made "
    "while breaking a rule) is not evidence for the analysts that voted for "
    "it; GOOD_LOSSes with clean execution that an analyst kept voting for ARE "
    "evidence its read is weak; BAD_LOSSes point at execution, not at the "
    "analysts. With fewer than 3 votes from an analyst, leave it at 0.0. "
    "Answer ONLY with JSON: "
    '{"adjustments": {"<analyst>": <number from -0.15 to 0.15>}, '
    '"rationale": "<two sentences citing the numbers>"}'
)


# --------------------------------------------------------------------------- #
# Parsing
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
    """The first balanced {...}, ignoring braces inside strings and fences."""
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
    """Turn a model answer into bounded multipliers, or refuse to.

    Accepts {"adjustments": {name: delta}} (preferred) or
    {"strategy_weights": {name: absolute}}.
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

    if isinstance(data.get("adjustments"), dict):
        source, absolute = data["adjustments"], False
    elif isinstance(data.get("strategy_weights"), dict):
        source, absolute = data["strategy_weights"], True
    else:
        result.errors.append('no "adjustments" or "strategy_weights" object')
        return result

    deltas: dict[str, float] = {}
    for name, value in source.items():
        key = str(name).strip().lower()
        if key not in AGENTS:
            result.errors.append(f"unknown analyst {name!r} ignored")
            continue
        number = _number(value)
        if number is None:
            result.errors.append(f"{name}: {value!r} is not a number")
            continue
        deltas[key] = number - float(current.get(key, 1.0)) if absolute else number
    if not deltas:
        result.errors.append("no usable adjustment")
        return result

    step = abs(float(max_step))
    weights = {k: float(current.get(k, 1.0)) for k in AGENTS}
    for key, delta in deltas.items():
        new = round(max(WEIGHT_MIN, min(WEIGHT_MAX,
                                        weights[key] + max(-step, min(step, delta)))), 3)
        if new != weights[key]:
            result.changes[key] = round(new - weights[key], 3)
        weights[key] = new
    result.weights = weights
    result.rationale = str(data.get("rationale") or "").strip()[:1000]
    result.ok = True
    return result


# --------------------------------------------------------------------------- #
# The week's data
# --------------------------------------------------------------------------- #
def _votes(signal_id: str | None) -> tuple[str, dict[str, float]]:
    """The trade's direction and each analyst's score on it."""
    if not signal_id:
        return "", {}
    from app.storage import db

    try:
        signal = db.get_signal(signal_id) or {}
        reports = db.reports_for_signal(signal_id)
    except Exception:                                  # noqa: BLE001
        return "", {}
    scores = {r["agent_id"]: float(r.get("score") or 0.0) for r in reports
              if r.get("agent_id") in AGENTS}
    return str(signal.get("bias") or ""), scores


def closed_trades(since: date, until: date) -> list[dict[str, Any]]:
    """The week's graded trades as plain JSON-ready dicts."""
    from app.journal import store

    out = []
    for row in store.entries(limit=1000, since=since.isoformat(),
                             until=until.isoformat()):
        try:
            mistakes = json.loads(row.get("mistakes") or "[]")
        except (TypeError, ValueError):
            mistakes = []
        bias, scores = _votes(row.get("signal_id"))
        want = 1 if bias == "BULLISH" else -1 if bias == "BEARISH" else 0
        out.append({
            "date": str(row.get("ts") or "")[:10], "symbol": row.get("symbol"),
            "setup": row.get("setup"), "side": row.get("side"), "bias": bias,
            "verdict": row.get("verdict"), "discipline": row.get("execution_score"),
            "pnl": round(float(row.get("pnl") or 0.0), 2),
            "r_multiple": round(float(row.get("r_multiple") or 0.0), 2),
            "mistakes": mistakes,
            "for": sorted(a for a, s in scores.items() if want and s * want >= 0.25),
            "against": sorted(a for a, s in scores.items() if want and s * want <= -0.25),
        })
    return out


def summarise(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per analyst: the trades it voted for, by process grade, and their R."""
    out = {a: {"voted_for": 0, "voted_against": 0, "GOOD_WIN": 0, "GOOD_LOSS": 0,
               "BAD_WIN": 0, "BAD_LOSS": 0, "r_when_for": 0.0} for a in AGENTS}
    for t in trades:
        for agent in t["for"]:
            s = out[agent]
            s["voted_for"] += 1
            s["r_when_for"] = round(s["r_when_for"] + t["r_multiple"], 2)
            if t.get("verdict") in s:
                s[t["verdict"]] += 1
        for agent in t["against"]:
            out[agent]["voted_against"] += 1
    return out


def current_multipliers() -> dict[str, float]:
    saved = consensus.load_multipliers()
    return {a: float(saved.get(a, 1.0)) for a in AGENTS}


async def ask_ollama(cfg: Any, payload: dict[str, Any]) -> str | None:
    """The model's raw answer, or None when Ollama is not there."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=float(cfg.ollama_timeout)) as client:
            r = await client.post(f"{cfg.ollama_host.rstrip('/')}/api/chat", json={
                "model": cfg.ollama_model, "stream": False, "format": "json",
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": json.dumps(payload, default=str)}],
                "options": {"temperature": 0.1, "num_predict": 600}})
    except Exception as exc:                          # noqa: BLE001 - never fatal
        log.warning("Ollama unavailable (%s) — strategy weights unchanged", exc)
        return None
    if r.status_code != 200:
        log.warning("Ollama answered HTTP %s — strategy weights unchanged", r.status_code)
        return None
    return (r.json().get("message") or {}).get("content", "")


def write_weights(path: Path, weights: dict[str, float], week: str, rationale: str) -> None:
    """Replace config/strategy_weights.json atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"_comment": "Written by scripts/ollama_feedback.py after Friday's close. "
                        "Multipliers on the CMIO vote weights. Safe to delete.",
            "week": week, "updated": datetime.now().isoformat(timespec="seconds"),
            "rationale": rationale, "strategy_weights": weights}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
@dataclass
class Feedback:
    """What one run did."""
    week: str
    applied: bool
    trades: int
    weights: dict[str, float] = field(default_factory=dict)
    changes: dict[str, float] = field(default_factory=dict)
    note: str = ""
    record: str = ""


def last_finished_week(cfg: Any) -> tuple[date, date]:
    from app.journal import weekly

    start, end = weekly.current_week(cfg)
    if not weekly.is_complete(end, cfg):
        start, end = start - timedelta(days=7), end - timedelta(days=7)
    return start, end


def record_path(week: str) -> Path:
    from app.journal import store
    return store.JOURNAL_DIR / "reflections" / f"{week}.json"


async def run(cfg: Any, week_start: date, week_end: date,
              answer: str | None = None, apply: bool = True) -> Feedback:
    """The week's feedback. `answer` skips Ollama (tests, replays)."""
    week = f"{week_start.isoformat()}_to_{week_end.isoformat()}"
    trades = closed_trades(week_start, week_end)
    before = current_multipliers()
    out = Feedback(week=week, applied=False, trades=len(trades), weights=before)
    payload = {"week": week, "current_multipliers": before,
               "by_analyst": summarise(trades), "trades": trades}

    minimum = int(cfg.get("feedback.min_trades", 5))
    parsed: ParseResult | None = None
    if len(trades) < minimum:
        out.note = (f"{len(trades)} closed trade(s) — fewer than {minimum}, too few "
                    f"to learn from; weights unchanged")
    else:
        if answer is None:
            answer = await ask_ollama(cfg, payload)
        if answer is None:
            out.note = "Ollama did not answer; weights unchanged"
        else:
            parsed = parse_response(answer, before, float(cfg.get("feedback.max_step", 0.15)))
            if not parsed.ok:
                out.note = "unusable model answer: " + "; ".join(parsed.errors)
            else:
                out.weights, out.changes = parsed.weights, parsed.changes
                out.note = parsed.rationale or "weights adjusted"
                if apply:
                    write_weights(consensus.STRATEGY_WEIGHTS_PATH, parsed.weights,
                                  week, parsed.rationale)
                    out.applied = True

    if apply:
        path = record_path(week)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "week": week, "applied": out.applied, "note": out.note,
            "multipliers_before": before, "multipliers_after": out.weights,
            "changes": out.changes, "parse_errors": parsed.errors if parsed else [],
            "model_answer": answer, "input": payload}, indent=2, default=str),
            encoding="utf-8")
        out.record = str(path)
    log.info("Friday feedback %s: %s%s", week, out.note,
             f" — {out.changes}" if out.changes else "")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="ask and parse, but write nothing")
    parser.add_argument("--answer-file", help="use this saved model answer instead of Ollama")
    parser.add_argument("--week-end", help="the Friday to review (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    from app.core.config import get_config

    cfg = get_config()
    if args.week_end:
        end = date.fromisoformat(args.week_end)
        start = end - timedelta(days=end.weekday())
    else:
        start, end = last_finished_week(cfg)
    answer = (Path(args.answer_file).read_text(encoding="utf-8")
              if args.answer_file else None)
    done = asyncio.run(run(cfg, start, end, answer=answer, apply=not args.dry_run))
    print(f"Week {done.week}: {done.trades} closed trade(s)")
    print(f"  {done.note}")
    for agent, weight in done.weights.items():
        change = done.changes.get(agent)
        print(f"  {agent:<16} x{weight:.2f}" + (f"  ({change:+.2f})" if change else ""))
    if done.applied:
        print(f"  written: {consensus.STRATEGY_WEIGHTS_PATH}")
    if done.record:
        print(f"  record:  {done.record}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
