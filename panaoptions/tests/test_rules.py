"""The Rules pop-up: written from the live config, reachable from the header."""
from __future__ import annotations

from pathlib import Path

from panaoptions import rules

STATIC = Path(__file__).resolve().parents[1] / "panaoptions" / "web" / "static"


def test_every_strategy_is_written_out_with_its_live_window(cfg):
    doc = rules.build(cfg, capital=4000)
    [section] = [s for s in doc["sections"] if s["title"] == "The strategies"]
    keys = [s["key"] for s in section["strategies"]]
    assert keys == ["orb_vwap", "vwap_ema_pullback", "liquidity_sweep",
                    "candlestick_at_level"]
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
