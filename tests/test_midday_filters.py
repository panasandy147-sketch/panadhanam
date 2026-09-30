"""The four over-filtering fixes from the 30 Sept midday rejection logs:
regime-dependent reversal confluence, target snapping at the PDH/PDL, held
symbols left to the position manager (pyramid adds allowed), and the Band B
morning promotion."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agents import fno_confluence as fc
from app.core.models import AgentReport, Bias, Candle, MarketContext, Quote
from tests.test_fno_confluence import _reports
from tests.test_screener import EXTENDED, _at


@pytest.fixture
def regime(cfg):
    cfg.settings["fno_confluence"] = {
        "enabled": True, "touch_atr": 0.15, "lookback_bars": 3, "when_oi_unknown": "block",
        "room_check": True, "regime_rules": True, "rangebound_proximity_pct": 0.25,
        "trend_pullback_pct": 0.30}
    return cfg


def _ctx(close, lows, highs, regime_name, vwap=None, ema9=None, ema20=None,
         call=500.0, put=500.0):
    bars = [Candle(ts=datetime(2026, 9, 30, 5, 0, tzinfo=UTC) + timedelta(minutes=5 * i),
                   open=close, high=h, low=lo, close=close)
            for i, (lo, h) in enumerate(zip(lows, highs, strict=False))]
    ctx = MarketContext(symbol="RELIANCE", cycle_id="t", candles={"5m": bars},
                        quote=Quote(symbol="RELIANCE", last_price=close))
    ctx.indicators = {
        "primary": {"last_close": close, "atr": 6.0, "regime": regime_name,
                    "vwap": vwap, "ema9": ema9, "ema20": ema20},
        "previous_day": {"high": 1020.0, "low": 1000.0, "close": 1015.0,
                         "close_before": 1005.0},
        "derivatives": {"call_oi_change": call, "put_oi_change": put, "oi_known": True},
    }
    return ctx


# --------------------------------------------------------------------------- #
# 1. Regime-dependent confluence
# --------------------------------------------------------------------------- #
def test_rangebound_needs_the_pdl_within_a_quarter_percent(regime):
    at_pdl = _ctx(1003.0, [1004, 1002.2, 1003], [1006, 1005, 1004], "rangebound")
    assert fc.confluence_reason(at_pdl, Bias.BULLISH, _reports("hammer"), regime) == ""
    # 1003.2 is 0.32% above the PDL 1000: not at it.
    away = _ctx(1006.0, [1004, 1003.2, 1005], [1008, 1007, 1007], "rangebound")
    assert "not at the previous-day low" in fc.confluence_reason(
        away, Bias.BULLISH, _reports("hammer"), regime)


def test_a_trend_pullback_to_vwap_needs_no_pdh_or_pdl(regime):
    # trending_up, a hammer 1.2% above the PDL — refused under the old rule —
    # on a pullback to VWAP 1012.0 (low 1012.5 is 0.05% away), closed above.
    ctx = _ctx(1014.0, [1013, 1012.5, 1013.5], [1015, 1014, 1015], "trending_up",
               vwap=1012.0, ema9=1016.0, ema20=1010.0, call=-100)   # call OI falling
    assert fc.confluence_reason(ctx, Bias.BULLISH, _reports("hammer"), regime) == ""
    # Too far from every level: refused, naming each distance.
    far = _ctx(1014.0, [1013, 1012.5, 1013.5], [1015, 1014, 1015], "trending_up",
               vwap=1000.0, ema9=1030.0, ema20=990.0)
    why = fc.confluence_reason(far, Bias.BULLISH, _reports("hammer"), regime)
    assert "pullback within 0.3%" in why and "VWAP 1,000.00" in why


def test_the_session_poc_counts_as_a_pullback_level(regime):
    ctx = _ctx(1014.0, [1013, 1012.9, 1013.5], [1015, 1014, 1015], "trending_up",
               vwap=990.0, ema9=1030.0, ema20=990.0)
    reports = _reports("bullish_engulfing") + [AgentReport(
        agent_id="volume_profile", symbol="RELIANCE",
        extra={"profiles": {"current": {"poc": 1012.0}}})]
    assert fc.confluence_reason(ctx, Bias.BULLISH, reports, regime) == ""


def test_a_reversal_against_the_trend_keeps_the_pdh_rule(regime):
    # A shooting star short in a trending_UP tape: still needs the PDH.
    ctx = _ctx(1014.0, [1013, 1012.5, 1013.5], [1015, 1016, 1015], "trending_up",
               vwap=1014.2)
    assert "not at the previous-day high" in fc.confluence_reason(
        ctx, Bias.BEARISH, _reports("shooting_star"), regime)
    down = _ctx(1014.0, [1013, 1012.5, 1013.5], [1015, 1016.0, 1015], "trending_down",
                vwap=1015.5, ema9=1018.0, ema20=1019.0)
    assert fc.confluence_reason(down, Bias.BEARISH, _reports("shooting_star"), regime) == ""


# --------------------------------------------------------------------------- #
# 2. Target snapping at the PDH / PDL
# --------------------------------------------------------------------------- #
def test_room_between_2_2_and_3r_snaps_the_target_inside_the_level(regime):
    regime.settings.setdefault("risk", {}).update(
        min_risk_reward=3.0, target_snap={"enabled": True, "min_r": 2.2, "ticks": 2})
    ctx = _ctx(1006.0, [1005, 1005, 1005], [1007, 1007, 1007], "rangebound")
    # entry 1006.5, stop 1001.5: 5 points of risk; the PDH 1020 is 2.7R away.
    why, target, r = fc.room_snap(ctx, Bias.BULLISH, 1006.5, 1001.5, regime, 0.05)
    assert why == "" and target == 1019.9 and r == pytest.approx(2.68, abs=0.01)
    # 3R or more: the standard target.
    assert fc.room_snap(ctx, Bias.BULLISH, 1004.0, 1000.0, regime, 0.05)[:2] == ("", None)
    # Under 2.2R: refused.
    why, target, _ = fc.room_snap(ctx, Bias.BULLISH, 1010.0, 1005.0, regime, 0.05)
    assert "only 2.0R of room" in why and target is None
    # Shorts snap above the PDL.
    why, target, _ = fc.room_snap(ctx, Bias.BEARISH, 1013.0, 1018.0, regime, 0.05)
    assert why == "" and target == 1000.1


# --------------------------------------------------------------------------- #
# 3. Held symbols: no new-entry scan; pyramid adds allowed
# --------------------------------------------------------------------------- #
def test_one_position_per_symbol_blocks_base_entries_not_pyramid_adds(cfg, monkeypatch):
    from app.agents.risk import RiskManager
    from app.storage import db
    monkeypatch.setattr(db, "open_signals", lambda: [{"symbol": "RBLX"}])
    rm = RiskManager(cfg)
    assert any("Already holding RBLX" in r for r in rm.symbol_checks("RBLX"))
    assert rm.symbol_checks("RBLX", order="PYRAMID_ADD") == []
    assert not any("Already holding" in r for r in rm.approve_add("RBLX", 50.0, 100.0))
    assert any("No averaging down" in r for r in rm.approve_add("RBLX", -5.0, 100.0))


@pytest.mark.asyncio
async def test_the_engine_does_not_rescan_a_held_symbol(cfg, monkeypatch):
    from app.brokers.paper import PaperBroker
    from app.scheduler import TradingEngine
    from app.storage import db
    broker = PaperBroker(config={"total_capital": 350_000})
    await broker.connect()
    eng = TradingEngine(broker, cfg)
    monkeypatch.setattr(db, "open_signals", lambda: [{"symbol": "INFY"}])
    seen = []

    async def cycle(symbol, *a, **k):
        seen.append(symbol)
        return {"symbol": symbol, "signal": None}

    async def nothing(*a, **k):
        return {}

    monkeypatch.setattr(eng, "_cycle_for_symbol", cycle)
    monkeypatch.setattr(eng, "_prefetch", nothing)
    out = await eng.run_cycle(["INFY", "TCS"])
    assert seen == ["TCS"]
    assert any(o.get("held") and o["symbol"] == "INFY" for o in out)


# --------------------------------------------------------------------------- #
# 4. Band B morning promotion
# --------------------------------------------------------------------------- #
def test_a_trending_band_b_name_with_a_strong_composite_trades_in_the_morning(
        cfg, monkeypatch):
    from app.agents.risk import RiskManager
    from tests.test_screener import _listed
    cfg.switch_market("IN")
    for s in (cfg._base_settings, cfg.settings):
        s.setdefault("screener", {})["enabled"] = True
        s["screener"]["band_b_promotion"] = {"enabled": True, "min_composite": 0.85}
    _listed(cfg)
    _at(monkeypatch, 9, 45)
    rm = RiskManager(cfg)
    up = {"primary": {**EXTENDED["primary"], "regime": "trending_up"}}
    flat = {"primary": {**EXTENDED["primary"], "regime": "rangebound"}}
    # SBIN is Band B, longs only.
    assert rm.screener_checks("SBIN", Bias.BULLISH, up, 0.90) == []
    assert any("Band B enters only" in r
               for r in rm.screener_checks("SBIN", Bias.BULLISH, up, 0.80))
    assert any("Band B enters only" in r
               for r in rm.screener_checks("SBIN", Bias.BULLISH, flat, 0.95))
    # The side rule still holds: a promoted Band B long-only name cannot short.
    down = {"primary": {**EXTENDED["primary"], "regime": "trending_down"}}
    assert any("longs-only" in r for r in rm.screener_checks("SBIN", Bias.BEARISH, down, -0.9))
