"""Black-Scholes delta.

Yahoo's option chain gives strike, bid, ask, implied volatility and open
interest — but no greeks. Delta is the filter the strategy actually selects on,
so it has to be computed rather than read.

This is the textbook model: European exercise, no dividends. American equity
options and dividend-paying underlyings differ a little, but not by enough to
change which side of a 0.45-0.60 band a contract falls on, which is all it is
used for here.
"""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — no scipy dependency for one function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def delta(spot: float, strike: float, days_to_expiry: float,
          implied_vol: float, is_call: bool,
          risk_free_rate: float = 0.04) -> float:
    """Signed delta: positive for calls, negative for puts.

    Returns 0.0 rather than guessing when an input makes the model undefined —
    a zero IV or an expired contract has no meaningful delta, and inventing one
    would put junk contracts through a filter that exists to keep them out.
    """
    if spot <= 0 or strike <= 0 or implied_vol <= 0 or days_to_expiry <= 0:
        return 0.0

    t = days_to_expiry / 365.0
    vol_t = implied_vol * math.sqrt(t)
    if vol_t <= 0:
        return 0.0

    d1 = (math.log(spot / strike) + (risk_free_rate + implied_vol ** 2 / 2) * t) / vol_t
    return round(_norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0, 4)


def atm_premium_estimate(spot: float, implied_vol: float,
                         days_to_expiry: float) -> float:
    """Roughly what an at-the-money option costs, per share.

    The standard approximation, 0.4 * S * sigma * sqrt(T). Used only by the
    diagnostic that explains why a $100 budget cannot buy an ATM contract, so
    the answer can be given without a live chain in hand.
    """
    if spot <= 0 or implied_vol <= 0 or days_to_expiry <= 0:
        return 0.0
    return round(0.4 * spot * implied_vol * math.sqrt(days_to_expiry / 365.0), 4)
