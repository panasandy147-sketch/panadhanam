"""The weekend review: the week's trades plus the reasoning behind each one."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.journal import store, weekly
from app.journal.models import JournalEntry, MistakeTag, SetupType
from app.storage import db


def _seed(monday: datetime, specs) -> None:
    store.init_journal()
    db.init_db()
    conn = db.get_conn()
    # The test DB is shared across the session, so start from a clean week —
    # agent_reports has an autoincrement key and would otherwise accumulate.
    for table in ("agent_reports", "signals", "journal_entries", "mistake_cards"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    for i, (sym, side, entry, stop, target, exit_price, mistakes, votes) in enumerate(specs):
        ts = monday + timedelta(days=i)
        sid = f"SIG-{i}"
        conn.execute(
            "INSERT OR REPLACE INTO signals (id, ts, symbol, side, entry, stop_loss,"
            " target, composite_score, confirmations, rationale, counter_argument,"
            " regime, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, ts.isoformat(), sym, side, entry, stop, target, 0.52,
             '["candlestick: bullish (+0.51)"]', "Weighted vote of 2 analysts.",
             "A close back below VWAP would invalidate this.", "TREND", "CLOSED"))
        for agent, score, available in votes:
            conn.execute(
                "INSERT INTO agent_reports (signal_id, cycle_id, ts, agent_id, symbol,"
                " bias, score, confidence, data_available, used_llm, rationale)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, f"C{i}", ts.isoformat(), agent, sym,
                 "BULLISH" if score > 0 else "BEARISH", score, 0.7,
                 1 if available else 0, 0, f"{agent} reasoning"))
        conn.commit()

        store.save_entry(JournalEntry(
            id=f"T{i}", ts=ts, symbol=sym, market="US", side=side,
            setup=SetupType.BREAKOUT, instrument=sym,
            planned_entry=entry, planned_stop=stop, planned_target=target,
            planned_quantity=10, actual_entry=entry, actual_exit=exit_price,
            actual_quantity=10, mistakes=list(mistakes), entry_ts=ts,
            exit_ts=ts + timedelta(minutes=30), signal_id=sid))


@pytest.fixture
def week(tmp_path, monkeypatch, cfg):
    """A seeded Mon-Fri with one clean win, one rule-breaking loss, one short."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "journal.db", raising=False)
    monkeypatch.setattr(store, "JOURNAL_DIR", tmp_path / "journal", raising=False)
    monkeypatch.setattr(db, "_conn", None, raising=False)
    monkeypatch.setattr(type(cfg), "db_path",
                        property(lambda self: tmp_path / "runtime.db"))

    monday = datetime(2026, 9, 14, 10, 0)
    _seed(monday, [
        ("AAPL", "BUY", 230.0, 228.0, 234.0, 234.0, [],
         [("candlestick", 0.51, True), ("macro_flow", 0.46, True),
          ("derivatives", 0.0, False)]),
        ("TSLA", "BUY", 410.0, 405.0, 420.0, 402.0, [MistakeTag.MOVED_STOP],
         [("candlestick", 0.40, True), ("macro_flow", -0.30, True)]),
        ("SPY", "SELL", 570.0, 573.0, 564.0, 564.0, [],
         [("candlestick", -0.55, True), ("macro_flow", 0.10, True)]),
    ])
    return date(2026, 9, 14), date(2026, 9, 18)


# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("day,expected_monday", [
    (date(2026, 9, 14), date(2026, 9, 14)),    # Monday itself
    (date(2026, 9, 17), date(2026, 9, 14)),    # midweek
    (date(2026, 9, 18), date(2026, 9, 14)),    # Friday
    (date(2026, 9, 19), date(2026, 9, 14)),    # Saturday -> the week just ended
    (date(2026, 9, 20), date(2026, 9, 14)),    # Sunday   -> likewise
])
def test_the_week_runs_monday_to_friday(day, expected_monday):
    start, end = weekly.week_bounds(day)
    assert start == expected_monday
    assert end == expected_monday + timedelta(days=4)
    assert end.weekday() == 4


def test_a_weekend_review_covers_the_week_that_just_finished():
    # Reviewing on Saturday must not hand you the week that has not started.
    start, _ = weekly.week_bounds(date(2026, 9, 19))
    assert start < date(2026, 9, 19)


# --------------------------------------------------------------------------- #
def test_the_review_collects_the_week_and_nothing_either_side(week):
    start, end = week
    review = weekly.collect(start, end)

    assert len(review.trades) == 3
    assert [t["symbol"] for t in review.trades] == ["AAPL", "TSLA", "SPY"], \
        "a week reads forwards, not newest-first"
    assert weekly.collect(date(2026, 9, 7), date(2026, 9, 11)).trades == []


def test_every_trade_carries_the_reasoning_that_produced_it(week):
    start, end = week
    trade = weekly.collect(start, end).trades[0]

    assert trade["rationale"], "the CMIO's reasoning is the point of the report"
    assert trade["counter_argument"], \
        "what would have made it wrong was recorded BEFORE the outcome"
    assert trade["composite_score"] == pytest.approx(0.52)
    assert {v["agent"] for v in trade["votes"]} == \
        {"candlestick", "macro_flow", "derivatives"}

    abstained = [v for v in trade["votes"] if not v["data_available"]]
    assert [v["agent"] for v in abstained] == ["derivatives"], \
        "an abstention must be visible, not silently missing"


def test_the_scorecard_counts_only_the_trades_an_analyst_confirmed(week):
    start, end = week
    scores = {a["agent"]: a for a in weekly.collect(start, end).agent_scorecard}

    # candlestick backed all three in the traded direction (-0.55 on a SELL is
    # agreement, not opposition).
    assert scores["candlestick"]["confirmed"] == 3
    # macro_flow only backed AAPL: it opposed TSLA and was too weak on SPY.
    assert scores["macro_flow"]["confirmed"] == 1
    # derivatives abstained, so it is not counted either way.
    assert "derivatives" not in scores


def test_a_losing_trade_that_broke_a_rule_shows_up_in_the_mistake_cost(week):
    start, end = week
    stats = weekly.collect(start, end).stats

    assert stats["total"] == 3
    assert stats["mistake_cost_index"] > 0, \
        "the TSLA loss broke a rule, so it belongs in the index"
    assert stats["clean_pct"] < 100


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_without_an_llm_the_coach_still_reports_the_numbers(week, monkeypatch, cfg):
    monkeypatch.setattr(type(cfg), "llm_enabled", property(lambda self: False))
    start, end = week

    review = await weekly.build(start, end)
    assert review.coach is not None
    assert review.coach.generated_by == "rules"
    assert "3 trades" in review.coach.headline
    assert review.coach.what_cost_money, "a rule-breaking loss must be named"


@pytest.mark.asyncio
async def test_a_small_sample_is_called_small_rather_than_called_a_trend(week):
    start, end = week
    review = await weekly.build(start, end)
    assert "too few" in review.coach.headline.lower()


@pytest.mark.asyncio
async def test_an_empty_week_says_so_instead_of_dividing_by_zero(week):
    review = await weekly.build(date(2026, 9, 7), date(2026, 9, 11))
    assert review.trades == []
    assert review.stats["total"] == 0
    assert "no trades" in review.coach.headline.lower()


@pytest.mark.asyncio
async def test_a_model_that_does_not_answer_falls_back_to_the_numbers(
        week, monkeypatch, cfg):
    monkeypatch.setattr(type(cfg), "llm_enabled", property(lambda self: True))

    async def _no_answer(**kwargs):
        return None

    monkeypatch.setattr("app.core.llm.structured_complete", _no_answer)
    start, end = week

    review = await weekly.build(start, end)
    assert review.coach is not None, "a dead model must not blank the report"
    assert "did not answer" in review.coach.headline


@pytest.mark.asyncio
async def test_a_model_that_raises_does_not_take_the_report_down(
        week, monkeypatch, cfg):
    monkeypatch.setattr(type(cfg), "llm_enabled", property(lambda self: True))

    async def _explode(**kwargs):
        raise RuntimeError("ollama is not running")

    monkeypatch.setattr("app.core.llm.structured_complete", _explode)
    start, end = week

    review = await weekly.build(start, end)
    assert len(review.trades) == 3
    assert review.coach is not None


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_markdown_names_each_analyst_and_its_score(week):
    start, end = week
    md = weekly.to_markdown(await weekly.build(start, end))

    assert "# Weekly Review — 2026-09-14 to 2026-09-18" in md
    assert "Candlestick & Technical" in md
    assert "+0.51" in md
    assert "*abstained*" in md
    assert "Counter-argument recorded at entry" in md
    assert "Mistake Cost Index" in md


@pytest.mark.asyncio
async def test_a_pipe_in_an_analyst_rationale_does_not_break_the_table(week):
    start, end = week
    review = await weekly.build(start, end)
    review.trades[0]["votes"][0]["rationale"] = "RSI 70 | MACD cross\nsecond line"

    md = weekly.to_markdown(review)
    row = next(ln for ln in md.splitlines() if "MACD cross" in ln)
    assert "\\|" in row, "the pipe inside the cell must be escaped"
    assert row.count("|") - row.count("\\|") == 6, \
        "an unescaped pipe would add a phantom column"
    assert "\n" not in row, "a newline inside a cell splits the table"


@pytest.mark.asyncio
async def test_an_unfinished_week_is_labelled_provisional(week, monkeypatch):
    monkeypatch.setattr(weekly, "is_complete", lambda *a, **k: False)
    start, end = week

    md = weekly.to_markdown(await weekly.build(start, end))
    assert "not finished" in md.lower()


def test_a_week_is_complete_only_after_its_last_session_closes(cfg, monkeypatch):
    monkeypatch.setattr(weekly.clock, "market_now",
                        lambda tz: datetime(2026, 9, 17, 12, 0))
    assert weekly.is_complete(date(2026, 9, 18), cfg) is False, "it is Thursday"

    monkeypatch.setattr(weekly.clock, "market_now",
                        lambda tz: datetime(2026, 9, 21, 9, 0))
    assert weekly.is_complete(date(2026, 9, 18), cfg) is True, "the week is past"


@pytest.mark.asyncio
async def test_saving_writes_both_formats_where_git_can_see_them(week, tmp_path):
    start, end = week
    out = weekly.save(await weekly.build(start, end))

    from pathlib import Path
    md, js = Path(out["markdown"]), Path(out["json"])
    assert md.exists() and js.exists()
    assert md.parent.name == "weekly"
    assert "2026-09-14_to_2026-09-18" in out["label"]

    import json
    assert len(json.loads(js.read_text())["trades"]) == 3


# --------------------------------------------------------------------------- #
# The scheduler writes the review once, after Friday closes.
# --------------------------------------------------------------------------- #
@pytest.fixture
async def engine(cfg, week):
    from app.brokers.paper import PaperBroker
    from app.scheduler import TradingEngine

    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    return TradingEngine(broker, cfg)


@pytest.mark.asyncio
async def test_the_review_is_written_once_the_week_has_closed(
        engine, monkeypatch, tmp_path):
    monkeypatch.setattr(weekly, "current_week",
                        lambda *a, **k: (date(2026, 9, 14), date(2026, 9, 18)))
    monkeypatch.setattr(weekly, "is_complete", lambda *a, **k: True)

    written: list[str] = []
    monkeypatch.setattr(weekly, "save",
                        lambda review: written.append(review.label) or {})

    await engine._maybe_write_weekly_review()
    assert written == ["2026-09-14_to_2026-09-18"]

    # Running again the same weekend must not rewrite it every two minutes.
    await engine._maybe_write_weekly_review()
    assert len(written) == 1


@pytest.mark.asyncio
async def test_nothing_is_written_mid_week(engine, monkeypatch):
    monkeypatch.setattr(weekly, "current_week",
                        lambda *a, **k: (date(2026, 9, 14), date(2026, 9, 18)))
    monkeypatch.setattr(weekly, "is_complete", lambda *a, **k: False)

    written: list[str] = []
    monkeypatch.setattr(weekly, "save", lambda r: written.append(r.label) or {})

    await engine._maybe_write_weekly_review()
    assert written == [], "a Wednesday snapshot is not the week's verdict"


@pytest.mark.asyncio
async def test_a_failure_does_not_retry_all_weekend(engine, monkeypatch):
    monkeypatch.setattr(weekly, "current_week",
                        lambda *a, **k: (date(2026, 9, 14), date(2026, 9, 18)))
    monkeypatch.setattr(weekly, "is_complete", lambda *a, **k: True)

    calls: list[int] = []

    def _explode(review):
        calls.append(1)
        raise OSError("disk full")

    monkeypatch.setattr(weekly, "save", _explode)

    await engine._maybe_write_weekly_review()
    await engine._maybe_write_weekly_review()
    assert len(calls) == 1, "a broken write must not hammer the loop"
