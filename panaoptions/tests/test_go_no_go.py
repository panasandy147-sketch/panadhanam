"""The previous-day GO/NO-GO for the 5-minute candlestick strategies: a LONG
CALL only after a clean sweep of the PDL, a LONG PUT only after the PDH."""
from __future__ import annotations

from panaoptions.engine import strategies
from panaoptions.models import Direction
from tests.test_pd_liquidity_sweep import TODAY, bars, mirror, session


def _on(cfg, only=("candlestick_at_level",)):
    cfg.data["fno"]["go_no_go"] = {"enabled": True, "strategies": list(only)}
    for key in ("pd_liquidity_sweep", "orb_vwap", "vwap_ema_pullback", "liquidity_sweep",
                "va_rejection", "lvn_acceleration", "poc_bounce"):
        cfg.data["strategies"].setdefault(key, {})["enabled"] = False
    cfg.data["strategies"]["candlestick_at_level"].update({"from": "09:00", "to": "16:00"})
    return cfg


FLAT = [(100.0, 100.3, 99.7, 100.0)] * 4        # stays inside yesterday's range


def test_no_sweep_skips_the_candlestick_with_the_reason(cfg):
    _on(cfg)
    tape = bars(FLAT)
    winner, attempts = strategies.evaluate_all("SPY", tape, session(tape), cfg)
    assert winner is None
    [a] = [a for a in attempts if a.strategy.name == "CANDLESTICK_AT_LEVEL"]
    assert a.blockers == ["No institutional sweep of previous day extremes."]
    assert not a.triggered


def test_a_pdl_sweep_opens_the_gate_for_calls_only(cfg):
    _on(cfg)
    tape = bars(TODAY)                              # sweeps the PDL, closes back inside
    found = strategies.pd_sweep_now(strategies.localise(
        __import__("panaoptions.engine.indicators", fromlist=["x"]).to_frame(tape),
        "America/New_York"), session(tape), cfg)
    assert found and found["direction"] > 0
    _, attempts = strategies.evaluate_all("SPY", tape, session(tape), cfg)
    [a] = [a for a in attempts if a.strategy.name == "CANDLESTICK_AT_LEVEL"]
    assert "No institutional sweep of previous day extremes." not in a.blockers
    if a.direction is Direction.SHORT and not a.blockers[:-1]:
        assert "LONG PUT needs the PDH" in a.blockers[-1]


def test_a_pdh_sweep_is_the_mirror(cfg):
    _on(cfg)
    tape = bars(mirror(TODAY), yesterday=mirror(
        __import__("tests.test_pd_liquidity_sweep", fromlist=["YESTERDAY"]).YESTERDAY))
    lv = session(tape)
    df = strategies.localise(
        __import__("panaoptions.engine.indicators", fromlist=["x"]).to_frame(tape),
        "America/New_York")
    assert strategies.pd_sweep_now(df, lv, cfg)["direction"] < 0


def test_off_switch_and_other_strategies_untouched(cfg):
    _on(cfg)
    cfg.data["fno"]["go_no_go"]["enabled"] = False
    tape = bars(FLAT)
    _, attempts = strategies.evaluate_all("SPY", tape, session(tape), cfg)
    assert all("No institutional sweep" not in " ".join(a.blockers) for a in attempts)
    assert strategies.pd_gate_strategies(cfg) == set()


def test_the_rules_page_names_the_go_no_go():
    import json

    from panaoptions import rules
    from panaoptions.config import Config
    text = json.dumps(rules.build(Config(), capital=4000))
    assert "No institutional sweep of previous day extremes" in text
    assert "candlestick at level" in text
