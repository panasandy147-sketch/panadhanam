"""The learning loop: grade on process, never on outcome."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from panaoptions.journal import store as jstore
from panaoptions.journal import weekly
from panaoptions.journal.analytics import analyse
from panaoptions.journal.grade import build_card, build_entry, detect, score, verdict
from panaoptions.journal.models import Mistake, Verdict
from panaoptions.models import Direction, ExitReason, Fill, PaperTrade, SetupType

ET = ZoneInfo("America/New_York")


def _trade(*, pnl=50.0, strategy=SetupType.ORB_VWAP, reason=ExitReason.TARGET_2,
           opened="10:00", quantity=1, entry=0.80, stop=0.44, day=23):
    hour, minute = (int(x) for x in opened.split(":"))
    opened_at = datetime(2026, 9, day, hour, minute, tzinfo=ET)
    exit_price = entry + pnl / (quantity * 100)
    return PaperTrade(
        id=f"PT-{day}-{hour}{minute}-{int(pnl * 100)}-{quantity}",
        signal_id="S1", symbol="SPY",
        direction=Direction.LONG, contract_label="SPY 2026-10-02 110C",
        opened_at=opened_at, quantity=quantity, entry_price=entry,
        stop_price=stop, target_1=entry * 1.4, target_2=entry * 1.7,
        underlying_support=100.0, strategy=strategy,
        invalidation_note="a 5m close back inside the opening range",
        remaining=0, realised_pnl=pnl, exit_reason=reason,
        closed_at=opened_at + timedelta(minutes=40),
        fills=[Fill(ts=opened_at, quantity=quantity, price=entry, reason="ENTRY"),
               Fill(ts=opened_at + timedelta(minutes=40), quantity=-quantity,
                    price=exit_price, reason=reason.value)])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from panaoptions.ledger import store as ledger_store
    monkeypatch.setattr(ledger_store, "_conn", None)
    monkeypatch.setattr(ledger_store, "db_path", lambda: tmp_path / "j.db")
    monkeypatch.setattr(jstore, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(weekly, "WEEKLY_DIR", tmp_path / "journal" / "weekly")
    return tmp_path


# --------------------------------------------------------------------------- #
def test_a_clean_winner_scores_ten(cfg):
    entry = build_entry(_trade(pnl=50.0), cfg)
    assert entry.mistakes == []
    assert entry.verdict is Verdict.GOOD_WIN
    assert entry.execution_score == 10


def test_a_clean_loser_also_scores_ten(cfg):
    # Honouring the invalidation is the behaviour you want, and the P&L has no
    # vote in the discipline score. -36 on a 0.80 entry exits at 0.44, which
    # is the stop exactly.
    entry = build_entry(_trade(pnl=-36.0, reason=ExitReason.STOP), cfg)
    assert entry.verdict is Verdict.GOOD_LOSS
    assert entry.execution_score == 10


def test_a_winner_that_broke_a_rule_is_graded_as_a_failure(cfg):
    # BAD_WIN is the dangerous one: the money reinforces the habit.
    entry = build_entry(_trade(pnl=60.0, opened="14:00"), cfg)
    assert Mistake.OUTSIDE_WINDOW in entry.mistakes
    assert entry.verdict is Verdict.BAD_WIN
    assert entry.execution_score < 10


def test_an_untagged_trade_is_flagged(cfg):
    entry = build_entry(_trade(strategy=SetupType.OTHER), cfg)
    assert Mistake.NO_STRATEGY_TAG in entry.mistakes


def test_holding_to_the_bell_is_recorded(cfg):
    entry = build_entry(_trade(reason=ExitReason.DAY_END), cfg)
    assert Mistake.HELD_TO_THE_BELL in entry.mistakes


def test_an_oversized_position_is_caught(cfg):
    # 20% of $500 is $100; three contracts at $0.80 is $240.
    entry = build_entry(_trade(quantity=3), cfg)
    assert Mistake.OVERSIZED in entry.mistakes


def test_a_stop_filled_far_below_its_level_is_caught(cfg):
    trade = _trade(pnl=-60.0, reason=ExitReason.STOP, stop=0.60)
    trade.fills[-1].price = 0.30           # filled well through the stop
    assert Mistake.STOP_NOT_HONOURED in detect(trade, cfg)


def test_ordinary_slippage_on_a_cheap_stop_is_not_a_rule_break(cfg):
    # $0.02 of slippage is 4.5% of a $0.44 stop. A percentage tolerance would
    # call every honest stop fill on a cheap contract a violation.
    trade = _trade(pnl=-38.0, reason=ExitReason.STOP, stop=0.44)
    trade.fills[-1].price = 0.42
    assert Mistake.STOP_NOT_HONOURED not in detect(trade, cfg)


@pytest.mark.parametrize("pnl,mistakes,expected", [
    (10.0, [], Verdict.GOOD_WIN),
    (-10.0, [], Verdict.GOOD_LOSS),
    (10.0, [Mistake.CHASED], Verdict.BAD_WIN),
    (-10.0, [Mistake.CHASED], Verdict.BAD_LOSS),
])
def test_the_verdict_grid(pnl, mistakes, expected):
    assert verdict(pnl, mistakes) is expected


def test_the_score_never_goes_below_one():
    assert score(list(Mistake)) == 1


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_card_reads_as_a_post_mortem(cfg):
    entry = build_entry(_trade(pnl=60.0, opened="14:00"), cfg)
    card = await build_card(entry, cfg)
    markdown = card.to_markdown()

    assert "BAD_WIN" in markdown
    assert "while breaking a rule" in markdown
    assert card.generated_by == "rules"


@pytest.mark.asyncio
async def test_a_clean_card_says_what_to_repeat(cfg):
    card = await build_card(build_entry(_trade(), cfg), cfg)
    assert "Repeat this" in card.what_to_repeat
    assert card.core_violation == "None."


@pytest.mark.asyncio
async def test_a_dead_model_does_not_stop_the_card(cfg, monkeypatch):
    cfg.data["journal"]["use_llm"] = True

    async def _explode(**kwargs):
        raise RuntimeError("ollama is not running")

    monkeypatch.setattr("panaoptions.ml.llm.structured_complete", _explode)
    card = await build_card(build_entry(_trade(), cfg), cfg)

    assert card.generated_by == "rules", "the rules version must survive"
    assert card.what_happened


# --------------------------------------------------------------------------- #
def test_the_mistake_cost_excludes_clean_losses(cfg):
    rows = []
    for pnl, opened in ((-40.0, "10:00"), (-30.0, "14:00"), (50.0, "10:00")):
        entry = build_entry(_trade(pnl=pnl, opened=opened), cfg)
        jstore.save_entry(entry)
        rows.append(entry)

    stats = analyse(jstore.entries())
    assert stats["total"] == 3
    assert stats["mistake_cost"] == 30.0, \
        "only the rule-breaking loss counts; a clean loss is the cost of an edge"
    assert stats["clean_loss"] == 40.0


def test_analytics_breaks_results_down_by_strategy(cfg):
    for strategy, pnl in ((SetupType.ORB_VWAP, 50.0),
                          (SetupType.ORB_VWAP, -20.0),
                          (SetupType.LIQUIDITY_SWEEP, -30.0)):
        jstore.save_entry(build_entry(_trade(pnl=pnl, strategy=strategy,
                                             day=21 + int(pnl) % 3), cfg))

    by_strategy = analyse(jstore.entries())["by_strategy"]
    assert by_strategy["ORB + VWAP"]["count"] == 2
    assert by_strategy["Liquidity Sweep Reversal"]["total_pnl"] == -30.0


def test_reading_an_empty_journal_is_not_an_error():
    assert jstore.entries() == []
    assert analyse([])["total"] == 0


# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("day,monday", [
    (date(2026, 9, 21), date(2026, 9, 21)),
    (date(2026, 9, 24), date(2026, 9, 21)),
    (date(2026, 9, 26), date(2026, 9, 21)),   # Saturday -> the week just ended
])
def test_the_week_runs_monday_to_friday(day, monday):
    start, end = weekly.week_bounds(day)
    assert start == monday
    assert end == monday + timedelta(days=4)


@pytest.mark.asyncio
async def test_the_weekly_review_compares_the_strategies(cfg):
    for strategy, pnl, day in ((SetupType.ORB_VWAP, 50.0, 21),
                               (SetupType.ORB_VWAP, 40.0, 22),
                               (SetupType.LIQUIDITY_SWEEP, -30.0, 23)):
        jstore.save_entry(build_entry(_trade(pnl=pnl, strategy=strategy, day=day), cfg))

    review = await weekly.build(cfg, date(2026, 9, 21), date(2026, 9, 25))
    assert review.stats["total"] == 3
    assert review.coach is not None
    assert any("ORB" in x for x in review.coach.what_worked)
    assert any("Liquidity" in x for x in review.coach.what_cost_money)

    markdown = weekly.to_markdown(review, cfg)
    assert "Which strategy is working" in markdown
    assert "ORB + VWAP" in markdown


@pytest.mark.asyncio
async def test_a_small_sample_is_called_small(cfg):
    jstore.save_entry(build_entry(_trade(), cfg))
    review = await weekly.build(cfg, date(2026, 9, 21), date(2026, 9, 25))
    assert "too few" in review.coach.headline.lower()


@pytest.mark.asyncio
async def test_an_empty_week_says_so_rather_than_dividing_by_zero(cfg):
    review = await weekly.build(cfg, date(2026, 9, 7), date(2026, 9, 11))
    assert review.trades == []
    assert "no trades" in review.coach.headline.lower()


@pytest.mark.asyncio
async def test_saving_writes_both_formats(cfg, isolated):
    jstore.save_entry(build_entry(_trade(), cfg))
    review = await weekly.build(cfg, date(2026, 9, 21), date(2026, 9, 25))
    out = weekly.save(review, cfg)

    from pathlib import Path
    assert Path(out["markdown"]).exists()
    assert Path(out["json"]).exists()


# --------------------------------------------------------------------------- #
# The daily review. Same machinery, a much firmer caveat.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_daily_review_covers_one_session(cfg):
    jstore.save_entry(build_entry(_trade(pnl=50.0, day=23), cfg))
    jstore.save_entry(build_entry(_trade(pnl=-20.0, day=24), cfg))

    review = await weekly.build_daily(cfg, date(2026, 9, 23))
    assert review.period == "day"
    assert review.stats["total"] == 1, "the other day's trade is not this day's"
    assert review.label == "2026-09-23"
    assert review.title == "Daily Review — 2026-09-23"


@pytest.mark.asyncio
async def test_a_daily_review_refuses_to_judge_a_strategy(cfg):
    # One session says how you executed. Which strategy works needs the week,
    # and letting a good day read as proof is how a fluke becomes a rule.
    for pnl in (50.0, 40.0, 30.0):
        jstore.save_entry(build_entry(
            _trade(pnl=pnl, day=23, quantity=1, entry=0.80 + pnl / 1000), cfg))

    review = await weekly.build_daily(cfg, date(2026, 9, 23))
    assert "weekly review's job" in review.coach.headline
    assert "Tomorrow" in review.coach.focus_next_week


@pytest.mark.asyncio
async def test_an_empty_day_says_what_to_look_at(cfg):
    review = await weekly.build_daily(cfg, date(2026, 9, 23))
    assert review.trades == []
    assert "no trades were taken today" in review.coach.headline.lower()
    assert "rejection tally" in review.coach.focus_next_week


@pytest.mark.asyncio
async def test_the_daily_markdown_is_titled_as_a_day(cfg):
    jstore.save_entry(build_entry(_trade(day=23), cfg))
    review = await weekly.build_daily(cfg, date(2026, 9, 23))
    markdown = weekly.to_markdown(review, cfg)

    assert markdown.startswith("# Daily Review — 2026-09-23")
    assert "How each strategy did today" in markdown


@pytest.mark.asyncio
async def test_daily_and_weekly_are_saved_to_different_folders(cfg, isolated):
    from pathlib import Path

    jstore.save_entry(build_entry(_trade(day=23), cfg))

    daily = weekly.save(await weekly.build_daily(cfg, date(2026, 9, 23)), cfg)
    week = weekly.save(
        await weekly.build(cfg, date(2026, 9, 21), date(2026, 9, 25)), cfg)

    assert Path(daily["markdown"]).parent.name == "daily"
    assert Path(week["markdown"]).parent.name == "weekly"
    assert daily["period"] == "day" and week["period"] == "week"


def test_a_session_is_only_over_after_the_forced_close(cfg, monkeypatch):
    from panaoptions import clock

    monkeypatch.setattr(clock, "now",
                        lambda tz: datetime(2026, 9, 23, 12, 0, tzinfo=ET))
    assert weekly.session_over(cfg, date(2026, 9, 23)) is False

    monkeypatch.setattr(clock, "now",
                        lambda tz: datetime(2026, 9, 23, 16, 0, tzinfo=ET))
    assert weekly.session_over(cfg, date(2026, 9, 23)) is True

    # A past day is over whatever the clock says now.
    assert weekly.session_over(cfg, date(2026, 9, 22)) is True
