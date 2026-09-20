"""Journal, post-mortem verdicts and the Mistake Cost Index.

The property under test throughout: PROCESS is judged, not profit.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.journal.analytics import compute_analytics
from app.journal.models import JournalEntry, MistakeTag, SetupType, TradeVerdict
from app.journal.postmortem import PostMortemEngine

NOW = datetime(2026, 9, 18, 10, 0)


def _trade(**kw) -> JournalEntry:
    base = {
        "id": "T", "symbol": "RELIANCE", "setup": SetupType.MEAN_REVERSION,
        "side": "BUY",
        "planned_entry": 1000.0, "planned_stop": 980.0, "planned_target": 1040.0,
        "planned_quantity": 50, "actual_entry": 1000.0,
        "entry_ts": NOW, "exit_ts": NOW + timedelta(minutes=20),
        "context": {"capital": 100_000},
    }
    base.update(kw)
    return JournalEntry(**base).compute()


# --------------------------------------------------------------------------- #
# The four verdicts
# --------------------------------------------------------------------------- #
def test_clean_loss_is_an_acceptable_loss():
    t = _trade(actual_exit=980.0)
    assert t.verdict == TradeVerdict.GOOD_LOSS
    assert t.r_multiple == pytest.approx(-1.0)
    assert not t.rule_violation


def test_clean_win_is_a_good_win():
    t = _trade(actual_exit=1040.0)
    assert t.verdict == TradeVerdict.GOOD_WIN
    assert t.r_multiple == pytest.approx(2.0)


def test_a_rule_breaking_winner_is_still_a_bad_trade():
    """The whole point of the journal: profit does not excuse indiscipline."""
    t = _trade(actual_exit=1040.0, mistakes=[MistakeTag.CHASED_PRICE])
    assert t.pnl > 0
    assert t.verdict == TradeVerdict.BAD_WIN
    assert t.rule_violation


def test_rule_breaking_loser_is_a_bad_loss():
    t = _trade(actual_exit=960.0, stop_honoured=False)
    assert t.verdict == TradeVerdict.BAD_LOSS


# --------------------------------------------------------------------------- #
# R-multiple realisation
# --------------------------------------------------------------------------- #
def test_r_multiple_is_pnl_over_initial_risk():
    t = _trade(actual_exit=1040.0)
    # risk = 20 points x 50 = 1000; pnl = 40 x 50 = 2000 → 2R
    assert t.initial_risk == pytest.approx(1000.0)
    assert t.pnl == pytest.approx(2000.0)
    assert t.r_multiple == pytest.approx(2.0)


def test_short_side_r_multiple_has_the_right_sign():
    t = _trade(side="SELL", planned_entry=1000.0, planned_stop=1020.0,
               planned_target=960.0, actual_entry=1000.0, actual_exit=960.0)
    assert t.pnl > 0
    assert t.r_multiple == pytest.approx(2.0)


def test_slippage_and_hold_time_are_recorded():
    t = _trade(actual_entry=1003.0, actual_exit=1040.0,
               exit_ts=NOW + timedelta(minutes=37))
    assert t.slippage == pytest.approx(3.0)
    assert t.hold_minutes == pytest.approx(37.0)


# --------------------------------------------------------------------------- #
# Deterministic mistake detection
# --------------------------------------------------------------------------- #
@pytest.fixture
def engine(cfg):
    cfg.settings.setdefault("risk", {})["time_stop_minutes"] = 30
    cfg.settings["risk"]["max_risk_per_trade_pct"] = 2.0
    return PostMortemEngine(cfg)


def test_detects_a_widened_stop_from_the_loss_size(engine):
    """Losing far more than the planned risk proves the stop was not honoured."""
    t = _trade(actual_exit=940.0)      # -3R against a 1R plan
    found = engine.detect_mistakes(t)
    assert MistakeTag.MOVED_STOP in found
    assert t.stop_honoured is False


def test_detects_an_explicitly_moved_stop(engine):
    t = _trade(actual_exit=970.0, context={"capital": 100_000, "actual_stop": 965.0})
    assert MistakeTag.MOVED_STOP in engine.detect_mistakes(t)


def test_detects_a_chased_entry(engine):
    """Paying more than a quarter of the stop distance to get in is a chase."""
    t = _trade(actual_entry=1006.0, actual_exit=1040.0)   # 6 pts on a 20 pt stop
    assert MistakeTag.CHASED_PRICE in engine.detect_mistakes(t)


def test_normal_slippage_is_not_called_a_chase(engine):
    t = _trade(actual_entry=1001.0, actual_exit=1040.0)
    assert MistakeTag.CHASED_PRICE not in engine.detect_mistakes(t)


def test_detects_oversizing_against_the_risk_limit(engine):
    # 20 points x 1000 units = 20,000 risked on 100,000 = 20%
    t = _trade(planned_quantity=1000, actual_exit=1040.0)
    assert MistakeTag.SIZED_TOO_LARGE in engine.detect_mistakes(t)


def test_detects_an_early_exit(engine):
    t = _trade(actual_exit=1005.0)     # +0.25R on a 2R target
    assert MistakeTag.EXITED_EARLY in engine.detect_mistakes(t)


def test_a_time_stop_exit_is_not_punished_as_an_early_exit(engine):
    """Exiting early BECAUSE the time stop fired is correct, not a mistake."""
    t = _trade(actual_exit=1005.0,
               context={"capital": 100_000, "time_stop_hit": True})
    assert MistakeTag.EXITED_EARLY not in engine.detect_mistakes(t)


def test_detects_holding_past_the_time_stop(engine):
    t = _trade(actual_exit=985.0, exit_ts=NOW + timedelta(minutes=90))
    assert MistakeTag.HELD_PAST_TIME_STOP in engine.detect_mistakes(t)


def test_detects_missing_volume_confirmation(engine):
    t = _trade(actual_exit=1040.0, context={"capital": 100_000, "volume_surge": 0.6})
    assert MistakeTag.NO_VOLUME_CONFIRM in engine.detect_mistakes(t)


def test_a_clean_trade_produces_no_false_positives(engine):
    t = _trade(actual_exit=1040.0,
               context={"capital": 100_000, "volume_surge": 2.2})
    assert engine.detect_mistakes(t) == []


# --------------------------------------------------------------------------- #
# The card itself
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_card_scores_discipline_not_profit(engine):
    clean_loss = await engine.build(_trade(actual_exit=980.0,
                                           context={"capital": 100_000,
                                                    "volume_surge": 2.0}))
    dirty_win = await engine.build(_trade(actual_exit=1040.0, actual_entry=1008.0))

    # The losing trade followed its plan; the winning one chased.
    assert clean_loss.execution_score > dirty_win.execution_score
    assert clean_loss.verdict == TradeVerdict.GOOD_LOSS
    assert dirty_win.verdict == TradeVerdict.BAD_WIN


@pytest.mark.asyncio
async def test_card_markdown_has_every_required_section(engine):
    card = await engine.build(_trade(actual_exit=940.0))
    md = card.to_markdown()
    for heading in ("Trade Post-Mortem Card", "Ticker & Setup",
                    "Execution Quality Score", "The Core Violation",
                    "Technical Breakdown", "Entry Analysis",
                    "Exit & Stop Discipline", "Root Cause & Bias",
                    "Corrective Protocol"):
        assert heading in md


@pytest.mark.asyncio
async def test_corrective_protocol_is_specific_not_platitude(engine):
    card = await engine.build(_trade(actual_exit=940.0))
    # A real rule names a mechanism; "be disciplined" would not.
    assert len(card.corrective_protocol) > 40
    assert any(w in card.corrective_protocol.lower()
               for w in ("stop", "order", "limit", "rule", "entry"))


# --------------------------------------------------------------------------- #
# Mistake Cost Index
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mistake_cost_index_counts_only_rule_violations(cfg, tmp_path):
    """A loss on a clean setup is the cost of doing business, not a mistake.
    The index must isolate what indiscipline alone costs."""
    from app.journal import store
    from app.storage import db

    db.init_db()
    store.init_journal()
    store.JOURNAL_DIR = tmp_path        # don't write into the repo during tests

    engine = PostMortemEngine(cfg)

    clean = _trade(id="CLEAN", actual_exit=980.0,
                   context={"capital": 100_000, "volume_surge": 2.0})
    dirty = _trade(id="DIRTY", actual_exit=940.0)      # widened stop, -3R

    for t in (clean, dirty):
        card = await engine.build(t)
        t.execution_score = card.execution_score
        store.save_entry(t)

    stats = compute_analytics()
    ids = {r["id"] for r in store.entries(limit=50)}
    assert {"CLEAN", "DIRTY"} <= ids

    # Only the rule-breaking loss is counted in the index.
    assert stats["mistake_cost_index"] == pytest.approx(3000.0, abs=1.0)
    assert stats["clean_loss_total"] == pytest.approx(1000.0, abs=1.0)
    assert stats["avoidable_loss_pct"] == pytest.approx(75.0, abs=1.0)


def test_analytics_handle_an_empty_journal():
    stats = compute_analytics(limit=0)
    assert stats["total"] == 0
    assert stats["mistake_cost_index"] == 0.0
