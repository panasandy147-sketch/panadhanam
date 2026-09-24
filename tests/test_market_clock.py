"""Exits run on the market's clock, never the computer's.

The desk runs on a PC in one timezone and trades a market in another. Every
cutoff here is written in the market's own time.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.learning.outcomes import OutcomeTracker

ET = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")


def _at(monkeypatch, moment: datetime) -> None:
    from app.core import clock

    monkeypatch.setattr(clock, "market_now", lambda tz: moment.astimezone(ZoneInfo(tz)))


@pytest.fixture
def tracker(cfg):
    yield OutcomeTracker(broker=None, cfg=cfg)
    cfg.switch_market("IN")


def test_a_us_trade_is_not_squared_off_before_the_us_square_off(tracker, cfg, monkeypatch):
    """On a UTC machine at 15:29 ET the clock reads 19:29 — past 15:45 — so
    every US trade was closed the moment it opened."""
    cfg.switch_market("US")
    _at(monkeypatch, datetime(2026, 9, 24, 15, 29, tzinfo=ET))
    assert tracker._past_squareoff() is False
    _at(monkeypatch, datetime(2026, 9, 24, 15, 46, tzinfo=ET))
    assert tracker._past_squareoff() is True


def test_an_indian_trade_is_squared_off_on_indian_time(tracker, cfg, monkeypatch):
    """15:15 IST is 05:45 ET. On a PC in New York the local clock never reads
    15:15 during the Indian session, so the square-off never came."""
    cfg.switch_market("IN")
    _at(monkeypatch, datetime(2026, 9, 24, 15, 16, tzinfo=IST))
    assert tracker._past_squareoff() is True
    _at(monkeypatch, datetime(2026, 9, 24, 14, 0, tzinfo=IST))
    assert tracker._past_squareoff() is False


def _row(minutes_ago: float) -> dict:
    opened = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {"ts": opened.isoformat(), "side": "BUY", "entry": 100.0,
            "payload": json.dumps({"setup": "Mean Reversion"})}


def test_the_time_stop_measures_age_in_real_time(tracker, cfg):
    """Rows are UTC. Aged against local time on a New York PC, a trade looked
    four hours younger and the time stop never fired."""
    limit = float(cfg.get("risk.time_stop_minutes"))
    assert tracker._time_stop_hit(_row(limit + 5), price=99.0) is True
    assert tracker._time_stop_hit(_row(limit - 5), price=99.0) is False
    # A trade that is working is left alone.
    assert tracker._time_stop_hit(_row(limit + 5), price=101.0) is False
