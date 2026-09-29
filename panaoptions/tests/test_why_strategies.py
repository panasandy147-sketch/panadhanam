"""`run.py --why` by strategy: how often each was checked, how often it fired,
and why not — per market, so a no-trade day names the rule behind it."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from panaoptions import clock, why
from panaoptions.ledger import store
from panaoptions.models import Direction, Setup, SetupType

ET = ZoneInfo("America/New_York")
DAY = "2026-09-23"


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "w.db")
    store.init()


def _attempt(strategy, fired=False, blocker=""):
    s = Setup(symbol="SPY", ts=datetime(2026, 9, 23, 10, 0, tzinfo=ET), strategy=strategy,
              direction=Direction.LONG if fired else Direction.NONE)
    if blocker:
        s.blockers.append(blocker)
    return s


def test_checks_are_tallied_with_the_numbers_taken_out(db):
    store.tally_checks(DAY, [_attempt(SetupType.ORB_VWAP, blocker="volume 1.2x below 1.5x"),
                             _attempt(SetupType.PD_LIQUIDITY_SWEEP, fired=True)])
    store.tally_checks(DAY, [_attempt(SetupType.ORB_VWAP, blocker="volume 0.8x below 1.5x")])
    rows = {(r["strategy"], r["outcome"], r["reason"]): r["n"] for r in store.checks(DAY)}
    assert rows[("ORB + VWAP", "not_fired", "volume #x below #x")] == 2
    assert rows[("PD Liquidity Sweep", "fired", "")] == 1


def test_the_table_names_every_strategy_and_why_it_did_not_fire(db, cfg):
    store.tally_checks(DAY, [_attempt(SetupType.ORB_VWAP, blocker="no 5m close beyond the range")] * 3
                       + [_attempt(SetupType.ORB_VWAP, fired=True)])
    table = {t["strategy"]: t for t in why.strategy_table(cfg, DAY)}
    orb = table["ORB + VWAP"]
    assert (orb["checked"], orb["fired"]) == (4, 1)
    assert orb["not_fired"][0] == {"reason": "no #m close beyond the range", "count": 3}
    assert table["Value Area Rejection"]["note"] == "switched off"
    assert "never asked yet" in table["VWAP / 9-EMA Pullback"]["note"]
    text = "\n".join(why._strategy_lines(list(table.values())))
    assert "checked     4   fired    1" in text and "not fired    3x" in text


@pytest.mark.asyncio
async def test_the_desk_records_its_checks_as_it_hunts(monkeypatch, cfg, tmp_path):
    from panaoptions.app import OptionsDesk
    from tests.test_app import FakeFeed, _at
    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "d.db")
    cfg.data["contracts"]["max_contract_price"] = 2.00
    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())
    monkeypatch.setattr(clock, "now", lambda tz: _at(9, 50))
    await desk.cycle()
    rows = store.checks("2026-09-23")
    assert rows and any(r["outcome"] == "fired" for r in rows)
    report = why.report(cfg, 800.0)
    assert any(t["checked"] for t in report["strategies"])
    assert "By strategy" in why.render(report)


def test_before_the_session_it_says_the_desk_has_not_hunted(db, cfg):
    report = why.report(cfg, 800.0)
    assert "has not hunted yet today" in why.render(report)
