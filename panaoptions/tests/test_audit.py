"""The audit log records every buy and sell with the reasons behind it."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.test_app import FakeFeed

ET = ZoneInfo("America/New_York")


def test_a_buy_and_its_sell_are_both_in_the_audit_with_reasons(cfg, monkeypatch):
    from panaoptions import audit, clock
    from panaoptions.app import OptionsDesk

    now = {"t": datetime(2026, 9, 23, 10, 20, tzinfo=ET)}
    monkeypatch.setattr(clock, "now", lambda tz: now["t"])
    cfg.data["contracts"]["max_contract_price"] = 2.0
    cfg.data["universe"]["symbols"] = ["SPY"]
    feed = FakeFeed()
    desk = OptionsDesk(cfg=cfg, feed=feed)
    asyncio.run(desk.cycle())
    [trade] = desk.ledger.open_trades.values()

    day = now["t"].date()
    [buy] = [e for e in audit.entries(day) if e["event"] == "BUY"]
    assert buy["trade_id"] == trade.id and buy["contract"] == trade.contract_label
    assert buy["strategy"] and buy["confirmations"] and buy["invalidation_note"]
    assert buy["signal"]["invalidation_level"] == round(buy["underlying_stop"], 4)
    assert buy["committee"]["votes"], "the committee's votes are recorded"
    assert buy["committee"]["gate"]["approved"] is True

    # One contract trails rather than scaling out; the square-off closes it.
    feed.contract_mid = 1.20
    now["t"] = datetime(2026, 9, 23, 16, 0, tzinfo=ET)
    asyncio.run(desk.cycle())
    halves = audit.by_trade(audit.entries(day))[trade.id]
    sell = halves["sell"]
    assert sell["pnl"] > 0 and sell["exits"]
    assert sell["exit_reason"] == "DAY_END"
    assert sell["held_minutes"] == 340.0

    text = audit.day_markdown(day)
    assert trade.id in text and "**BUY**" in text and "**SELL**" in text
    assert "Wrong if" in text
