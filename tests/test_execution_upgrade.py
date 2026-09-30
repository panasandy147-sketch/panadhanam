"""The 30 Sept execution upgrade: the 2-analyst quorum (built, off by
default), confirmations that must AGREE, the order_tag on every order, and
the stop floor max(1.5 ATR, 0.75% of the price)."""
from __future__ import annotations

import pytest

from app.agents.cmio import CMIOAgent
from app.core.models import AgentReport, Bias, MarketContext


def _r(agent, score):
    return AgentReport(agent_id=agent, symbol="SPY", score=score, confidence=0.8,
                       bias=Bias.BULLISH if score > 0 else Bias.BEARISH)


def _vote(cfg, reports):
    return CMIOAgent(cfg)._weighted_vote(MarketContext(symbol="SPY", cycle_id="t"), reports)


@pytest.fixture
def quorum(cfg):
    cfg.settings.setdefault("consensus", {})["quorum"] = {
        "enabled": True, "primary": "candlestick", "primary_min": 0.35,
        "confirmers": ["volume_profile", "derivatives", "macro_flow", "news_sentiment"],
        "confirm_min": 0.25}
    cfg.settings["consensus"].update(min_confirmations=1, trend_filter=False)
    return cfg


# --------------------------------------------------------------------------- #
# 1. The quorum
# --------------------------------------------------------------------------- #
def test_the_quorum_is_off_by_default_and_one_strong_chart_still_trades(cfg):
    assert cfg.get("consensus.quorum.enabled") is False
    cfg.settings["consensus"]["trend_filter"] = False
    assert _vote(cfg, [_r("candlestick", 0.8)])["proceed"]


def test_with_the_quorum_one_analyst_never_trades_alone(quorum):
    alone = _vote(quorum, [_r("candlestick", 0.8)])
    assert not alone["proceed"] and "no second analyst" in alone["rationale"]
    both = _vote(quorum, [_r("candlestick", 0.8), _r("news_sentiment", 0.30)])
    assert both["proceed"]
    # A short needs both at -0.35 / -0.25 or stronger.
    short = _vote(quorum, [_r("candlestick", -0.6), _r("derivatives", -0.40)])
    assert short["proceed"] and short["bias"] == Bias.BEARISH


def test_the_primary_must_be_the_candlestick_at_035(quorum):
    weak = _vote(quorum, [_r("candlestick", 0.30), _r("volume_profile", 0.9),
                          _r("derivatives", 0.8)])
    assert not weak["proceed"] and "candlestick trigger" in weak["rationale"]
    missing = _vote(quorum, [_r("volume_profile", 0.9), _r("derivatives", 0.8)])
    assert not missing["proceed"]


def test_a_confirmer_voting_the_other_way_is_not_a_confirmation(quorum):
    split = _vote(quorum, [_r("candlestick", 0.9), _r("news_sentiment", -0.30)])
    assert not split["proceed"]


def test_confirmations_only_count_analysts_that_agree(cfg):
    d = _vote(cfg, [_r("candlestick", 0.9), _r("derivatives", 0.5),
                    _r("news_sentiment", -0.30)])
    assert d["composite_score"] > 0
    assert [c.split(":")[0] for c in d["confirmations"]] == ["candlestick", "derivatives"]


# --------------------------------------------------------------------------- #
# 2. order_tag
# --------------------------------------------------------------------------- #
def test_every_order_carries_its_tag():
    from app.core.models import Instrument, Side, TradeSignal
    sig = TradeSignal(id="S", instrument=Instrument(symbol="X", tradingsymbol="X"),
                      side=Side.BUY, entry=100, stop_loss=99, target=103)
    assert sig.order_tag == "BASE_ENTRY"
    assert TradeSignal.model_validate({**sig.model_dump(), "order_tag": "PYRAMID_ADD"}
                                      ).order_tag == "PYRAMID_ADD"


# --------------------------------------------------------------------------- #
# 3. The stop floor
# --------------------------------------------------------------------------- #
def test_the_floor_is_the_larger_of_15_atr_and_075_percent(cfg):
    from app.agents.risk_manager import stop_floor, underlying_stop
    cfg.settings["risk"].update(min_stop_atr=1.5, min_stop_pct=0.75)
    # 0.75% of 1,000 = 7.50 beats 1.5 x 2.0 ATR = 3.00.
    assert stop_floor(cfg, 1000.0, 2.0)[0] == pytest.approx(7.5)
    # A volatile name: 1.5 x 8 = 12 beats 7.50.
    assert stop_floor(cfg, 1000.0, 8.0)[0] == pytest.approx(12.0)
    # A structural stop 2 points away is widened to the floor, both ways.
    long = underlying_stop(cfg, spot=1000.0, bullish=True, atr=2.0, named_level=998.5)
    assert long.level == pytest.approx(992.5) and "0.75% of the price" in long.note
    short = underlying_stop(cfg, spot=1000.0, bullish=False, atr=2.0, named_level=1001.5)
    assert short.level == pytest.approx(1007.5)
    cfg.settings["risk"].update(min_stop_atr=0, min_stop_pct=0)
    assert stop_floor(cfg, 1000.0, 2.0) == (0.0, "")


def test_the_shipped_floor(cfg):
    from app.core.config import get_config
    c = get_config()
    c.reload()                      # the file as shipped, not the fixture's pins
    for market in ("IN", "US"):
        c.switch_market(market)
        assert c.get("risk.min_stop_atr") == 1.5 and c.get("risk.min_stop_pct") == 0.75
        assert c.get("consensus.quorum.enabled") is False
