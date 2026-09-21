"""The weekend review — one week of trading, with the reasoning behind each trade.

Three questions this answers, which the live dashboard cannot:

  1. **What did I actually do?** Every trade the desk took, with its plan, its
     outcome and its R-multiple.
  2. **Why did the desk take it?** Each analyst's vote, score and reasoning at
     the moment of entry, plus what the CMIO weighed and what the counter-
     argument was. This is recovered from `agent_reports`, which is written at
     decision time — so it is what the desk actually thought, not a story
     reconstructed afterwards from the outcome.
  3. **What should change?** A coach's read of the week: which setups paid,
     what indiscipline cost, and which rules to tighten.

Question 3 is the only part an LLM touches, and its output is **advice, not
configuration**. Nothing here writes to settings.yaml, adjusts a weight or
resizes a position. The risk desk stays deterministic; a model that has read
one good week must never be able to talk it into a bigger bet.

Without an LLM configured the first two sections are unchanged and the third
falls back to a deterministic summary of the same numbers.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from app.core import clock
from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.journal import store
from app.journal.analytics import analyse

log = get_logger("journal.weekly")


# --------------------------------------------------------------------------- #
# The coach's output. Narrow on purpose — a free-form essay cannot be checked.
# --------------------------------------------------------------------------- #
class RuleChange(BaseModel):
    rule: str = Field(description="The existing rule or setting, named exactly")
    change: str = Field(description="The specific change proposed")
    why: str = Field(description="The evidence from THIS week that supports it")


class CoachNotes(BaseModel):
    headline: str = Field(description="One sentence: the week in a line")
    what_worked: list[str] = Field(default_factory=list)
    what_cost_money: list[str] = Field(default_factory=list)
    rule_changes: list[RuleChange] = Field(default_factory=list)
    focus_next_week: str = Field(description="The single thing to practise")
    generated_by: str = "rules"


# --------------------------------------------------------------------------- #
def week_bounds(anchor: date) -> tuple[date, date]:
    """The Monday-to-Friday trading week containing `anchor`.

    A date at the weekend belongs to the week that has just finished, which is
    the one you want to review on a Saturday.
    """
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=4)


def current_week(cfg: Config | None = None) -> tuple[date, date]:
    cfg = cfg or get_config()
    tz = str(cfg.get("system.timezone", "Asia/Kolkata"))
    return week_bounds(clock.market_now(tz).date())


def is_complete(week_end: date, cfg: Config | None = None) -> bool:
    """Has the week's last session finished?

    The review is readable mid-week — a provisional number beats no number —
    but it has to say which it is, or Wednesday's half-week gets filed as the
    week's verdict.
    """
    cfg = cfg or get_config()
    tz = str(cfg.get("system.timezone", "Asia/Kolkata"))
    today = clock.market_now(tz).date()
    if today > week_end:
        return True
    if today < week_end:
        return False
    return clock.past(tz, str(cfg.get("system.square_off_time", "15:15")))


# --------------------------------------------------------------------------- #
class WeekReview(BaseModel):
    week_start: date
    week_end: date
    market: str
    currency: str = ""
    complete: bool = False
    generated_at: datetime = Field(default_factory=datetime.now)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    agent_scorecard: list[dict[str, Any]] = Field(default_factory=list)
    coach: CoachNotes | None = None

    @property
    def label(self) -> str:
        return f"{self.week_start.isoformat()}_to_{self.week_end.isoformat()}"


def collect(week_start: date, week_end: date,
            cfg: Config | None = None) -> WeekReview:
    """Gather the week from the journal and the decision log."""
    cfg = cfg or get_config()
    rows = store.entries(limit=500, since=week_start.isoformat(),
                         until=week_end.isoformat())
    # entries() returns newest first; a week reads forwards.
    rows = sorted(rows, key=lambda r: str(r.get("ts") or ""))

    trades = [_trade_detail(r) for r in rows]
    return WeekReview(
        week_start=week_start,
        week_end=week_end,
        market=cfg.active_market,
        currency=cfg.market.currency_symbol,
        complete=is_complete(week_end, cfg),
        trades=trades,
        stats=analyse(rows),
        agent_scorecard=score_agents(trades),
    )


def _trade_detail(row: dict[str, Any]) -> dict[str, Any]:
    """One journal row plus the decision trail that produced it."""
    detail: dict[str, Any] = {
        "id": row.get("id"),
        "ts": row.get("ts"),
        "symbol": row.get("symbol"),
        "instrument": row.get("instrument"),
        "setup": row.get("setup"),
        "side": row.get("side"),
        "planned_entry": row.get("planned_entry"),
        "planned_stop": row.get("planned_stop"),
        "planned_target": row.get("planned_target"),
        "actual_entry": row.get("actual_entry"),
        "actual_exit": row.get("actual_exit"),
        "quantity": row.get("actual_quantity") or row.get("planned_quantity"),
        "pnl": row.get("pnl"),
        "r_multiple": row.get("r_multiple"),
        "hold_minutes": row.get("hold_minutes"),
        "verdict": row.get("verdict"),
        "execution_score": row.get("execution_score"),
        "mistakes": _loads(row.get("mistakes")) or [],
        "stop_honoured": bool(row.get("stop_honoured")),
        "notes": row.get("notes") or "",
        "rationale": "",
        "counter_argument": "",
        "composite_score": None,
        "confirmations": [],
        "regime": None,
        "votes": [],
    }

    signal_id = row.get("signal_id")
    if not signal_id:
        return detail

    # The decision trail lives in the signals DB, which is written at decision
    # time. A fresh install has no such table, and a journal-only read must not
    # fail because of it.
    try:
        from app.storage import db

        signal = db.get_signal(signal_id)
        if signal:
            detail.update({
                "rationale": signal.get("rationale") or "",
                "counter_argument": signal.get("counter_argument") or "",
                "composite_score": signal.get("composite_score"),
                "confirmations": _loads(signal.get("confirmations")) or [],
                "regime": signal.get("regime"),
            })
        # One row per analyst, latest first. A signal re-analysed in a later
        # cycle writes a second report, and showing an agent twice would read
        # as two independent confirmations when it is one changing its mind.
        latest: dict[str, dict[str, Any]] = {}
        for r in sorted(db.reports_for_signal(signal_id),
                        key=lambda r: str(r.get("ts") or "")):
            agent = r.get("agent_id")
            if not agent:
                continue
            latest[agent] = {
                "agent": agent,
                "bias": r.get("bias"),
                "score": r.get("score"),
                "confidence": r.get("confidence"),
                "data_available": bool(r.get("data_available")),
                "used_llm": bool(r.get("used_llm")),
                "rationale": r.get("rationale") or "",
            }
        detail["votes"] = list(latest.values())
    except Exception as exc:                     # noqa: BLE001 - never fatal
        log.debug("no decision trail for %s: %s", signal_id, exc)

    return detail


def _loads(raw: Any) -> Any:
    if not raw:
        return None
    if isinstance(raw, list | dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
def score_agents(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which analyst is earning its weight?

    Scored on the trades each one CONFIRMED (|score| >= 0.25 and pointing the
    way the trade was taken), because that is when its opinion actually moved
    the decision. An analyst that abstained is not counted either way — the
    same rule the CMIO votes by.
    """
    tally: dict[str, dict[str, Any]] = {}

    for t in trades:
        r = t.get("r_multiple") or 0.0
        long = str(t.get("side", "BUY")).upper() == "BUY"
        for vote in t.get("votes") or []:
            agent = vote.get("agent")
            if not agent or not vote.get("data_available"):
                continue
            slot = tally.setdefault(agent, {
                "agent": agent, "voted": 0, "confirmed": 0,
                "total_r": 0.0, "wins": 0})
            slot["voted"] += 1

            score = float(vote.get("score") or 0.0)
            agreed = score >= 0.25 if long else score <= -0.25
            if not agreed:
                continue
            slot["confirmed"] += 1
            slot["total_r"] += r
            if r > 0:
                slot["wins"] += 1

    out = []
    for slot in tally.values():
        n = slot["confirmed"]
        out.append({**slot,
                    "avg_r": round(slot["total_r"] / n, 3) if n else 0.0,
                    "win_rate": round(slot["wins"] / n * 100, 1) if n else 0.0,
                    "total_r": round(slot["total_r"], 3)})
    return sorted(out, key=lambda s: s["total_r"], reverse=True)


# --------------------------------------------------------------------------- #
# The coach. Advice only — it reads the week and proposes; it changes nothing.
# --------------------------------------------------------------------------- #
COACH_SYSTEM = (
    "You are a trading coach reviewing one week of a disciplined intraday desk. "
    "You grade PROCESS, not outcome: a profitable trade that broke a rule is a "
    "bad trade, and a losing trade that honoured its stop is an acceptable one. "
    "Every claim you make must cite a number or a trade from the week you are "
    "given — if the evidence is not there, say the sample is too small rather "
    "than inventing a pattern. Four trades is not a trend. You are advising a "
    "human who will decide; you are not changing any setting yourself. Never "
    "propose loosening a stop, widening risk per trade, or lowering the "
    "confirmation requirement to get more trades."
)


async def coach_review(review: WeekReview, cfg: Config | None = None) -> CoachNotes:
    """Ask the configured LLM what to change. Falls back to the rule summary."""
    cfg = cfg or get_config()
    if not cfg.llm_enabled or not review.trades:
        return _rule_notes(review)

    from app.core.llm import structured_complete

    try:
        notes = await structured_complete(
            system=COACH_SYSTEM,
            prompt=_coach_prompt(review),
            schema=CoachNotes,
            max_tokens=2000,
            cfg=cfg,
        )
    except Exception as exc:                     # noqa: BLE001 - never fatal
        log.warning("weekly coach review failed: %s", exc)
        notes = None

    if notes is None:
        fallback = _rule_notes(review)
        fallback.headline = f"{fallback.headline} (the model did not answer)"
        return fallback

    notes.generated_by = cfg.llm_label
    return notes


def _coach_prompt(review: WeekReview) -> str:
    s = review.stats
    cur = review.currency
    lines = [
        f"WEEK {review.week_start} to {review.week_end} ({review.market} market)",
        "" if review.complete else "NOTE: this week is not finished yet.",
        "",
        f"Trades: {s.get('total', 0)} | Win rate: {s.get('win_rate', 0):.0f}% | "
        f"Total: {s.get('total_r', 0):+.2f}R | Avg: {s.get('avg_r', 0):+.2f}R",
        f"Clean execution: {s.get('clean_pct', 0):.0f}% | "
        f"Avg discipline score: {s.get('avg_execution_score', 0):.1f}/10",
        f"Mistake Cost Index: {cur}{s.get('mistake_cost_index', 0):,.0f} "
        f"(losses on rule-breaking trades; clean losses excluded)",
        f"Avoidable share of losses: {s.get('avoidable_loss_pct', 0):.0f}%",
        "",
        "BY SETUP:",
    ]
    for name, v in (s.get("by_setup") or {}).items():
        lines.append(f"  {name}: {v['count']} trades, {v['win_rate']:.0f}% win, "
                     f"{v['avg_r']:+.2f}R avg")
    if s.get("by_mistake"):
        lines.append("")
        lines.append("MISTAKES LOGGED:")
        for tag, v in s["by_mistake"].items():
            lines.append(f"  {tag}: {v['count']}x, {cur}{abs(v['cost']):,.0f} lost")

    if review.agent_scorecard:
        lines += ["", "ANALYST SCORECARD (on the trades each one confirmed):"]
        for a in review.agent_scorecard:
            lines.append(f"  {a['agent']}: confirmed {a['confirmed']}/{a['voted']}, "
                         f"{a['win_rate']:.0f}% win, {a['total_r']:+.2f}R")

    lines += ["", "THE TRADES:"]
    for t in review.trades:
        lines.append(
            f"  {t['symbol']} {t['side']} {t['setup']} | "
            f"entry {t['planned_entry']} stop {t['planned_stop']} "
            f"target {t['planned_target']} | exit {t['actual_exit']} | "
            f"{(t['r_multiple'] or 0):+.2f}R | {t['verdict']} "
            f"({t['execution_score']}/10)"
            + (f" | mistakes: {', '.join(t['mistakes'])}" if t["mistakes"] else ""))
        if t.get("confirmations"):
            lines.append(f"      confirmed by: {'; '.join(t['confirmations'])}")
        if t.get("counter_argument"):
            lines.append(f"      counter-argument at entry: {t['counter_argument'][:200]}")

    lines += [
        "",
        "Review this week. Say what worked, what indiscipline cost, and name "
        "specific rule changes with the evidence from these trades. If the "
        "sample is too small to conclude anything, say so plainly.",
    ]
    return "\n".join(lines)


def _rule_notes(review: WeekReview) -> CoachNotes:
    """The deterministic review: the same numbers, read out honestly.

    This is what you get with no LLM configured, and it is also the floor the
    model's version has to beat — so it states the sample-size caveat rather
    than pretending four trades mean something.
    """
    s = review.stats
    total = s.get("total", 0)
    cur = review.currency

    if not total:
        return CoachNotes(
            headline="No trades were logged this week, so there is nothing to grade.",
            focus_next_week=("Check the desk was armed and that signals were "
                             "reaching the broker — a week with no trades is "
                             "either a quiet market or a switch left off."))

    worked, cost = [], []
    for name, v in sorted((s.get("by_setup") or {}).items(),
                          key=lambda kv: kv[1]["avg_r"], reverse=True):
        line = (f"{name}: {v['count']} trades, {v['win_rate']:.0f}% win rate, "
                f"{v['avg_r']:+.2f}R average")
        (worked if v["avg_r"] > 0 else cost).append(line)

    for tag, v in sorted((s.get("by_mistake") or {}).items(),
                         key=lambda kv: kv[1]["cost"]):
        cost.append(f"{tag}: {v['count']}x, {cur}{abs(v['cost']):,.0f} lost")

    bad_wins = (s.get("by_verdict") or {}).get("BAD_WIN", {}).get("count", 0)
    if bad_wins:
        cost.append(f"{bad_wins} BAD_WIN — made money while breaking a rule. "
                    f"These are the dangerous ones: the P&L rewards the habit "
                    f"that will eventually cost you.")

    mci = s.get("mistake_cost_index", 0.0)
    headline = (f"{total} trades, {s.get('total_r', 0):+.2f}R, "
                f"{s.get('clean_pct', 0):.0f}% clean execution.")
    if total < 20:
        headline += " Too few trades to conclude anything about the strategy."

    focus = ("Keep logging — twenty-plus graded trades is where the numbers "
             "start meaning something.")
    if mci > 0:
        focus = (f"{cur}{mci:,.0f} of this week's losses came from rule breaks, "
                 f"not from the market. That is the cheapest thing to fix.")

    return CoachNotes(headline=headline, what_worked=worked,
                      what_cost_money=cost, focus_next_week=focus)


# --------------------------------------------------------------------------- #
# Rendering and export
# --------------------------------------------------------------------------- #
_AGENT_NAMES = {
    "candlestick": "Candlestick & Technical",
    "derivatives": "Options & Futures (F&O)",
    "news_sentiment": "News & Sentiment",
    "macro_flow": "Macro & Flow",
    "fundamental": "Fundamental Filter",
    "cmio": "CMIO",
}


def to_markdown(review: WeekReview) -> str:
    s = review.stats
    cur = review.currency
    out: list[str] = [
        f"# Weekly Review — {review.week_start} to {review.week_end}",
        "",
        f"*{review.market} market · generated {review.generated_at:%Y-%m-%d %H:%M}*",
        "",
    ]

    if not review.complete:
        out += ["> **This week is not finished.** These numbers are provisional "
                "and will change before Friday's close.", ""]

    if not review.trades:
        out += ["No trades were logged in this window, so there is nothing to "
                "grade. That is either a genuinely quiet week or a switch left "
                "off — check that the desk was armed and that "
                "`AUTO_PLACE_ORDERS` is on.", ""]
        _append_coach(out, review)
        return "\n".join(out)

    # --- scorecard ---------------------------------------------------------
    out += [
        "## Scorecard",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Trades | {s.get('total', 0)} |",
        f"| Win rate | {s.get('win_rate', 0):.0f}% |",
        f"| Total R | {s.get('total_r', 0):+.2f}R |",
        f"| Average R | {s.get('avg_r', 0):+.2f}R |",
        f"| Clean execution | {s.get('clean_pct', 0):.0f}% |",
        f"| Avg discipline score | {s.get('avg_execution_score', 0):.1f}/10 |",
        f"| Mistake Cost Index | {cur}{s.get('mistake_cost_index', 0):,.0f} |",
        f"| Avoidable share of losses | {s.get('avoidable_loss_pct', 0):.0f}% |",
        "",
        "The Mistake Cost Index counts money lost **only** on trades that broke "
        "a rule. A loss on a clean setup is the cost of doing business and is "
        "excluded deliberately — this number isolates what indiscipline alone "
        "cost you.",
        "",
    ]

    if s.get("by_setup"):
        out += ["### By setup", "",
                "| Setup | Trades | Win rate | Avg R |", "|---|---|---|---|"]
        for name, v in sorted((s["by_setup"]).items(),
                              key=lambda kv: kv[1]["avg_r"], reverse=True):
            out.append(f"| {name} | {v['count']} | {v['win_rate']:.0f}% | "
                       f"{v['avg_r']:+.2f}R |")
        out.append("")

    if review.agent_scorecard:
        out += [
            "### Which analyst earned its weight",
            "",
            "Scored on the trades each analyst **confirmed** — when its opinion "
            "actually moved the decision. An abstention counts neither way.",
            "",
            "| Analyst | Confirmed | Win rate | Total R | Avg R |",
            "|---|---|---|---|---|",
        ]
        for a in review.agent_scorecard:
            name = _AGENT_NAMES.get(a["agent"], a["agent"])
            out.append(f"| {name} | {a['confirmed']}/{a['voted']} | "
                       f"{a['win_rate']:.0f}% | {a['total_r']:+.2f}R | "
                       f"{a['avg_r']:+.2f}R |")
        out.append("")

    # --- trade by trade ----------------------------------------------------
    out += ["## Every trade, and why the desk took it", ""]
    for i, t in enumerate(review.trades, 1):
        out += _trade_markdown(i, t, cur)

    _append_coach(out, review)
    return "\n".join(out)


def _trade_markdown(i: int, t: dict[str, Any], cur: str) -> list[str]:
    r = t.get("r_multiple") or 0.0
    pnl = t.get("pnl") or 0.0
    when = str(t.get("ts") or "")[:16].replace("T", " ")

    out = [
        f"### {i}. {t['symbol']} {t['side']} — {r:+.2f}R ({cur}{pnl:,.0f})",
        "",
        f"*{when} · {t.get('setup') or 'Other'} · "
        f"{t.get('instrument') or t['symbol']}*",
        "",
        "| | Planned | What happened |",
        "|---|---|---|",
        f"| Entry | {t.get('planned_entry')} | {t.get('actual_entry')} |",
        f"| Stop | {t.get('planned_stop')} | {'held' if t.get('stop_honoured') else 'moved'} |",
        f"| Target | {t.get('planned_target')} | exited at {t.get('actual_exit')} |",
        f"| Quantity | {t.get('quantity')} | {t.get('quantity')} |",
        f"| Held | — | {(t.get('hold_minutes') or 0):.0f} min |",
        "",
        f"**Verdict: {t.get('verdict') or 'ungraded'}** "
        f"· discipline {t.get('execution_score') or '—'}/10 "
        f"· stop {'honoured' if t.get('stop_honoured') else 'NOT honoured'}",
        "",
    ]

    if t.get("verdict") == "BAD_WIN":
        out += ["> This one made money **while breaking a rule**. It is the most "
                "dangerous kind of trade in the book, because the P&L rewards "
                "the habit that will eventually cost you.", ""]

    if t.get("mistakes"):
        out += [f"**Mistakes logged:** {', '.join(t['mistakes'])}", ""]

    # --- the decision trail ---
    if t.get("votes"):
        out += ["**What each analyst said at entry:**", "",
                "| Analyst | Bias | Score | Confidence | Reasoning |",
                "|---|---|---|---|---|"]
        for v in sorted(t["votes"], key=lambda v: -abs(float(v.get("score") or 0))):
            name = _AGENT_NAMES.get(v["agent"], v["agent"])
            if not v.get("data_available"):
                out.append(f"| {name} | *abstained* | — | — | "
                           f"{_cell(v.get('rationale'))} |")
                continue
            out.append(
                f"| {name} | {v.get('bias') or '—'} | "
                f"{float(v.get('score') or 0):+.2f} | "
                f"{float(v.get('confidence') or 0) * 100:.0f}% | "
                f"{_cell(v.get('rationale'))} |")
        out.append("")

    if t.get("composite_score") is not None:
        out.append(f"**Composite score:** {float(t['composite_score']):+.3f}"
                   + (f" · regime {t['regime']}" if t.get("regime") else ""))
        out.append("")
    if t.get("confirmations"):
        out += [f"**Confirmations:** {'; '.join(t['confirmations'])}", ""]
    if t.get("rationale"):
        out += [f"**CMIO:** {t['rationale']}", ""]
    if t.get("counter_argument"):
        out += ["**Counter-argument recorded at entry** (what would have made "
                f"this wrong): {t['counter_argument']}", ""]
    if t.get("notes"):
        out += [f"*{t['notes']}*", ""]

    out.append("---")
    out.append("")
    return out


def _cell(text: Any) -> str:
    """Markdown tables break on a pipe or a newline inside a cell."""
    s = str(text or "").replace("|", "\\|").replace("\n", " ").strip()
    return (s[:157] + "…") if len(s) > 160 else (s or "—")


def _append_coach(out: list[str], review: WeekReview) -> None:
    coach = review.coach
    if coach is None:
        return
    out += [f"## The coach's read ({coach.generated_by})", "",
            f"**{coach.headline}**", ""]
    if coach.what_worked:
        out += ["### What worked", ""] + [f"- {x}" for x in coach.what_worked] + [""]
    if coach.what_cost_money:
        out += ["### What cost money", ""] + \
               [f"- {x}" for x in coach.what_cost_money] + [""]
    if coach.rule_changes:
        out += ["### Proposed rule changes", "",
                "These are **suggestions for you to decide on**. Nothing here "
                "has been applied: the risk desk stays deterministic, and no "
                "model can change a stop, a position size or the confirmation "
                "requirement.", "",
                "| Rule | Proposed change | Evidence from this week |",
                "|---|---|---|"]
        for c in coach.rule_changes:
            out.append(f"| {_cell(c.rule)} | {_cell(c.change)} | {_cell(c.why)} |")
        out.append("")
    out += ["### Focus next week", "", coach.focus_next_week, ""]


async def build(week_start: date | None = None, week_end: date | None = None,
                with_coach: bool = True,
                cfg: Config | None = None) -> WeekReview:
    """Collect the week and, unless told otherwise, have it coached."""
    cfg = cfg or get_config()
    if week_start is None or week_end is None:
        week_start, week_end = current_week(cfg)
    review = collect(week_start, week_end, cfg)
    if with_coach:
        review.coach = await coach_review(review, cfg)
    return review


def save(review: WeekReview) -> dict[str, Any]:
    """Write the review to journal/weekly/ so it is in the repo and on disk.

    Both formats: markdown to read (and to render on GitHub), JSON so a later
    version can diff one week against another without re-deriving anything.
    """
    folder = store.JOURNAL_DIR / "weekly"
    folder.mkdir(parents=True, exist_ok=True)

    md = folder / f"{review.label}.md"
    js = folder / f"{review.label}.json"
    md.write_text(to_markdown(review), encoding="utf-8")
    js.write_text(review.model_dump_json(indent=2), encoding="utf-8")

    log.info("weekly review written: %s (%d trades, %s)",
             md.name, len(review.trades),
             "final" if review.complete else "provisional")
    return {"markdown": str(md), "json": str(js),
            "label": review.label, "complete": review.complete}
