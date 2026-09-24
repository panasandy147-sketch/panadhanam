"""Why a trade was bought, and why it was sold — in words.

The database holds every number: entry, stop, target, exit, R, P&L, and the
analysts' votes. None of that is an answer to "why did it buy?" or "why did it
sell?" until it is put into a sentence, and until now nothing did. These are
the sentences the dashboard, the day report and the notifications read from,
so all three say the same thing.
"""
from __future__ import annotations

import json
from typing import Any


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _agent_name(confirmation: str) -> str:
    """'candlestick: bullish (+0.70)' -> 'Candlestick bullish (+0.70)'."""
    names = {
        "candlestick": "Candlestick & technicals",
        "derivatives": "Options & futures",
        "news_sentiment": "News",
        "macro_flow": "Macro & FII flow",
    }
    agent, _, rest = confirmation.partition(":")
    return f"{names.get(agent.strip(), agent.strip())}{':' if rest else ''}{rest}"


def why_bought(row: dict[str, Any]) -> dict[str, Any]:
    """The case for the entry, as it stood BEFORE the outcome was known."""
    confirmations = [_agent_name(c) for c in _list(row.get("confirmations"))]
    rationale = str(row.get("rationale") or "")
    # The rationale is "<vote> | Sizing: <how the stop was set> | <trim>".
    vote, _, rest = rationale.partition(" | Sizing: ")
    sizing = rest.split(" | ")[0] if rest else ""

    entry, stop, target = (_num(row.get(k)) for k in ("entry", "stop_loss", "target"))
    rr = _num(row.get("risk_reward"))
    plan = ""
    if entry is not None and stop is not None and target is not None:
        plan = (f"Entered at {entry:,.2f} with the stop at {stop:,.2f} and the "
                f"target at {target:,.2f}"
                + (f" — {rr:.1f}R of reward for 1R of risk." if rr else "."))

    side = str(row.get("side") or "").upper()
    direction = "long" if side == "BUY" else "short" if side == "SELL" else ""
    headline = (f"{len(confirmations)} analysts agreed on a {direction}"
                if confirmations and direction
                else f"{len(confirmations)} analysts agreed" if confirmations
                else "")
    return {
        "headline": headline,
        "confirmations": confirmations,
        "vote": vote.strip(),
        "stop_basis": sizing.strip(),
        "plan": plan,
        "counter_argument": str(row.get("counter_argument") or ""),
    }


def why_sold(row: dict[str, Any]) -> str:
    """How the position ended, or how it will, in one sentence."""
    status = str(row.get("status") or "")
    entry, stop, target, exit_price, r = (
        _num(row.get(k))
        for k in ("entry", "stop_loss", "target", "exit_price", "r_multiple"))

    def fmt(v: float | None) -> str:
        return "—" if v is None else f"{v:,.2f}"

    r_text = "" if r is None else f" ({'+' if r >= 0 else ''}{r:.2f}R)"
    if status == "CLOSED_TARGET":
        return (f"Target {fmt(target)} reached — sold at {fmt(exit_price)}"
                f"{r_text}. The move the setup called for happened.")
    if status == "CLOSED_STOP":
        return (f"Stop {fmt(stop)} hit — sold at {fmt(exit_price)}{r_text}. "
                f"The level that would prove the idea wrong gave way, so the "
                f"planned loss was taken rather than hoping.")
    if status == "CLOSED_TIME":
        detail = str(row.get("exit_detail") or "")
        if detail == "time_stop":
            cause = "no follow-through within the time stop"
        elif detail == "square_off":
            cause = "the session's square-off came before the stop or target"
        else:
            cause = ("no follow-through within the time stop, or the "
                     "session's square-off came first")
        return (f"Closed on time at {fmt(exit_price)}{r_text} — {cause}. An "
                f"idea that has not worked in its window is treated as wrong.")
    if status in {"OPEN", "APPROVED"}:
        return (f"Still held — sells at the target {fmt(target)}, the stop "
                f"{fmt(stop)}, the time stop, or square-off, whichever comes "
                f"first.")
    if status == "REJECTED":
        return "Not bought."
    return status.replace("_", " ").lower()
