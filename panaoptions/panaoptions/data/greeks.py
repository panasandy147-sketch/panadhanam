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


def _norm_ppf(p: float) -> float:
    """The inverse normal CDF, Acklam's rational approximation.

    Needed to answer "how far in the money is a 0.70-delta call", which is the
    question behind whether a pattern that asks for one can ever be bought.
    """
    if not 0.0 < p < 1.0:
        return 0.0
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    low, high = 0.02425, 1 - 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) * (p - 0.5)
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def premium_estimate(spot: float, implied_vol: float, days_to_expiry: float,
                     target_delta: float = 0.50) -> float:
    """Roughly what a call at `target_delta` costs, per share.

    An at-the-money estimate is not good enough once patterns start asking for
    their own delta: a 0.70-delta call is a different instrument at a
    different price, and using the 0.50 figure for it would understate the
    cost of exactly the setups most likely to be unaffordable.

    Black-Scholes at zero rate, with the strike backed out of the delta.
    """
    if spot <= 0 or implied_vol <= 0 or days_to_expiry <= 0:
        return 0.0
    target_delta = min(max(abs(target_delta), 0.01), 0.99)
    t = days_to_expiry / 365.0
    vol_t = implied_vol * math.sqrt(t)
    d1 = _norm_ppf(target_delta)
    # d1 = (ln(S/K) + v^2 t / 2) / v_t  =>  K = S * exp(v^2 t / 2 - d1 * v_t)
    strike = spot * math.exp(vol_t * vol_t / 2 - d1 * vol_t)
    d2 = d1 - vol_t
    price = spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return round(max(price, 0.0), 4)
