"""The weekly review, day by day and market by market.

What is promised:
  * Opened on a Wednesday, the weekly review shows every buy and sell from
    the audit log for Monday, Tuesday and Wednesday, date by date.
  * US and India are reviewed apart: their own trades, audit and file.
  * The week so far is saved after every session, not only after Friday.
  * On Auto, India's day and the US day share a date — both get written.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core import audit
from app.journal import store, weekly

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "JOURNAL_DIR", tmp_path / "journal")
    return tmp_path / "journal"


def _at(monkeypatch, day, hh=11, mm=0):
    when = datetime(2026, 9, day, hh, mm, tzinfo=IST)
    monkeypatch.setattr(audit.clock, "market_now", lambda tz: when)
    monkeypatch.setattr(weekly.clock, "market_now", lambda tz: when)
    return when


def _buy(cfg, sid, symbol="RELIANCE"):
    return audit._write(cfg, {
        "event": "BUY", "signal_id": sid, "symbol": symbol,
        "tradingsymbol": f"{symbol}-EQ", "quantity": 10, "entry": 100.0,
        "stop_loss": 98.0, "target": 104.0, "notional": 1000.0, "total_risk": 20.0,
        "why": {"headline": "3 analysts agreed on a long",
                "confirmations": ["Candlestick", "Macro"], "vote": "Weighted vote"},
        "counter_argument": "A close below VWAP would invalidate it."})


def _sell(cfg, sid, pnl, symbol="RELIANCE"):
    return audit._write(cfg, {
        "event": "SELL", "signal_id": sid, "symbol": symbol,
        "tradingsymbol": f"{symbol}-EQ", "quantity": 10, "entry": 100.0,
        "exit_price": 100.0 + pnl / 10, "pnl": pnl, "r_multiple": pnl / 20,
        "status": "CLOSED_TARGET" if pnl > 0 else "CLOSED_STOP",
        "exit_detail": "Target reached" if pnl > 0 else "Stop hit",
        "held_minutes": 35.0})


def _week_of_india(cfg, monkeypatch):
    """Mon: buy+sell (win). Tue: buy+sell (loss). Wed: a buy still open.
    Thu: a trade that has not happened yet when we look on Wednesday."""
    monkeypatch.setattr(cfg, "active_market", "IN")
    _at(monkeypatch, 21, 10)
    _buy(cfg, "M1")
    _at(monkeypatch, 21, 11)
    _sell(cfg, "M1", 40.0)
    _at(monkeypatch, 22, 10)
    _buy(cfg, "T1", "TCS")
    _at(monkeypatch, 22, 12)
    _sell(cfg, "T1", -20.0, "TCS")
    _at(monkeypatch, 23, 10)
    _buy(cfg, "W1", "INFY")


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_on_wednesday_the_weekly_review_shows_monday_to_wednesday(
        cfg, journal, monkeypatch):
    _week_of_india(cfg, monkeypatch)
    _at(monkeypatch, 23, 14)                     # Wednesday afternoon
    review = await weekly.build(with_coach=False, cfg=cfg)

    assert review.market == "IN" and not review.complete
    days = review.audit_days
    assert [d["date"] for d in days] == ["2026-09-21", "2026-09-22", "2026-09-23"]
    assert [d["weekday"] for d in days] == ["Mon", "Tue", "Wed"]
    mon, tue, wed = days
    assert (mon["buys"], mon["sells"], mon["pnl"], mon["wins"]) == (1, 1, 40.0, 1)
    assert (tue["pnl"], tue["losses"]) == (-20.0, 1)
    assert (wed["buys"], wed["sells"]) == (1, 0)
    buy = mon["events"][0]
    assert buy["event"] == "BUY" and buy["stop"] == 98.0 and buy["target"] == 104.0
    assert "3 analysts agreed" in buy["reason"] and "Candlestick" in buy["reason"]
    assert mon["events"][1]["reason"] == "Target reached"

    md = weekly.to_markdown(review)
    assert "Audit log — day by day" in md
    for day in ("Mon 2026-09-21", "Tue 2026-09-22", "Wed 2026-09-23"):
        assert day in md
    assert "journal/audit/in/2026-09-22.md" in md
    assert "This week is not finished" in md


@pytest.mark.asyncio
async def test_a_later_day_is_not_shown_before_it_happens(cfg, journal, monkeypatch):
    _week_of_india(cfg, monkeypatch)
    _at(monkeypatch, 24, 10)
    _buy(cfg, "TH1")                              # Thursday
    _at(monkeypatch, 23, 14)                     # …looked at on Wednesday
    review = await weekly.build(with_coach=False, cfg=cfg)
    assert "2026-09-24" not in [d["date"] for d in review.audit_days]


@pytest.mark.asyncio
async def test_us_and_india_are_reviewed_apart(cfg, journal, monkeypatch):
    _week_of_india(cfg, monkeypatch)
    monkeypatch.setattr(cfg, "active_market", "US")
    _at(monkeypatch, 22, 23)
    _buy(cfg, "U1", "AAPL")
    _at(monkeypatch, 23, 14)

    us = await weekly.build(with_coach=False, cfg=cfg)
    assert [e["symbol"] for d in us.audit_days for e in d["events"]] == ["AAPL"]
    monkeypatch.setattr(cfg, "active_market", "IN")
    india = await weekly.build(with_coach=False, cfg=cfg)
    symbols = {e["symbol"] for d in india.audit_days for e in d["events"]}
    assert "AAPL" not in symbols and "RELIANCE" in symbols


def test_journal_rows_are_read_one_market_at_a_time(cfg, journal, monkeypatch, tmp_path):
    from datetime import timedelta

    from app.journal.models import JournalEntry, SetupType
    from app.storage import db

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "j.db", raising=False)
    monkeypatch.setattr(db, "_conn", None, raising=False)
    monkeypatch.setattr(type(cfg), "db_path", property(lambda self: tmp_path / "rt.db"))
    store.init_journal()
    ts = datetime(2026, 9, 22, 10, 0)
    for i, (market, sym) in enumerate((("US", "AAPL"), ("IN", "TCS"))):
        store.save_entry(JournalEntry(
            id=f"J{i}", ts=ts, symbol=sym, market=market, side="BUY",
            setup=SetupType.BREAKOUT, instrument=sym, planned_entry=100,
            planned_stop=98, planned_target=104, planned_quantity=1,
            actual_entry=100, actual_exit=104, actual_quantity=1, entry_ts=ts,
            exit_ts=ts + timedelta(minutes=5)))
    rows = store.entries(since="2026-09-21", until="2026-09-25", market="IN")
    assert [r["symbol"] for r in rows if r["id"] in {"J0", "J1"}] == ["TCS"]


@pytest.mark.asyncio
async def test_each_market_saves_its_own_weekly_file(cfg, journal, monkeypatch):
    _week_of_india(cfg, monkeypatch)
    _at(monkeypatch, 23, 16)
    india = weekly.save(await weekly.build(with_coach=False, cfg=cfg))
    monkeypatch.setattr(cfg, "active_market", "US")
    _buy(cfg, "U1", "AAPL")
    us = weekly.save(await weekly.build(with_coach=False, cfg=cfg))
    assert india["markdown"] != us["markdown"]
    assert india["markdown"].endswith("2026-09-21_to_2026-09-25-in.md")
    assert us["markdown"].endswith("2026-09-21_to_2026-09-25-us.md")
    saved = json.loads(open(india["json"], encoding="utf-8").read())
    assert len(saved["audit_days"]) == 3


# --------------------------------------------------------------------------- #
# The engine keeps it up to date
# --------------------------------------------------------------------------- #
@pytest.fixture
async def engine(cfg):
    from app.brokers.paper import PaperBroker
    from app.scheduler import TradingEngine

    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    return TradingEngine(broker, cfg)


@pytest.mark.asyncio
async def test_the_week_so_far_is_saved_after_each_session(
        engine, cfg, journal, monkeypatch):
    _week_of_india(cfg, monkeypatch)
    _at(monkeypatch, 22, 16)                     # Tuesday, after the close
    await engine._save_week_so_far()
    path = journal / "weekly" / "2026-09-21_to_2026-09-25-in.md"
    tuesday = path.read_text(encoding="utf-8")
    assert "Tue 2026-09-22" in tuesday and "Wed 2026-09-23" not in tuesday

    _at(monkeypatch, 23, 16)                     # Wednesday, after the close
    await engine._save_week_so_far()
    assert "Wed 2026-09-23" in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_on_auto_both_markets_get_their_friday_review(engine, cfg, monkeypatch):
    from datetime import date

    monkeypatch.setattr(weekly, "current_week",
                        lambda *a, **k: (date(2026, 9, 21), date(2026, 9, 25)))
    monkeypatch.setattr(weekly, "is_complete", lambda *a, **k: True)
    written: list[str] = []
    monkeypatch.setattr(weekly, "save",
                        lambda review: written.append(review.market) or {})

    async def _build(*a, **k):
        return weekly.WeekReview(week_start=date(2026, 9, 21), week_end=date(2026, 9, 25),
                                 market=cfg.active_market, trades=[{"id": 1}])

    async def _nothing(*a, **k):
        return None

    monkeypatch.setattr(weekly, "build", _build)
    monkeypatch.setattr(engine, "_friday_feedback", _nothing)

    monkeypatch.setattr(cfg, "active_market", "IN")
    await engine._maybe_write_weekly_review()
    await engine._maybe_write_weekly_review()     # not twice for India
    monkeypatch.setattr(cfg, "active_market", "US")
    await engine._maybe_write_weekly_review()
    assert written == ["IN", "US"]
