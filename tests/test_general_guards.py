"""The guards on GENERAL setups (no named strategy) from the US week of
28 Sept 2026: every trade a candlestick-led short, many at RSI 8-25, and
late entries squared off before they could work. Each guard is a setting,
off by default and switched per market."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.agents.risk import RiskManager
from app.core.models import AgentReport, Bias

ET = ZoneInfo("America/New_York")


def _r(agent, score, available=True):
    return AgentReport(agent_id=agent, symbol="AAPL", score=score, confidence=0.8,
                       data_available=available)


def _ind(rsi):
    return {"primary": {"rsi": rsi}}


def test_everything_is_off_by_default(cfg):
    risk = RiskManager(cfg)
    assert risk.general_checks(Bias.BEARISH, [_r("candlestick", -0.6)], _ind(9)) == []


def test_a_general_setup_needs_a_second_analyst(cfg):
    cfg.settings["consensus"]["general_min_analysts"] = 2
    risk = RiskManager(cfg)
    alone = risk.general_checks(Bias.BEARISH, [_r("candlestick", -0.58),
                                               _r("news_sentiment", +0.4)], _ind(40))
    assert alone and "needs 2 analysts" in alone[0] and "1 (candlestick)" in alone[0]
    # An abstaining analyst is not a vote; one at -0.30 the same way is.
    assert risk.general_checks(Bias.BEARISH, [_r("candlestick", -0.58),
                                              _r("fundamental", -0.5, False)], _ind(40))
    assert risk.general_checks(Bias.BEARISH, [_r("candlestick", -0.58),
                                              _r("macro_flow", -0.30)], _ind(40)) == []


def test_no_short_into_an_oversold_move_and_no_long_into_an_overbought_one(cfg):
    cfg.settings["risk"]["rsi_guard"] = {"enabled": True, "short_min": 25, "long_max": 75}
    risk = RiskManager(cfg)
    assert "below 25" in risk.general_checks(Bias.BEARISH, [], _ind(9))[0]
    assert risk.general_checks(Bias.BEARISH, [], _ind(32)) == []
    assert "above 75" in risk.general_checks(Bias.BULLISH, [], _ind(81))[0]
    assert risk.general_checks(Bias.BULLISH, [], _ind(9)) == []


def test_general_entries_stop_at_general_to(cfg, monkeypatch):
    from app.core import clock
    cfg.switch_market("US")
    try:
        cfg.settings["screener"]["windows"]["general_to"] = "14:00"
        risk = RiskManager(cfg)
        two = [_r("candlestick", -0.5), _r("macro_flow", -0.3)]
        monkeypatch.setattr(clock, "market_now",
                            lambda tz: datetime(2026, 10, 5, 13, 55, tzinfo=ET))
        assert risk.general_checks(Bias.BEARISH, two, _ind(40)) == []
        monkeypatch.setattr(clock, "market_now",
                            lambda tz: datetime(2026, 10, 5, 14, 0, tzinfo=ET))
        assert "Past 14:00" in risk.general_checks(Bias.BEARISH, two, _ind(40))[0]
    finally:
        cfg.switch_market("IN")


def test_a_market_file_can_set_the_vote_gate(cfg):
    from app.core.markets import load_profiles
    us = load_profiles()["US"]
    us.data = {**us.data, "consensus": {"general_min_analysts": 2}}
    out = us.apply_to({"consensus": {"general_min_analysts": 0, "general_agree_min": 0.25}})
    assert out["consensus"] == {"general_min_analysts": 2, "general_agree_min": 0.25}


def test_the_us_desk_ships_two_and_india_none(cfg):
    """The 14:00 general cutoff is off since 5 Oct 2026 (the user's call:
    every entry runs to 15 min before the close)."""
    cfg.switch_market("US")
    try:
        assert cfg.get("consensus.general_min_analysts") == 2
        assert cfg.get("risk.rsi_guard")["enabled"] is True
        assert not cfg.get("screener.windows.general_to")
    finally:
        cfg.switch_market("IN")
    assert cfg.get("consensus.general_min_analysts") == 0
    assert cfg.get("risk.rsi_guard")["enabled"] is False
    assert not cfg.get("screener.windows.general_to")


def test_two_losing_trades_end_the_us_day(cfg):
    """risk.max_losses_per_day: 2 on the US (5 Oct 2026, the user's test 6)."""
    cfg.switch_market("US")
    try:
        risk = RiskManager(cfg)
        assert cfg.get("risk.max_losses_per_day") == 2
        risk.state.losses_today = 1
        assert not any("losing trades today" in r for r in risk.desk_checks())
        risk.state.losses_today = 2
        assert any("2 losing trades today" in r for r in risk.desk_checks())
    finally:
        cfg.switch_market("IN")
    assert not cfg.get("risk.max_losses_per_day")
