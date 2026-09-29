"""Previous-day F&O levels, open-interest build-up, the PDH/PDL confluence
filter and the 1:3 reward-to-risk gate."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd
import pytest

from panaoptions import clock, fno
from panaoptions.engine import confluence, reward
from panaoptions.engine import levels as levels_mod
from panaoptions.models import Direction, Indicators, OptionRight, SessionLevels, Setup, SetupType
from panaoptions.risk.gatekeeper import RiskGatekeeper
from tests.test_debit_spreads import opt
from tests.test_long_puts import DAY, ET, PutFeed, _candles

# Yesterday: low 98.00, high 101.00 late in the day, close 100.40.
YESTERDAY = ([(99.5, 99.8, 98.0, 99.6)] + [(99.6, 100.0, 99.4, 99.8)] * 27
             + [(100.2, 101.0, 100.0, 100.3), (100.3, 100.5, 100.1, 100.4)])
# Today: a wide opening range (98.50-101.00), then a Tweezer Top AT the PDH.
TOP_AT_PDH = [
    (100.0, 100.6, 98.5, 100.4),     # 09:30
    (100.4, 101.0, 100.2, 100.8),    # 09:35
    (100.8, 100.9, 100.3, 100.5),    # 09:40
    (100.5, 100.7, 100.2, 100.6),    # 09:45
    (100.6, 100.8, 100.4, 100.7),    # 09:50
    (100.7, 101.0, 100.6, 100.95),   # 09:55
    (100.95, 101.0, 100.55, 100.85), # 10:00  tweezer top at 101.00 = PDH
    (100.6, 100.65, 100.3, 100.4),   # 10:05  breaks 100.55
]


def _levels():
    bars = _candles(TOP_AT_PDH, before=YESTERDAY)
    return levels_mod.compute(bars, "America/New_York", DAY, "09:30", "16:00")


def chain_with(calls: int, puts: int):
    return [opt(100, 0.45, 1.0, oi=calls), opt(100, 0.45, 1.0, OptionRight.PUT, oi=puts)]


def _setup(direction=Direction.SHORT, extreme=101.0, close=100.4, trigger=100.55,
           target=0.0, strategy=SetupType.CANDLESTICK_AT_LEVEL, pattern="Tweezer Top"):
    return Setup(symbol="SPY", ts=datetime(2026, 9, 23, 10, 10, tzinfo=ET),
                 direction=direction, strategy=strategy, pattern=pattern,
                 indicators=Indicators(close=close, atr=0.5, vwap=100.5, rvol=1.5),
                 underlying_support=extreme, entry_trigger=trigger,
                 underlying_target=target, invalidation_note="back above the high")


# --------------------------------------------------------------------------- #
# 1. Previous-day levels and open interest
# --------------------------------------------------------------------------- #
def test_pdh_pdl_pdc_come_from_the_previous_regular_session():
    lv = _levels()
    assert (lv.previous_high, lv.previous_low, lv.previous_close) == (101.0, 98.0, 100.4)
    kinds = {k.source: k.kind for k in levels_mod.key_levels(
        pd.DataFrame([(100.0, 100.2, 99.9, 100.0)] * 3,
                     columns=["open", "high", "low", "close"]), lv)}
    assert kinds["previous day high"] == "resistance"
    assert kinds["previous day low"] == "support"


@pytest.mark.parametrize("price,oi,expected", [
    (1.2, 500, "Long Buildup"), (-1.2, 500, "Short Buildup"),
    (1.2, -500, "Short Covering"), (-1.2, -500, "Long Unwinding"),
    (None, 500, "unknown"), (1.2, None, "unknown"), (1.2, 0, "neutral"),
])
def test_the_buildup_read(price, oi, expected):
    assert fno.buildup(price, oi) == expected


def test_oi_change_is_day_over_day_then_intraday():
    d1, d2 = datetime(2026, 9, 22, 9, 40, tzinfo=ET), datetime(2026, 9, 23, 9, 40, tzinfo=ET)
    first = fno.record_oi("SPY", d1, chain_with(1000, 800))
    assert first.call_change is None and not first.known       # nothing to compare yet
    later = fno.record_oi("SPY", d1.replace(hour=11), chain_with(1100, 850))
    assert (later.call_change, later.put_change) == (100, 50)   # intraday (NSE-style)
    today = fno.record_oi("SPY", d2, chain_with(1500, 700))
    assert (today.call_change, today.put_change) == (400, -150)  # vs yesterday's latest
    assert today.calls_rising is True and today.puts_rising is False
    assert "vs 2026-09-22" in today.line()


def test_the_day_picture_is_saved_with_its_buildup():
    fno.record_oi("SPY", datetime(2026, 9, 22, 9, 40, tzinfo=ET), chain_with(1000, 1000))
    now = datetime(2026, 9, 23, 9, 40, tzinfo=ET)
    fno.record_oi("SPY", now, chain_with(1400, 1200))
    lv = SessionLevels(previous_high=101, previous_low=98, previous_close=100.4,
                       close_before=99.4)
    pic = fno.picture("SPY", now, lv)
    assert pic.price_change_pct == pytest.approx(1.01, abs=0.01)
    assert pic.bias == "Long Buildup"
    fno.save_picture(pic)
    [saved] = fno.pictures("2026-09-23")
    assert saved["pdh"] == 101 and saved["bias"] == "Long Buildup"


# --------------------------------------------------------------------------- #
# 2. The confluence filter
# --------------------------------------------------------------------------- #
@pytest.fixture
def on(cfg):
    cfg.data.setdefault("fno", {})["confluence"] = {
        "enabled": True, "strategies": ["candlestick_at_level"], "touch_atr": 0.15,
        "when_oi_unknown": "block"}
    return cfg


def rising(calls=None, puts=None):
    return fno.OiRead(call_oi=1000, put_oi=1000, call_change=calls, put_change=puts,
                      basis="vs yesterday")


def test_a_put_at_the_pdh_with_rising_put_oi_passes(on):
    why, notes = confluence.check(_setup(), _levels(), rising(puts=300), on)
    assert why == "" and "tested the previous-day high (PDH)" in notes[0]
    assert "put OI rising" in notes[1]


def test_a_put_away_from_the_pdh_is_refused(on):
    why, _ = confluence.check(_setup(extreme=100.2, close=99.9), _levels(),
                              rising(puts=300), on)
    assert "not at the previous-day high" in why and "PDH" in why


def test_a_put_needs_put_oi_rising(on):
    why, _ = confluence.check(_setup(), _levels(), rising(puts=-200), on)
    assert "put OI is not rising" in why


def test_a_call_needs_a_pdl_sweep_and_rising_call_oi(on):
    call = _setup(direction=Direction.LONG, extreme=97.9, close=98.3, trigger=98.4,
                  pattern="Tweezer Bottom")
    assert confluence.check(call, _levels(), rising(calls=500), on)[0] == ""
    assert "call OI is not rising" in confluence.check(call, _levels(),
                                                       rising(calls=-5), on)[0]
    no_close_back = _setup(direction=Direction.LONG, extreme=97.9, close=97.95,
                           pattern="Tweezer Bottom")
    assert "not at the previous-day low" in confluence.check(
        no_close_back, _levels(), rising(calls=500), on)[0]


def test_unknown_oi_blocks_unless_allowed(on):
    unknown = fno.OiRead()
    why, _ = confluence.check(_setup(), _levels(), unknown, on)
    assert "open interest is unknown" in why
    on.data["fno"]["confluence"]["when_oi_unknown"] = "allow"
    why, notes = confluence.check(_setup(), _levels(), unknown, on)
    assert why == "" and "price level alone" in notes[1]


def test_other_strategies_are_not_filtered(on):
    orb = _setup(strategy=SetupType.ORB_VWAP, pattern="Opening range breakout")
    assert confluence.check(orb, SessionLevels(), fno.OiRead(), on) == ("", [])


# --------------------------------------------------------------------------- #
# 3. The 1:3 gate
# --------------------------------------------------------------------------- #
def test_the_target_is_projected_at_three_r(cfg):
    target, rr, why, note = reward.project(_setup(), _levels(), cfg)
    assert why == "" and rr == 3.0
    assert target == pytest.approx(100.55 - 3 * 0.45)
    assert "1:3" in note


def test_a_level_inside_three_r_refuses_the_trade(cfg):
    tight = _levels()
    tight.opening_range_low = 99.9                  # 1.4R below the entry
    target, rr, why, _ = reward.project(_setup(), tight, cfg)
    assert target == 0.0 and "1.4R of room before the opening range low 99.90" in why


def test_a_strategy_target_beyond_three_r_is_kept(cfg):
    target, rr, why, note = reward.project(_setup(target=98.6), _levels(), cfg)
    assert why == "" and target == 98.6 and rr >= 3 and "strategy's own" in note


def test_the_gatekeeper_refuses_under_one_to_three(cfg):
    gate = RiskGatekeeper(cfg)
    signal = {"symbol": "SPY", "direction": "SHORT", "trigger_price": 100.55,
              "invalidation_level": 101.0, "confidence_score": 0.7}
    put = opt(100, 0.45, 0.80, OptionRight.PUT)
    two_r = gate.review(signal, put, (0.40, 0.50), target=99.65)
    assert not two_r.approved and any("under the 1:3" in r for r in two_r.rejected)
    three_r = gate.review(signal, put, (0.40, 0.50), target=99.20)
    assert any("≥ 1:3" in p for p in three_r.passed)


# --------------------------------------------------------------------------- #
# On the desk
# --------------------------------------------------------------------------- #
class PdhFeed(PutFeed):
    async def candles(self, symbol, interval="5m", include_prepost=False):
        return _candles(TOP_AT_PDH, before=YESTERDAY)


@pytest.fixture
def desk(cfg, monkeypatch, tmp_path):
    from panaoptions.app import OptionsDesk
    from panaoptions.ledger import store
    from panaoptions.models import PreMarketRead

    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "f.db")
    for name in ("orb_vwap", "vwap_ema_pullback", "liquidity_sweep", "va_rejection",
                 "lvn_acceleration", "poc_bounce"):
        cfg.data["strategies"].setdefault(name, {})["enabled"] = False
    cfg.data["strategies"]["candlestick_at_level"].update(
        {"enabled": True, "from": "09:45", "to": "15:00", "timeframe": "5m"})
    cfg.data["agents"]["enabled"] = False
    # These two test the "touch" form of the rule (the pattern AT the PDH);
    # the default two-candle sweep form has its own tests (test_pd_sweep.py).
    cfg.data["fno"]["confluence"]["mode"] = "touch"
    cfg.data["strategies"]["pd_liquidity_sweep"]["enabled"] = False
    cfg.data["contracts"].update(min_dte=0, max_dte=4, max_contract_price=5.0,
                                 min_contract_price=0.10)
    cfg.data["universe"]["symbols"] = ["SPY"]
    monkeypatch.setattr(clock, "now", lambda tz: datetime(2026, 9, 23, 10, 10, tzinfo=ET))

    async def _screen(feed, cfg, now, symbols=None):
        return [PreMarketRead(symbol="SPY", passed=True)]

    monkeypatch.setattr("panaoptions.app.screen", _screen)
    return OptionsDesk(cfg=cfg, feed=PdhFeed())


def _yesterday_put_oi(put_oi):
    fno.record_oi("SPY", datetime(2026, 9, 22, 15, 0, tzinfo=ET),
                  [opt(100, 0.45, 0.8, OptionRight.PUT, oi=put_oi)])


def test_a_tweezer_top_at_the_pdh_with_put_oi_building_is_traded(desk):
    _yesterday_put_oi(1000)                    # today's chain shows 1,800
    asyncio.run(desk.cycle())
    kinds = {e["kind"]: e for e in desk.activity.recent(200)}
    assert "fno.ingest" in kinds and "PDH 101.00" in kinds["fno.ingest"]["detail"]
    assert "confluence.ok" in kinds
    [trade] = desk.ledger.open_trades.values()
    assert trade.side_tag == "LONG_PUT"
    assert fno.pictures("2026-09-23")[0]["put_oi_change"] == 800


def test_the_same_setup_with_put_oi_falling_is_refused(desk):
    _yesterday_put_oi(5000)                    # 1,800 today: puts unwinding
    asyncio.run(desk.cycle())
    kinds = {e["kind"]: e for e in desk.activity.recent(200)}
    assert not desk.ledger.open_trades
    assert "put OI is not rising" in kinds["confluence.refused"]["detail"]
