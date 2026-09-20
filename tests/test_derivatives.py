"""Option maths: Greeks, PCR, max pain, OI buildup."""
from __future__ import annotations

import pytest

from app.core.models import OptionChain, OptionLeg
from app.indicators import derivatives as dv


@pytest.fixture
def chain():
    legs = []
    spot = 24_500.0
    for i in range(-5, 6):
        strike = 24_500 + i * 50
        for opt in ("CE", "PE"):
            legs.append(OptionLeg(
                strike=strike, option_type=opt,
                ltp=max(50 - abs(i) * 5, 5),
                oi=100_000 * (6 - abs(i)),
                oi_change=5_000 * (1 if opt == "PE" else -1),
                volume=10_000, iv=0.15,
            ))
    return OptionChain(underlying="NIFTY", spot=spot, expiry="2025-01-30", legs=legs)


# --------------------------------------------------------------------------- #
# Black-Scholes
# --------------------------------------------------------------------------- #
def test_call_delta_is_between_zero_and_one():
    g = dv.black_scholes_greeks(24_500, 24_500, 7, 0.15, option_type="CE")
    assert 0.0 < g["delta"] < 1.0
    assert g["price"] > 0


def test_put_delta_is_negative():
    g = dv.black_scholes_greeks(24_500, 24_500, 7, 0.15, option_type="PE")
    assert -1.0 < g["delta"] < 0.0


def test_deep_itm_call_approaches_delta_one():
    g = dv.black_scholes_greeks(25_500, 24_000, 7, 0.15, option_type="CE")
    assert g["delta"] > 0.9


def test_theta_is_negative_for_a_long_option():
    g = dv.black_scholes_greeks(24_500, 24_500, 7, 0.15, option_type="CE")
    assert g["theta"] < 0


def test_put_call_parity_holds():
    """C - P should equal S - K*e^(-rt) to within rounding."""
    import math
    spot, strike, dte, iv, rate = 24_500, 24_400, 7, 0.15, 0.065
    c = dv.black_scholes_greeks(spot, strike, dte, iv, rate, "CE")["price"]
    p = dv.black_scholes_greeks(spot, strike, dte, iv, rate, "PE")["price"]
    expected = spot - strike * math.exp(-rate * dte / 365)
    assert (c - p) == pytest.approx(expected, abs=0.5)


def test_implied_volatility_round_trips():
    price = dv.black_scholes_greeks(24_500, 24_500, 7, 0.18, option_type="CE")["price"]
    assert dv.implied_volatility(price, 24_500, 24_500, 7, option_type="CE") == pytest.approx(0.18, abs=0.01)


def test_zero_iv_returns_zeroed_greeks():
    g = dv.black_scholes_greeks(24_500, 24_500, 7, 0.0)
    assert g["delta"] == 0.0


# --------------------------------------------------------------------------- #
# Chain metrics
# --------------------------------------------------------------------------- #
def test_pcr_is_one_for_a_symmetric_chain(chain):
    assert dv.put_call_ratio(chain)["pcr_oi"] == pytest.approx(1.0)


def test_max_pain_lands_at_the_heaviest_oi_strike(chain):
    assert dv.max_pain(chain) == pytest.approx(24_500, abs=50)


def test_atm_strike_is_nearest_to_spot(chain):
    assert chain.atm_strike() == 24_500


def test_oi_walls_are_sorted_by_size(chain):
    walls = dv.oi_walls(chain)
    ois = [w["oi"] for w in walls["resistance"]]
    assert ois == sorted(ois, reverse=True)


@pytest.mark.parametrize("price_ch,oi_ch,expected", [
    (1.5, 20.0, "LONG_BUILDUP"),
    (-1.5, 20.0, "SHORT_BUILDUP"),
    (1.5, -20.0, "SHORT_COVERING"),
    (-1.5, -20.0, "LONG_UNWINDING"),
    (0.5, 1.0, "NEUTRAL"),
])
def test_oi_buildup_quadrants(price_ch, oi_ch, expected):
    label, _, _ = dv.classify_buildup(price_ch, oi_ch, threshold=5.0)
    assert label == expected


def test_strike_selection_respects_moneyness(chain):
    atm = dv.select_strike(chain, direction=1, moneyness="ATM")
    itm = dv.select_strike(chain, direction=1, moneyness="ITM")
    otm = dv.select_strike(chain, direction=1, moneyness="OTM")
    assert atm.option_type == "CE"
    assert itm.strike < atm.strike < otm.strike   # for a call


def test_bearish_direction_picks_a_put(chain):
    assert dv.select_strike(chain, direction=-1).option_type == "PE"


def test_analyse_produces_the_full_picture(chain):
    out = dv.analyse(chain, spot_change_pct=1.2, cfg={"pcr_bullish_above": 1.2,
                                                     "pcr_bearish_below": 0.7,
                                                     "oi_change_significant_pct": 15.0})
    for key in ("pcr_oi", "max_pain", "atm_iv", "buildup", "oi_walls", "atm_strike"):
        assert key in out


def test_empty_chain_does_not_crash():
    empty = OptionChain(underlying="X", spot=100.0, expiry="2025-01-30", legs=[])
    assert dv.max_pain(empty) == 100.0
    assert dv.put_call_ratio(empty)["pcr_oi"] == 0.0
    assert dv.select_strike(empty, 1) is None
