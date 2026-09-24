"""The Rules pop-up: written from the live config, reachable from the header."""
from __future__ import annotations

from pathlib import Path

from app.core.rules import build

STATIC = Path(__file__).resolve().parents[1] / "dashboard" / "static"


def _rows(doc):
    return [r for s in doc["sections"] for r in s.get("rules", [])]


def test_the_vote_gate_is_written_with_the_live_numbers(cfg):
    doc = build(cfg, capital=100_000)
    by_setting = {r["setting"]: r["value"] for r in _rows(doc)}
    assert by_setting["consensus.min_confirmations"] == str(
        cfg.get("consensus.min_confirmations"))
    assert by_setting["consensus.min_composite_score"] == str(
        cfg.get("consensus.min_composite_score"))


def test_sizing_is_shown_in_money_for_the_real_account(cfg):
    cfg.switch_market("US")
    try:
        doc = build(cfg, capital=100_000)
        [risk] = [r for r in _rows(doc) if r["setting"] == "risk.risk_per_trade_pct"]
        pct = float(cfg.get("risk.risk_per_trade_pct"))
        assert risk["value"] == f"{pct:g}% = ${1000 * pct:,.0f}"
        [cutoff] = [r for r in _rows(doc) if r["setting"].endswith("no_new_entry_after")]
        assert cutoff["value"] == "15:30" and "us.yaml" in cutoff["setting"]
    finally:
        cfg.switch_market("IN")


def test_every_analyst_is_described(cfg):
    doc = build(cfg, capital=100_000)
    [section] = [s for s in doc["sections"] if s.get("analysts")]
    names = [a["name"] for a in section["analysts"]]
    assert len(names) == 5 and all(a["reads"] for a in section["analysts"])


def test_every_rule_names_the_setting_that_changes_it(cfg):
    for row in _rows(build(cfg, capital=100_000)):
        assert row["setting"], row["text"]


def test_the_header_links_to_the_rules_pop_up():
    page = (STATIC / "index.html").read_text()
    header = page[page.index("<header>"):page.index("</header>")]
    assert 'id="btn-rules"' in header and 'href="#rules"' in header
    assert '<dialog id="rules-dialog"' in page
    script = (STATIC / "app.js").read_text()
    assert 'fetch("/api/rules")' in script
    assert 'location.hash === "#rules"' in script
