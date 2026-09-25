"""The contract filter, and the empty set its shipped settings describe."""
from __future__ import annotations

import pytest

from panaoptions.engine.contracts import affordable_delta, choose
from panaoptions.models import Direction, OptionContract, OptionRight


def _chain(symbol="AAPL", dte=10, rows=None):
    rows = rows or [(215, 0.88, 15.6), (225, 0.64, 7.4), (230, 0.52, 4.6),
                    (240, 0.21, 1.20), (245, 0.11, 0.55)]
    return [OptionContract(symbol=symbol, right=OptionRight.CALL, strike=k,
                           expiry="2026-10-02", dte=dte,
                           bid=round(mid * 0.99, 2), ask=round(mid * 1.01, 2),
                           delta=d, implied_volatility=0.25)
            for k, d, mid in rows]


def _budget_of_one_hundred(cfg):
    """The original brief's cap. The shipped config no longer uses it, but the
    filter must still explain itself when somebody sets it back."""
    cfg.data["contracts"]["min_contract_price"] = 0.60
    cfg.data["contracts"]["max_contract_price"] = 1.00
    return cfg


def test_an_at_the_money_contract_cannot_fit_a_hundred_dollar_budget(cfg):
    # The headline problem with the original brief: the filter must fail
    # LOUDLY rather than return an empty list in silence.
    cfg = _budget_of_one_hundred(cfg)
    result = choose("AAPL", _chain(), Direction.LONG, cfg)

    assert result.chosen is None
    assert result.closest_by_price is not None
    assert "cannot both hold" in result.note
    assert "$460" in result.note, "the note must name the real cost"


def test_the_rejection_reasons_are_counted_not_discarded(cfg):
    cfg = _budget_of_one_hundred(cfg)
    result = choose("AAPL", _chain(), Direction.LONG, cfg)

    assert result.examined == 5
    assert sum(result.rejected.values()) == 5
    assert any("delta" in k for k in result.rejected)


def test_raising_the_budget_makes_the_same_chain_tradeable(cfg):
    cfg.data["contracts"]["max_contract_price"] = 8.00
    cfg.data["contracts"]["min_contract_price"] = 0.50

    result = choose("AAPL", _chain(), Direction.LONG, cfg)
    assert result.chosen is not None
    assert 0.45 <= abs(result.chosen.delta) <= 0.60
    assert result.chosen.strike == 230, "the cheapest qualifying strike"


def test_the_cheapest_qualifying_contract_wins(cfg):
    cfg.data["contracts"]["max_contract_price"] = 20.00
    cfg.data["contracts"]["min_contract_price"] = 0.10
    cfg.data["contracts"]["min_delta"] = 0.10
    cfg.data["contracts"]["max_delta"] = 0.95

    result = choose("AAPL", _chain(), Direction.LONG, cfg)
    assert result.chosen.strike == 245, "on a small account premium binds first"


@pytest.mark.parametrize("dte,expected", [(3, None), (10, 230), (30, None)])
def test_expiries_outside_the_window_are_refused(cfg, dte, expected):
    cfg.data["contracts"]["max_contract_price"] = 8.00
    result = choose("AAPL", _chain(dte=dte), Direction.LONG, cfg)
    assert (result.chosen.strike if result.chosen else None) == expected


def test_a_wide_spread_is_refused_however_well_priced(cfg):
    cfg.data["contracts"]["max_contract_price"] = 8.00
    wide = [OptionContract(symbol="AAPL", right=OptionRight.CALL, strike=230,
                           expiry="2026-10-02", dte=10, bid=4.0, ask=5.2,
                           delta=0.52)]
    result = choose("AAPL", wide, Direction.LONG, cfg)

    assert result.chosen is None
    assert any("spread" in k for k in result.rejected), \
        "a 26% spread costs more than the edge is worth"


def test_a_put_signal_never_returns_a_call(cfg):
    cfg.data["contracts"]["max_contract_price"] = 20.00
    calls_only = _chain()
    result = choose("AAPL", calls_only, Direction.SHORT, cfg)

    assert result.chosen is None
    assert result.examined == 0
    assert "No put contracts" in result.note


def test_a_contract_with_no_bid_is_not_tradeable(cfg):
    cfg.data["contracts"]["max_contract_price"] = 20.00
    dead = [OptionContract(symbol="AAPL", right=OptionRight.CALL, strike=230,
                           expiry="2026-10-02", dte=10, bid=0.0, ask=0.0,
                           delta=0.52)]
    result = choose("AAPL", dead, Direction.LONG, cfg)
    assert result.chosen is None
    assert "no two-sided market" in result.rejected


def test_affordable_delta_answers_what_the_budget_does_buy(cfg):
    cfg.data["contracts"]["min_contract_price"] = 0.10
    cfg.data["contracts"]["max_contract_price"] = 1.50

    band = affordable_delta(_chain(), Direction.LONG, cfg)
    assert band is not None
    low, high = band
    assert high < 0.45, "a $100 budget buys out-of-the-money, and says so"


# --------------------------------------------------------------------------- #
# The budget fallback: take the trade at a delta the account can pay for.
# --------------------------------------------------------------------------- #
def _put(strike, delta, mid, dte=21, spread=0.20):
    from panaoptions.models import OptionContract, OptionRight

    return OptionContract(symbol="FTNT", right=OptionRight.PUT, strike=strike,
                          expiry="2026-10-16", dte=dte, bid=mid - spread / 2,
                          ask=mid + spread / 2, delta=-delta)


def _setup():
    class _S:
        delta_band = (0.55, 0.65)
        min_dte_override = 14
        max_dte_override = 30
    return _S()


def test_an_over_budget_band_falls_back_to_the_best_delta_that_fits(cfg):
    """From the desk: FTNT's 0.55-0.65 put 14-30 days out is ~$900; the
    budget is $800. Every such setup fired and nothing was ever bought."""
    from panaoptions.engine import contracts
    from panaoptions.models import Direction

    chain = [_put(180, 0.62, 9.00), _put(175, 0.48, 6.50),
             _put(172, 0.40, 5.10), _put(165, 0.22, 2.40)]
    search = contracts.choose("FTNT", chain, Direction.SHORT, cfg,
                              setup=_setup(), budget=800.0)
    assert search.chosen is not None and search.budget_fallback
    assert abs(search.chosen.delta) == 0.48          # highest delta that fits
    assert "over the $800 budget" in search.note
    assert "0.48 delta" in search.note


def test_the_fallback_never_goes_below_its_delta_floor(cfg):
    from panaoptions.engine import contracts
    from panaoptions.models import Direction

    chain = [_put(180, 0.62, 9.00), _put(165, 0.22, 2.40)]
    search = contracts.choose("FTNT", chain, Direction.SHORT, cfg,
                              setup=_setup(), budget=800.0)
    assert search.chosen is None                     # 0.22 is a lottery ticket


def test_an_in_band_contract_that_fits_is_still_preferred(cfg):
    from panaoptions.engine import contracts
    from panaoptions.models import Direction

    chain = [_put(178, 0.58, 7.50), _put(172, 0.40, 5.10)]
    search = contracts.choose("FTNT", chain, Direction.SHORT, cfg,
                              setup=_setup(), budget=800.0)
    assert abs(search.chosen.delta) == 0.58 and not search.budget_fallback


def test_the_fallback_can_be_switched_off(cfg):
    from panaoptions.engine import contracts
    from panaoptions.models import Direction

    cfg.data["contracts"]["budget_fallback_min_delta"] = 0
    try:
        chain = [_put(180, 0.62, 9.00), _put(175, 0.48, 6.50)]
        search = contracts.choose("FTNT", chain, Direction.SHORT, cfg,
                                  setup=_setup(), budget=800.0)
        assert search.chosen is None
    finally:
        cfg.data["contracts"]["budget_fallback_min_delta"] = 0.30


def test_the_budget_matches_what_sizing_will_accept(cfg):
    """If the picker's budget and the sizer's differ, the picker chooses a
    contract the sizer then refuses — the same dead end one step later."""
    from panaoptions.risk.guardrails import RiskManager

    guard = RiskManager(cfg)
    per_trade = guard.capital * float(cfg.get("risk.max_capital_deployed_pct")) / 100
    assert guard.budget_room() == pytest.approx(per_trade)


def test_start_raises_a_stale_env_capital_to_the_shipped_800_budget(cfg, monkeypatch, tmp_path):
    """.env.example shipped 2000 and .env wins, so the desk ran on a $400
    budget while settings.yaml said $800."""
    import run
    from panaoptions import config as config_mod

    env = tmp_path / ".env"
    env.write_text("PANAOPTIONS_CAPITAL=2000\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "ENV_PATH", env)
    monkeypatch.setenv("PANAOPTIONS_CAPITAL", "2000")
    assert run._ensure_capital() == 0
    assert "PANAOPTIONS_CAPITAL=4000" in env.read_text(encoding="utf-8")

    env.write_text("PANAOPTIONS_CAPITAL=9000\n", encoding="utf-8")
    monkeypatch.setenv("PANAOPTIONS_CAPITAL", "9000")
    run._ensure_capital()
    assert "PANAOPTIONS_CAPITAL=9000" in env.read_text(encoding="utf-8")   # never lowered
