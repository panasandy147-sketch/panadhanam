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


def test_an_at_the_money_contract_cannot_fit_a_hundred_dollar_budget(cfg):
    # This is the headline problem with the shipped configuration, and the
    # filter must fail LOUDLY rather than return an empty list in silence.
    result = choose("AAPL", _chain(), Direction.LONG, cfg)

    assert result.chosen is None
    assert result.closest_by_price is not None
    assert "cannot both hold" in result.note
    assert "$460" in result.note, "the note must name the real cost"


def test_the_rejection_reasons_are_counted_not_discarded(cfg):
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
