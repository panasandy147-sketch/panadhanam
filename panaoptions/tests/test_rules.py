"""The Rules pop-up: written from the live config, reachable from the header."""
from __future__ import annotations

from pathlib import Path

from panaoptions import rules

STATIC = Path(__file__).resolve().parents[1] / "panaoptions" / "web" / "static"


def test_every_strategy_is_written_out_with_its_live_window(cfg):
    doc = rules.build(cfg, capital=4000)
    [section] = [s for s in doc["sections"] if s["title"] == "The strategies"]
    keys = [s["key"] for s in section["strategies"]]
    assert keys == ["pd_liquidity_sweep", "orb_vwap", "vwap_ema_pullback", "liquidity_sweep",
                    "candlestick_at_level", "va_rejection", "lvn_acceleration",
                    "poc_bounce", "volatility_breakout"]
    # ...which is every strategy the desk actually runs, in its order.
    from panaoptions.engine.strategies import ALL
    assert keys == [cls(cfg).key for cls in ALL]
    for st in section["strategies"]:
        assert st["buy"] and st["wrong"]
        assert cfg.get(f"strategies.{st['key']}.to") in st["window"]


def test_the_numbers_follow_the_config_not_a_copy(cfg):
    cfg.data["risk"]["max_capital_deployed_pct"] = 12.5
    try:
        doc = rules.build(cfg, capital=4000)
        rows = [r for s in doc["sections"] for r in s.get("rules", [])]
        [per_trade] = [r for r in rows if r["setting"] == "risk.max_capital_deployed_pct"]
        assert per_trade["value"] == "12.5% = $500"
    finally:
        cfg.data["risk"]["max_capital_deployed_pct"] = 20.0


def test_every_rule_names_the_setting_that_changes_it(cfg):
    doc = rules.build(cfg, capital=4000)
    for section in doc["sections"]:
        for row in section.get("rules", []):
            assert row["setting"], row["text"]


def test_the_header_links_to_the_rules_pop_up():
    page = (STATIC / "index.html").read_text()
    header = page[page.index("<header>"):page.index("</header>")]
    assert 'id="btn-rules"' in header and 'href="#rules"' in header
    assert '<dialog id="rules-dialog"' in page
    script = (STATIC / "app.js").read_text()
    assert 'getJSON("/api/rules")' in script
    assert 'location.hash === "#rules"' in script


def test_the_new_rules_are_on_the_page(cfg):
    """Every rule the desk enforces is written out: the F&O confluence, the
    1:3 gate, the debit-spread ladder, calls and puts, and how a trade ends."""
    import json
    doc = rules.build(cfg, capital=4000)
    text = json.dumps(doc)
    section = next(s for s in doc["sections"]
                   if s["title"] == "Previous-day F&O and reward to risk")
    values = {r["setting"]: r["value"] for r in section["rules"]}
    assert values["risk.min_reward_risk / reward_room_levels"] == "1:3"
    assert values["fno.confluence.when_oi_unknown"] == "block"
    for words in ("previous-day LOW", "rising call OI", "LONG_PUT", "double rejection",
                  "debit spread", "skipped by a hard risk gate", "Long Buildup"):
        assert words.lower() in text.lower(), words


def test_the_sweep_and_execution_rules_are_on_the_page(cfg):
    """The PD sweep, the sweep confluence, single-leg grace, the 0.25 floor —
    and, on India, the 12% opening allowance and premium x lot."""
    import json

    from panaoptions.config import Config
    doc = rules.build(cfg, capital=4000)
    rows = {r["setting"]: r for s in doc["sections"] for r in s.get("rules", [])}
    assert rows["fno.confluence.mode / proximity_pct / touch_atr"]["value"] == "sweep"
    assert "0.5%" in rows["fno.confluence.mode / proximity_pct / touch_atr"]["text"]
    assert rows["contracts.single_leg_grace"]["value"] == "100%"
    assert rows["contracts.fallback_order / debit_spread / liquidity"]["value"] == "0.25"
    text = json.dumps(doc)
    assert "Previous Day Liquidity Sweep" in text or "PD Liquidity Sweep" in text

    india = Config(market="IN")
    doc = rules.build(india, capital=350000)
    rows = {r["setting"]: r for s in doc["sections"] for r in s.get("rules", [])}
    opening = rows["contracts.opening_spread"]
    assert opening["value"] == "12%"
    assert "09:15" in opening["text"] and "10:00" in opening["text"]
    assert "HDFCBANK 650" in rows["data.lot_sizes"]["value"]
    assert "INFY 400" in rows["data.lot_sizes"]["value"]
