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


def test_every_buy_carries_a_trade_card_and_the_sell_its_result_in_r(cfg, monkeypatch):
    """For a contest review: the trigger candles, the levels and figures, the
    strategy's written rules, the stop/target in R, the sizing arithmetic,
    and how it ended in R."""
    from panaoptions import audit, clock
    from panaoptions.app import OptionsDesk

    now = {"t": datetime(2026, 9, 23, 10, 20, tzinfo=ET)}
    monkeypatch.setattr(clock, "now", lambda tz: now["t"])
    cfg.data["contracts"]["max_contract_price"] = 2.0
    cfg.data["universe"]["symbols"] = ["SPY"]
    cfg.data["account"]["starting_capital"] = 5000.0
    cfg.data["risk"]["max_risk_per_trade_pct"] = 2.0
    feed = FakeFeed()
    desk = OptionsDesk(cfg=cfg, feed=feed)
    asyncio.run(desk.cycle())
    [trade] = desk.ledger.open_trades.values()
    day = now["t"].date()
    [buy] = [e for e in audit.entries(day) if e["event"] == "BUY"]
    card = buy["card"]

    assert len(card["candles"]) == 6
    last = card["candles"][-1]
    assert {"time", "open", "high", "low", "close", "volume", "colour"} <= set(last)
    assert set(card["levels"]) >= {"previous_high", "previous_low", "opening_range_high"}
    assert card["levels"]["opening_range_high"]          # the ORB trade it took
    assert card["indicators"]["vwap"] and card["indicators"]["atr"]
    assert card["rules"]["buy"] and card["rules"]["wrong"], "the strategy's written rules"
    p = card["plan"]
    assert p["stop"] == round(buy["underlying_stop"], 2)
    assert p["reward_risk"] == round(p["reward_points"] / p["risk_points"], 2)
    z = card["sizing"]
    assert z["capital"] == 5000.0 and z["quantity"] == trade.quantity
    assert z["risk_cap"] == 100.0                         # 2% of $5,000
    assert z["planned_risk"] <= z["risk_cap"]
    assert z["planned_risk"] == round(z["loss_per_contract_at_stop"] * z["quantity"], 2)

    feed.contract_mid = 1.20
    now["t"] = datetime(2026, 9, 23, 16, 0, tzinfo=ET)
    asyncio.run(desk.cycle())
    text = audit.day_markdown(day)
    assert "Trade card" in text and "**trigger**" in text
    assert "Strategy rules —" in text and "**Sizing**" in text
    assert "planned risk" in text
    sell = audit.by_trade(audit.entries(day))[trade.id]["sell"]
    want = sell["pnl"] / z["planned_risk"]
    assert f"Result: {want:+.2f}R" in text


def test_a_card_that_cannot_be_built_never_stops_the_buy_record(cfg, monkeypatch):
    from panaoptions import audit, clock
    from panaoptions.app import OptionsDesk

    def boom(*a, **k):
        raise RuntimeError("bad data")

    now = datetime(2026, 9, 23, 10, 20, tzinfo=ET)
    monkeypatch.setattr(clock, "now", lambda tz: now)
    monkeypatch.setattr(audit, "trade_card", boom)
    cfg.data["contracts"]["max_contract_price"] = 2.0
    cfg.data["universe"]["symbols"] = ["SPY"]
    desk = OptionsDesk(cfg=cfg, feed=FakeFeed())
    asyncio.run(desk.cycle())
    [buy] = [e for e in audit.entries(now.date()) if e["event"] == "BUY"]
    assert buy["card"] is None and buy["confirmations"]
    assert "**BUY**" in audit.day_markdown(now.date())
