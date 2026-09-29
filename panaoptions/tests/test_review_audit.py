"""The weekly review shows the audit log day by day, as far as the week has got.

  * Opened on a Wednesday: Monday, Tuesday and Wednesday, every buy and sell.
  * US and India each have their own (separate folders, separate reviews).
  * The week so far is written after every session, not only after Friday.
  * On Auto, India's day and the US day share a date — both get written.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from panaoptions import audit, clock, markets
from panaoptions.journal import weekly
from tests.test_app import FakeFeed

ET = ZoneInfo("America/New_York")


def _at(monkeypatch, day, hh=10, mm=0, tz=ET):
    when = datetime(2026, 9, day, hh, mm, tzinfo=tz)
    monkeypatch.setattr(clock, "now", lambda _tz: when)
    return when


def _buy(cfg, tid, symbol="SPY", cost=80.0):
    audit._write(cfg, {"event": "BUY", "trade_id": tid, "symbol": symbol,
                       "contract": f"{symbol} 2026-10-02 110C", "strategy": "orb_vwap",
                       "quantity": 1, "entry": 0.80, "cost": cost,
                       "underlying_stop": 99.5, "disaster_stop": 0.44, "target_1": 1.2,
                       "confirmations": ["broke the opening range on 2x volume"],
                       "invalidation_note": "back inside the range",
                       "currency": cfg.currency})


def _sell(cfg, tid, pnl, symbol="SPY"):
    audit._write(cfg, {"event": "SELL", "trade_id": tid, "symbol": symbol,
                       "contract": f"{symbol} 2026-10-02 110C", "strategy": "orb_vwap",
                       "entry": 0.80, "exits": [{"quantity": 1, "price": 0.80 + pnl / 100,
                                                 "reason": "TARGET"}],
                       "exit_reason": "TARGET_1" if pnl > 0 else "STOP",
                       "pnl": pnl, "held_minutes": 42.0})


def _week(cfg, monkeypatch):
    _at(monkeypatch, 21, 10)
    _buy(cfg, "M1")
    _at(monkeypatch, 21, 11)
    _sell(cfg, "M1", 40.0)
    _at(monkeypatch, 22, 10)
    _buy(cfg, "T1", "QQQ")
    _at(monkeypatch, 22, 12)
    _sell(cfg, "T1", -25.0, "QQQ")
    _at(monkeypatch, 23, 10)
    _buy(cfg, "W1", "IWM")


# --------------------------------------------------------------------------- #
async def test_on_wednesday_the_weekly_review_shows_monday_to_wednesday(cfg, monkeypatch):
    _week(cfg, monkeypatch)
    _at(monkeypatch, 24, 10)
    _buy(cfg, "TH1")                                  # Thursday…
    _at(monkeypatch, 23, 14)                          # …looked at on Wednesday
    review = await weekly.build(cfg, with_coach=False)

    assert not review.complete
    days = review.audit_days
    assert [d["date"] for d in days] == ["2026-09-21", "2026-09-22", "2026-09-23"]
    mon, tue, wed = days
    assert (mon["weekday"], mon["buys"], mon["sells"], mon["pnl"]) == ("Mon", 1, 1, 40.0)
    assert (tue["pnl"], tue["losses"]) == (-25.0, 1)
    assert (wed["buys"], wed["sells"]) == (1, 0)
    buy, sell = mon["events"]
    assert buy["cost"] == 80.0 and "opening range" in buy["reason"]
    assert sell["price"] == 1.2 and sell["reason"] == "TARGET_1"

    md = weekly.to_markdown(review, cfg)
    assert "Audit log — day by day" in md
    for day in ("Mon 2026-09-21", "Tue 2026-09-22", "Wed 2026-09-23"):
        assert day in md
    assert "2026-09-24" not in md


async def test_the_audit_shows_even_before_any_trade_is_graded(cfg, monkeypatch):
    _at(monkeypatch, 21, 10)
    _buy(cfg, "M1")
    _at(monkeypatch, 21, 14)
    review = await weekly.build(cfg, with_coach=False)
    assert review.stats["total"] == 0
    md = weekly.to_markdown(review, cfg)
    assert "No trades were graded" in md and "Mon 2026-09-21" in md


async def test_the_api_gives_the_dashboard_the_days(cfg, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from panaoptions.app import OptionsDesk
    from panaoptions.ledger import store
    from panaoptions.web import server

    _week(cfg, monkeypatch)
    _at(monkeypatch, 23, 14)
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "w.db")
    monkeypatch.setattr(server, "get_config", lambda: cfg)
    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(desk, "start", _noop)
    with TestClient(server.create_app(desk)) as c:
        body = c.get("/api/weekly?coach=false").json()
    assert [d["date"] for d in body["audit_days"]] == ["2026-09-21", "2026-09-22",
                                                       "2026-09-23"]
    assert body["market"] == "US"


async def test_india_keeps_its_own_audit_and_review(cfg, monkeypatch, tmp_path):
    from panaoptions import config as config_mod

    _week(cfg, monkeypatch)                           # US trades
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    india = config_mod.Config(market="IN")
    markets.activate("IN")
    try:
        IST = ZoneInfo("Asia/Kolkata")
        _at(monkeypatch, 22, 10, tz=IST)
        _buy(india, "N1", "NIFTY", cost=5625.0)
        _at(monkeypatch, 23, 14, tz=IST)
        review = await weekly.build(india, with_coach=False)
        symbols = [e["symbol"] for d in review.audit_days for e in d["events"]]
        assert symbols == ["NIFTY"] and review.market == "IN"
        md = weekly.to_markdown(review, india)
        assert "₹5,625.00" in md
        assert "(₹5625.0)" in audit.day_markdown(datetime(2026, 9, 22).date())
    finally:
        markets.activate("US")
    us = await weekly.build(cfg, with_coach=False)
    assert "NIFTY" not in [e["symbol"] for d in us.audit_days for e in d["events"]]


# --------------------------------------------------------------------------- #
# The desk keeps it up to date
# --------------------------------------------------------------------------- #
@pytest.fixture
def desk(cfg, monkeypatch, tmp_path):
    from panaoptions.app import OptionsDesk
    from panaoptions.ledger import store
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "d.db")
    return OptionsDesk(cfg=cfg, feed=FakeFeed())


def test_the_week_so_far_is_written_after_each_session(desk, cfg, monkeypatch):
    _week(cfg, monkeypatch)
    _at(monkeypatch, 22, 16, 5)                       # Tuesday, after square-off
    asyncio.run(desk._maybe_write_daily_review())
    path = weekly.WEEKLY_DIR / "2026-09-21_to_2026-09-25.md"
    text = path.read_text(encoding="utf-8")
    assert "Tue 2026-09-22" in text and "Wed 2026-09-23" not in text
    assert "not finished" in text                     # provisional, and says so

    _at(monkeypatch, 23, 16, 5)                       # Wednesday, after square-off
    asyncio.run(desk._maybe_write_daily_review())
    assert "Wed 2026-09-23" in path.read_text(encoding="utf-8")
    # And the day's own review has that day's audit.
    daily = (weekly.DAILY_DIR / "2026-09-23.md").read_text(encoding="utf-8")
    assert "Wed 2026-09-23" in daily


def test_on_auto_india_and_the_us_each_get_their_review_for_the_same_date(
        desk, cfg, monkeypatch):
    _at(monkeypatch, 23, 16, 5)
    built: list[str] = []

    async def _daily(c, day=None, with_coach=True):
        built.append(c.market)
        return weekly.Review(week_start=day, week_end=day, period="day")

    async def _week(c, *a, **k):
        return weekly.Review(week_start=datetime(2026, 9, 21).date(),
                             week_end=datetime(2026, 9, 25).date())

    monkeypatch.setattr(weekly, "build_daily", _daily)
    monkeypatch.setattr(weekly, "build", _week)
    cfg.market = "IN"
    asyncio.run(desk._maybe_write_daily_review())
    asyncio.run(desk._maybe_write_daily_review())     # not twice for India
    cfg.market = "US"
    asyncio.run(desk._maybe_write_daily_review())
    assert built == ["IN", "US"]


def test_a_real_trade_reaches_the_weekly_audit(cfg, monkeypatch):
    """End to end: the desk buys, squares off, and the week shows both."""
    from panaoptions.app import OptionsDesk

    now = {"t": datetime(2026, 9, 23, 10, 20, tzinfo=ET)}
    monkeypatch.setattr(clock, "now", lambda tz: now["t"])
    cfg.data["contracts"]["max_contract_price"] = 2.0
    cfg.data["universe"]["symbols"] = ["SPY"]
    feed = FakeFeed()
    desk = OptionsDesk(cfg=cfg, feed=feed)
    asyncio.run(desk.cycle())
    [trade] = desk.ledger.open_trades.values()
    feed.contract_mid = 1.20
    now["t"] = datetime(2026, 9, 23, 16, 0, tzinfo=ET)
    asyncio.run(desk.cycle())

    review = asyncio.run(weekly.build(cfg, with_coach=False))
    [wed] = review.audit_days
    assert [e["event"] for e in wed["events"]] == ["BUY", "SELL"]
    assert all(e["trade_id"] == trade.id for e in wed["events"])
    assert wed["pnl"] > 0
