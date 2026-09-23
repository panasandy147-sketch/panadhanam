"""Period reviews — one day or one week of trading, graded and read back.

The question these exist to answer is the one a P&L number cannot: of the
strategies, which are actually working, and how much of the damage was
self-inflicted.

One module serves both periods because they differ only in their bounds and in
how firmly they should warn you off reading anything into the sample. A daily
review of two trades is a diary entry, not evidence, and it says so.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from panaoptions import clock
from panaoptions.config import ROOT
from panaoptions.journal.analytics import analyse
from panaoptions.journal.store import entries
from panaoptions.logging import get_logger

log = get_logger("journal.weekly")

WEEKLY_DIR = ROOT / "journal" / "weekly"
DAILY_DIR = ROOT / "journal" / "daily"

# Below this many trades a period cannot support a conclusion about a
# strategy. A day almost never clears it; that is the point of saying so.
_MEANINGFUL_SAMPLE = 20


class RuleChange(BaseModel):
    rule: str = Field(description="The setting or strategy, named exactly")
    change: str = Field(description="The specific change proposed")
    why: str = Field(description="Evidence from THIS week")


class Coach(BaseModel):
    headline: str = Field(description="The week in one sentence")
    what_worked: list[str] = Field(default_factory=list)
    what_cost_money: list[str] = Field(default_factory=list)
    rule_changes: list[RuleChange] = Field(default_factory=list)
    focus_next_week: str = Field(description="The single thing to practise")
    generated_by: str = "rules"


class Review(BaseModel):
    week_start: date
    week_end: date
    period: str = "week"            # "day" | "week"
    complete: bool = False
    generated_at: datetime = Field(default_factory=datetime.now)
    trades: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    coach: Coach | None = None

    @property
    def label(self) -> str:
        if self.period == "day":
            return self.week_start.isoformat()
        return f"{self.week_start.isoformat()}_to_{self.week_end.isoformat()}"

    @property
    def title(self) -> str:
        if self.period == "day":
            return f"Daily Review — {self.week_start}"
        return f"Weekly Review — {self.week_start} to {self.week_end}"


def week_bounds(anchor: date) -> tuple[date, date]:
    """Monday to Friday. A weekend belongs to the week that just finished —
    which is the one you want to review on a Saturday."""
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=4)


def current_week(cfg) -> tuple[date, date]:
    return week_bounds(clock.now(cfg.timezone).date())


def today(cfg) -> date:
    return clock.now(cfg.timezone).date()


def session_over(cfg, day: date | None = None) -> bool:
    """Has that session finished? A review written mid-session is a guess."""
    day = day or today(cfg)
    now = clock.now(cfg.timezone).date()
    if now > day:
        return True
    if now < day:
        return False
    return clock.at_or_after(cfg.timezone,
                             str(cfg.get("session.force_exit_at", "15:45")))


def is_complete(week_end: date, cfg) -> bool:
    today = clock.now(cfg.timezone).date()
    if today > week_end:
        return True
    if today < week_end:
        return False
    return clock.at_or_after(cfg.timezone,
                             str(cfg.get("session.force_exit_at", "15:45")))


def collect(week_start: date, week_end: date, cfg,
            period: str = "week") -> Review:
    rows = entries(limit=500, since=week_start.isoformat(),
                   until=week_end.isoformat())
    rows = sorted(rows, key=lambda r: str(r.get("ts") or ""))
    complete = (session_over(cfg, week_end) if period == "day"
                else is_complete(week_end, cfg))
    return Review(week_start=week_start, week_end=week_end, period=period,
                  complete=complete, trades=rows, stats=analyse(rows))


# --------------------------------------------------------------------------- #
def _system(period: str) -> str:
    span = "one trading day" if period == "day" else "one week"
    caveat = (
        "A single day is a diary entry, not evidence. Do NOT draw conclusions "
        "about which strategy works from one session — comment on the "
        "execution of the trades in front of you and leave the strategy "
        "question to the weekly review."
        if period == "day" else
        "If the sample is too small to conclude anything, say so rather than "
        "inventing a pattern — five trades is not a trend."
    )
    return (
        f"You are a trading coach reviewing {span} on a small options paper "
        f"account running three named strategies. You grade PROCESS, not "
        f"outcome. Every claim must cite a number from what you are given. "
        f"{caveat} You advise; the person decides. Never propose a wider stop, "
        f"a larger position, or removing a confirmation to get more trades."
    )


async def coach_review(review: Review, cfg) -> Coach:
    notes = _rule_coach(review, cfg)
    if not bool(cfg.get("journal.use_llm", False)) or not review.trades:
        return notes

    try:
        from panaoptions.ml.llm import structured_complete

        answer = await structured_complete(
            system=_system(review.period), prompt=_prompt(review, cfg),
            schema=Coach, cfg=cfg, max_tokens=1200)
    except Exception as exc:                     # noqa: BLE001 - never fatal
        log.debug("weekly coach unavailable: %s", exc)
        answer = None

    if answer is None:
        notes.headline += " (the model did not answer)"
        return notes
    answer.generated_by = str(cfg.get("journal.llm_label", "a local model"))
    return answer


def _prompt(review: Review, cfg) -> str:
    s = review.stats
    currency = str(cfg.get("account.currency", "$"))
    header = (f"DAY {review.week_start}" if review.period == "day"
              else f"WEEK {review.week_start} to {review.week_end}")
    unfinished = ("NOTE: this session is not over yet." if review.period == "day"
                  else "NOTE: this week is not finished yet.")
    lines = [
        header,
        "" if review.complete else unfinished,
        "",
        f"Trades: {s['total']} | Win rate: {s['win_rate']:.0f}% | "
        f"P&L: {currency}{s['total_pnl']:+,.2f}",
        f"Clean execution: {s['clean_pct']:.0f}% | "
        f"Avg discipline: {s['avg_execution_score']:.1f}/10",
        f"Lost to rule breaks: {currency}{s['mistake_cost']:,.2f} "
        f"({s['avoidable_loss_pct']:.0f}% of all losses)",
        "",
        "BY STRATEGY:",
    ]
    for name, v in s["by_strategy"].items():
        lines.append(f"  {name}: {v['count']} trades, {v['win_rate']:.0f}% win, "
                     f"{currency}{v['total_pnl']:+,.2f}, "
                     f"discipline {v['avg_score']:.1f}/10")
    if s["by_mistake"]:
        lines += ["", "RULES BROKEN:"]
        for tag, v in s["by_mistake"].items():
            lines.append(f"  {tag}: {v['count']}x, "
                         f"{currency}{abs(v['cost']):,.2f} lost")
    lines += ["", "THE TRADES:"]
    for t in review.trades:
        lines.append(
            f"  {t['symbol']} {t['direction']} {t['strategy']} | "
            f"{t['entry_price']:.2f} -> {t['exit_price']:.2f} "
            f"({t['exit_reason']}) | {currency}{t['pnl']:+,.2f} | "
            f"{t['verdict']} ({t['execution_score']}/10)")
    if review.period == "day":
        lines += ["", "How well was this session executed? Comment on the "
                      "trades in front of you — not on which strategy is best."]
    else:
        lines += ["", "Which of the strategies is worth keeping, and "
                      "what should change? Say plainly if the sample is too "
                      "small."]
    return "\n".join(lines)


def _rule_coach(review: Review, cfg) -> Coach:
    s = review.stats
    daily = review.period == "day"
    currency = str(cfg.get("account.currency", "$"))
    if not s["total"]:
        return Coach(
            headline=("No trades were taken today." if daily else
                      "No trades were graded this week."),
            focus_next_week=("Check the rejection tally: a rule that never "
                             "passes and a quiet market look identical from "
                             "the outside." if daily else
                             "Check the desk was armed and that a strategy "
                             "window actually opened — `--check-config` and "
                             "the rejection tally say which."))

    worked, cost = [], []
    for name, v in sorted(s["by_strategy"].items(),
                          key=lambda kv: -kv[1]["avg_pnl"]):
        line = (f"{name}: {v['count']} trades, {v['win_rate']:.0f}% win rate, "
                f"{currency}{v['total_pnl']:+,.2f}, discipline "
                f"{v['avg_score']:.1f}/10")
        (worked if v["total_pnl"] > 0 else cost).append(line)

    for tag, v in sorted(s["by_mistake"].items(), key=lambda kv: kv[1]["cost"]):
        cost.append(f"{tag}: {v['count']}x, {currency}{abs(v['cost']):,.2f} lost")

    bad_wins = s["by_verdict"].get("BAD_WIN", 0)
    if bad_wins:
        cost.append(f"{bad_wins} BAD_WIN — made money while breaking a rule. "
                    f"These are the dangerous ones: the P&L rewards the habit "
                    f"that will eventually cost you.")

    headline = (f"{s['total']} trades, {currency}{s['total_pnl']:+,.2f}, "
                f"{s['clean_pct']:.0f}% clean execution.")
    if daily:
        headline += (" One session says how you executed, not which strategy "
                     "works — that is the weekly review's job.")
    elif s["total"] < _MEANINGFUL_SAMPLE:
        headline += " Too few trades to conclude anything about the strategies."

    focus = ("Tomorrow: repeat the clean entries and nothing else." if daily else
             "Keep logging — twenty-plus graded trades is where a strategy "
             "comparison starts meaning something.")
    if s["mistake_cost"] > 0:
        focus = (f"{currency}{s['mistake_cost']:,.2f} of this week's losses came "
                 f"from rule breaks rather than the market. That is the "
                 f"cheapest thing to fix.")

    return Coach(headline=headline, what_worked=worked, what_cost_money=cost,
                 focus_next_week=focus)


# --------------------------------------------------------------------------- #
async def build(cfg, week_start: date | None = None,
                week_end: date | None = None, with_coach: bool = True,
                period: str = "week") -> Review:
    if week_start is None or week_end is None:
        if period == "day":
            week_start = week_end = today(cfg)
        else:
            week_start, week_end = current_week(cfg)
    review = collect(week_start, week_end, cfg, period=period)
    if with_coach:
        review.coach = await coach_review(review, cfg)
    return review


async def build_daily(cfg, day: date | None = None,
                      with_coach: bool = True) -> Review:
    day = day or today(cfg)
    return await build(cfg, day, day, with_coach=with_coach, period="day")


def to_markdown(review: Review, cfg) -> str:
    s = review.stats
    currency = str(cfg.get("account.currency", "$"))
    out = [f"# {review.title}", "",
           f"*panaoptions · generated {review.generated_at:%Y-%m-%d %H:%M}*", ""]

    if not review.complete:
        out += ["> **This "
                + ("session is not over" if review.period == "day"
                   else "week is not finished")
                + ".** These numbers are provisional.", ""]
    if not s["total"]:
        out += ["No trades were graded in this window.", ""]
        return "\n".join(out)

    out += [
        "## Scorecard", "",
        "| Metric | Value |", "|---|---|",
        f"| Trades | {s['total']} |",
        f"| Win rate | {s['win_rate']:.0f}% |",
        f"| P&L | {currency}{s['total_pnl']:+,.2f} |",
        f"| Clean execution | {s['clean_pct']:.0f}% |",
        f"| Avg discipline | {s['avg_execution_score']:.1f}/10 |",
        f"| Lost to rule breaks | {currency}{s['mistake_cost']:,.2f} |",
        f"| Avoidable share of losses | {s['avoidable_loss_pct']:.0f}% |",
        "",
        "Money lost to rule breaks counts **only** trades that broke a rule. "
        "A loss on a clean setup is the cost of having an edge and is excluded "
        "deliberately — this isolates what indiscipline alone cost.",
        "",
        ("## How each strategy did today" if review.period == "day"
         else "## Which strategy is working"), "",
        "| Strategy | Trades | Win rate | P&L | Avg discipline |",
        "|---|---|---|---|---|",
    ]
    for name, v in sorted(s["by_strategy"].items(),
                          key=lambda kv: -kv[1]["total_pnl"]):
        out.append(f"| {name} | {v['count']} | {v['win_rate']:.0f}% | "
                   f"{currency}{v['total_pnl']:+,.2f} | {v['avg_score']:.1f}/10 |")

    out += ["", "## Every trade", "",
            "| Symbol | Strategy | Entry | Exit | Why it closed | P&L | Verdict |",
            "|---|---|---|---|---|---|---|"]
    for t in review.trades:
        out.append(f"| {t['symbol']} {t['direction']} | {t['strategy']} | "
                   f"{t['entry_price']:.2f} | {t['exit_price']:.2f} | "
                   f"{t['exit_reason']} | {currency}{t['pnl']:+,.2f} | "
                   f"{t['verdict']} ({t['execution_score']}/10) |")
    out.append("")

    coach = review.coach
    if coach:
        out += [f"## The coach's read ({coach.generated_by})", "",
                f"**{coach.headline}**", ""]
        if coach.what_worked:
            out += ["### What worked", ""] + [f"- {x}" for x in coach.what_worked] + [""]
        if coach.what_cost_money:
            out += ["### What cost money", ""] + \
                   [f"- {x}" for x in coach.what_cost_money] + [""]
        if coach.rule_changes:
            out += ["### Proposed changes", "",
                    "Suggestions only — **nothing here has been applied**. The "
                    "risk desk stays deterministic: no model can change a stop, "
                    "a size or a strategy's window.", "",
                    "| Rule | Change | Evidence |", "|---|---|---|"]
            for c in coach.rule_changes:
                out.append(f"| {c.rule} | {c.change} | {c.why} |")
            out.append("")
        out += ["### Focus next week", "", coach.focus_next_week, ""]
    return "\n".join(out)


def save(review: Review, cfg) -> dict[str, str]:
    folder = DAILY_DIR if review.period == "day" else WEEKLY_DIR
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / f"{review.label}.md"
    js = folder / f"{review.label}.json"
    md.write_text(to_markdown(review, cfg), encoding="utf-8")
    js.write_text(review.model_dump_json(indent=2), encoding="utf-8")
    log.info("%s review written: %s (%d trades, %s)", review.period, md.name,
             len(review.trades), "final" if review.complete else "provisional")
    return {"markdown": str(md), "json": str(js), "label": review.label,
            "complete": review.complete, "period": review.period}
