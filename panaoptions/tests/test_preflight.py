"""Preflight: catching rule combinations that can never all hold.

Each of these produces the same symptom — a desk that scans and takes nothing —
which is indistinguishable from a quiet market. The whole point of this module
is that the arithmetic runs before the market opens rather than being inferred
from a fortnight of silence.
"""
from __future__ import annotations

from panaoptions.preflight import check, report


def _blockers(cfg):
    return [f for f in check(cfg) if f.level == "blocker"]


def _settings(cfg, **kw):
    for dotted, value in kw.items():
        section, _, key = dotted.partition("__")
        cfg.data[section][key] = value
    return cfg


# --------------------------------------------------------------------------- #
def test_the_shipped_config_is_flagged_because_500_cannot_buy_at_the_money(cfg):
    blockers = _blockers(cfg)
    assert blockers, "a $500 account cannot buy an ATM mega-cap contract"
    assert any("starting_capital" in f.setting for f in blockers)


def test_the_message_names_the_capital_actually_needed(cfg):
    blocker = next(f for f in _blockers(cfg) if "capital" in f.setting)
    # 20% of X must cover the ~$319 cheapest ATM contract.
    assert "$1,59" in blocker.fix or "$1,6" in blocker.fix
    assert "PANAOPTIONS_CAPITAL" in blocker.command


def test_the_message_offers_the_cheaper_universe_as_the_other_way_out(cfg):
    blocker = next(f for f in _blockers(cfg) if "capital" in f.setting)
    assert "INTC" in blocker.fix, \
        "naming symbols the account CAN buy beats saying the config is wrong"


def test_raising_capital_clears_every_blocker(cfg):
    _settings(cfg, account__starting_capital=2000.0)
    assert _blockers(cfg) == []


def test_swapping_to_the_cheaper_universe_also_clears_it(cfg):
    cfg.data["universe"]["symbols"] = cfg.data["universe"]["small_account_alternative"]
    assert _blockers(cfg) == []


# --------------------------------------------------------------------------- #
def test_the_original_price_cap_is_caught_as_an_empty_set(cfg):
    # $1.00 a share with a 0.45-0.60 delta band: the bug this module exists for.
    _settings(cfg, contracts__max_contract_price=1.00,
              contracts__min_contract_price=0.60)
    blockers = _blockers(cfg)

    assert any("max_contract_price" in f.setting and "min_delta" in f.setting
               for f in blockers)
    caught = next(f for f in blockers if "min_delta" in f.setting)
    assert "at the money" in caught.problem
    assert "TSLA" in caught.problem


def test_a_price_floor_above_the_deployment_budget_is_caught(cfg):
    # Raising the cap alone does not help if the floor still outruns the budget.
    _settings(cfg, contracts__min_contract_price=5.00)
    blockers = _blockers(cfg)
    assert any("max_capital_deployed_pct" in f.setting for f in blockers)


def test_a_minimum_above_the_maximum_is_caught(cfg):
    _settings(cfg, contracts__min_contract_price=9.0,
              contracts__max_contract_price=2.0)
    assert any("min_contract_price" in f.setting for f in _blockers(cfg))


# --------------------------------------------------------------------------- #
def test_a_daily_limit_smaller_than_one_stop_is_a_warning(cfg):
    _settings(cfg, account__starting_capital=2000.0, risk__daily_loss_limit=20.0)
    warnings = [f for f in check(cfg) if f.level == "warning"]

    caught = next(f for f in warnings if "daily_loss_limit" in f.setting)
    assert "first loser" in caught.problem, \
        "a limit below one stop is really a one-trade-a-day rule"


def test_the_real_risk_per_trade_is_surfaced_as_a_warning(cfg):
    # 20% deployed behind a 20% stop is 4% at risk — four times a normal desk.
    _settings(cfg, account__starting_capital=2000.0)
    warnings = [f for f in check(cfg) if f.level == "warning"]
    caught = next(f for f in warnings if "max_capital_deployed_pct" in f.setting)

    assert "4.0% of the account" in caught.problem
    assert "deliberate choice" in caught.fix, \
        "it is their call to make, not an error to refuse"


def test_a_conventional_configuration_raises_nothing(cfg):
    _settings(cfg, account__starting_capital=25_000.0,
              risk__max_capital_deployed_pct=8.0,
              risk__daily_loss_limit=500.0)
    assert check(cfg) == []


# --------------------------------------------------------------------------- #
def test_report_logs_blockers_as_errors(cfg, caplog, monkeypatch):
    import logging

    # The app's logger deliberately does not propagate to root; caplog reads
    # the root handler, so turn propagation on just for this assertion.
    monkeypatch.setattr(logging.getLogger("panaoptions"), "propagate", True)
    with caplog.at_level(logging.WARNING, logger="panaoptions.preflight"):
        findings = report(cfg)

    assert findings
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_every_finding_carries_a_fix_not_just_a_complaint(cfg):
    _settings(cfg, contracts__max_contract_price=1.00)
    for finding in check(cfg):
        assert finding.fix and len(finding.fix) > 20
        assert finding.setting
        assert "[BLOCKER]" in finding.render() or "[warning]" in finding.render()


def test_a_single_contract_budget_flags_the_scale_out_as_inert(cfg):
    # Half of one contract does not exist, so "exit 50% at +40%" closes the
    # whole position and the runner never exists. A rule that silently does
    # nothing is the same failure as a rule that never fires.
    _settings(cfg, account__starting_capital=2000.0)
    warnings = [f for f in check(cfg) if f.level == "warning"]

    caught = next(f for f in warnings if "take_profit_1_size_pct" in f.setting)
    assert "half a contract does not exist" in caught.problem
    assert "funds two contracts" in caught.fix


def test_a_budget_that_funds_two_contracts_does_not_flag_it(cfg):
    _settings(cfg, account__starting_capital=6000.0)
    warnings = [f for f in check(cfg) if f.level == "warning"]
    assert not any("take_profit_1_size_pct" in f.setting for f in warnings)


def test_a_blocker_carries_a_command_you_can_actually_paste(cfg):
    # "set PANAOPTIONS_CAPITAL in the environment" is not something you can
    # type. Twice now the fix was understood and the command still had to be
    # worked out, so the finding carries one.
    blocker = next(f for f in _blockers(cfg) if "capital" in f.setting)

    assert blocker.command
    assert "run.py --set PANAOPTIONS_CAPITAL=" in blocker.command
    assert "Run this:" in blocker.render()


def test_the_suggested_command_names_a_real_interpreter_not_bare_python(cfg):
    # A bare `python` on Windows is the Microsoft Store build with none of the
    # packages, so suggesting it sends people back to ModuleNotFoundError.
    blocker = next(f for f in _blockers(cfg) if "capital" in f.setting)
    assert ".venv" in blocker.command or blocker.command.startswith("python ")


def test_the_suggested_capital_is_a_round_number(cfg):
    # Nobody sets their account size to $1,593.
    blocker = next(f for f in _blockers(cfg) if "capital" in f.setting)
    amount = int(blocker.command.rsplit("=", 1)[1])

    assert amount % 500 == 0
    assert amount >= 1593, "rounding must go up, or it still will not fit"


def test_a_warning_needs_no_command(cfg):
    _settings(cfg, account__starting_capital=2000.0)
    for finding in check(cfg):
        if finding.level == "warning":
            assert not finding.command
