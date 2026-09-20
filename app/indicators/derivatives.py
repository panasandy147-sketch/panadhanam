"""Option-chain analytics: Black-Scholes Greeks, PCR, Max Pain, OI buildup.

All self-contained (scipy only for the normal CDF) so the F&O brain works even
when the broker's chain endpoint doesn't return Greeks.
"""
from __future__ import annotations

import math
from typing import Any

from app.core.models import OptionChain, OptionLeg

try:
    from scipy.stats import norm
    _N = norm.cdf
    _n = norm.pdf
except ImportError:  # pragma: no cover - scipy is in requirements, this is a guard
    def _N(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _n(x: float) -> float:
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# --------------------------------------------------------------------------- #
# Black-Scholes
# --------------------------------------------------------------------------- #
def black_scholes_greeks(
    spot: float, strike: float, days_to_expiry: float, iv: float,
    rate: float = 0.065, option_type: str = "CE",
) -> dict[str, float]:
    """Greeks for one leg. `iv` is a decimal (0.18 = 18%)."""
    t = max(days_to_expiry, 0.5) / 365.0
    if spot <= 0 or strike <= 0 or iv <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "price": 0.0}

    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv ** 2) * t) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    disc = math.exp(-rate * t)

    gamma = _n(d1) / (spot * iv * sqrt_t)
    vega = spot * _n(d1) * sqrt_t / 100.0     # per 1% IV move

    if option_type.upper() in {"CE", "CALL", "C"}:
        delta = _N(d1)
        price = spot * _N(d1) - strike * disc * _N(d2)
        theta = (-(spot * _n(d1) * iv) / (2 * sqrt_t) - rate * strike * disc * _N(d2)) / 365.0
    else:
        delta = _N(d1) - 1.0
        price = strike * disc * _N(-d2) - spot * _N(-d1)
        theta = (-(spot * _n(d1) * iv) / (2 * sqrt_t) + rate * strike * disc * _N(-d2)) / 365.0

    return {"delta": round(delta, 4), "gamma": round(gamma, 6),
            "theta": round(theta, 4), "vega": round(vega, 4), "price": round(max(price, 0.0), 2)}


def implied_volatility(
    market_price: float, spot: float, strike: float, days_to_expiry: float,
    rate: float = 0.065, option_type: str = "CE", tol: float = 1e-4, max_iter: int = 80,
) -> float:
    """Bisection IV solve — slower than Newton but never diverges on junk quotes."""
    if market_price <= 0 or spot <= 0:
        return 0.0
    lo, hi = 0.001, 5.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        price = black_scholes_greeks(spot, strike, days_to_expiry, mid, rate, option_type)["price"]
        if abs(price - market_price) < tol:
            return round(mid, 4)
        if price > market_price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2, 4)


def enrich_chain(chain: OptionChain, days_to_expiry: float, rate: float = 0.065) -> OptionChain:
    """Fill in missing Greeks/IV so downstream agents always have them."""
    for leg in chain.legs:
        if leg.iv <= 0 and leg.ltp > 0:
            leg.iv = implied_volatility(
                leg.ltp, chain.spot, leg.strike, days_to_expiry, rate, leg.option_type)
        if leg.delta is None and leg.iv > 0:
            g = black_scholes_greeks(
                chain.spot, leg.strike, days_to_expiry, leg.iv, rate, leg.option_type)
            leg.delta, leg.gamma, leg.theta, leg.vega = g["delta"], g["gamma"], g["theta"], g["vega"]
    return chain


# --------------------------------------------------------------------------- #
# Chain-level metrics
# --------------------------------------------------------------------------- #
def put_call_ratio(chain: OptionChain) -> dict[str, float]:
    ce_oi = sum(leg.oi for leg in chain.legs if leg.option_type == "CE")
    pe_oi = sum(leg.oi for leg in chain.legs if leg.option_type == "PE")
    ce_vol = sum(leg.volume for leg in chain.legs if leg.option_type == "CE")
    pe_vol = sum(leg.volume for leg in chain.legs if leg.option_type == "PE")
    return {
        "pcr_oi": round(pe_oi / ce_oi, 3) if ce_oi else 0.0,
        "pcr_volume": round(pe_vol / ce_vol, 3) if ce_vol else 0.0,
        "total_ce_oi": ce_oi,
        "total_pe_oi": pe_oi,
    }


def max_pain(chain: OptionChain) -> float:
    """The strike where option buyers lose the most — price tends to gravitate here."""
    strikes = sorted({leg.strike for leg in chain.legs})
    if not strikes:
        return chain.spot
    best_strike, best_pain = strikes[0], float("inf")
    for expiry_price in strikes:
        pain = 0.0
        for leg in chain.legs:
            if leg.option_type == "CE":
                pain += max(expiry_price - leg.strike, 0.0) * leg.oi
            else:
                pain += max(leg.strike - expiry_price, 0.0) * leg.oi
        if pain < best_pain:
            best_pain, best_strike = pain, expiry_price
    return best_strike


def oi_walls(chain: OptionChain, top: int = 3) -> dict[str, list[dict]]:
    """The heaviest OI strikes: call walls act as resistance, put walls as support."""
    ce = sorted(chain.by_type("CE"), key=lambda leg: leg.oi, reverse=True)[:top]
    pe = sorted(chain.by_type("PE"), key=lambda leg: leg.oi, reverse=True)[:top]
    return {
        "resistance": [{"strike": leg.strike, "oi": leg.oi, "oi_change": leg.oi_change} for leg in ce],
        "support": [{"strike": leg.strike, "oi": leg.oi, "oi_change": leg.oi_change} for leg in pe],
    }


def classify_buildup(price_change_pct: float, oi_change_pct: float,
                     threshold: float = 5.0) -> tuple[str, int, str]:
    """The four-quadrant OI read. Returns (label, direction, explanation)."""
    price_up = price_change_pct > 0
    oi_up = oi_change_pct > threshold
    oi_down = oi_change_pct < -threshold

    if price_up and oi_up:
        return ("LONG_BUILDUP", +1,
                "Price up with OI up — fresh longs entering, trend has fuel")
    if not price_up and oi_up:
        return ("SHORT_BUILDUP", -1,
                "Price down with OI up — fresh shorts entering, pressure continues")
    if price_up and oi_down:
        return ("SHORT_COVERING", +1,
                "Price up with OI down — shorts covering, a rally that can fade once done")
    if not price_up and oi_down:
        return ("LONG_UNWINDING", -1,
                "Price down with OI down — longs exiting, weakness without new shorts")
    return ("NEUTRAL", 0, "No decisive OI shift")


def iv_snapshot(chain: OptionChain) -> dict[str, float]:
    ivs = [leg.iv for leg in chain.legs if leg.iv > 0]
    if not ivs:
        return {"atm_iv": 0.0, "avg_iv": 0.0, "iv_skew": 0.0}
    atm = chain.atm_strike()
    atm_ivs = [leg.iv for leg in chain.legs if abs(leg.strike - atm) < 1e-6 and leg.iv > 0]
    ce_ivs = [leg.iv for leg in chain.legs if leg.option_type == "CE" and leg.iv > 0]
    pe_ivs = [leg.iv for leg in chain.legs if leg.option_type == "PE" and leg.iv > 0]
    skew = (sum(pe_ivs) / len(pe_ivs) - sum(ce_ivs) / len(ce_ivs)) if ce_ivs and pe_ivs else 0.0
    return {
        "atm_iv": round((sum(atm_ivs) / len(atm_ivs)) * 100, 2) if atm_ivs else 0.0,
        "avg_iv": round((sum(ivs) / len(ivs)) * 100, 2),
        "iv_skew": round(skew * 100, 2),   # positive = puts bid = fear
    }


def select_strike(chain: OptionChain, direction: int, moneyness: str = "ATM",
                  step: float | None = None) -> OptionLeg | None:
    """Pick the leg to actually trade given a direction and moneyness preference."""
    if not chain.legs:
        return None
    opt_type = "CE" if direction > 0 else "PE"
    legs = chain.by_type(opt_type)
    if not legs:
        return None
    atm = chain.atm_strike()
    if step is None:
        strikes = sorted({leg.strike for leg in chain.legs})
        step = min((b - a for a, b in zip(strikes, strikes[1:], strict=False)), default=50.0) or 50.0

    if moneyness.upper() == "ATM":
        target = atm
    elif moneyness.upper() == "ITM":
        target = atm - step if direction > 0 else atm + step
    else:  # OTM
        target = atm + step if direction > 0 else atm - step
    return min(legs, key=lambda leg: abs(leg.strike - target))


def analyse(chain: OptionChain, spot_change_pct: float, cfg: dict[str, Any]) -> dict[str, Any]:
    """One call producing the full derivatives picture for an agent."""
    pcr = put_call_ratio(chain)
    ivs = iv_snapshot(chain)
    walls = oi_walls(chain)
    mp = max_pain(chain)

    total_oi_change = sum(leg.oi_change for leg in chain.legs)
    total_oi = sum(leg.oi for leg in chain.legs) or 1.0
    oi_change_pct = total_oi_change / total_oi * 100

    label, direction, explanation = classify_buildup(
        spot_change_pct, oi_change_pct, cfg.get("oi_change_significant_pct", 15.0) / 3)

    pcr_val = pcr["pcr_oi"]
    bull_above = cfg.get("pcr_bullish_above", 1.2)
    bear_below = cfg.get("pcr_bearish_below", 0.7)
    if pcr_val >= bull_above:
        pcr_signal, pcr_dir = f"PCR {pcr_val} — put writers dominant, supportive", +1
    elif 0 < pcr_val <= bear_below:
        pcr_signal, pcr_dir = f"PCR {pcr_val} — call writers dominant, capped", -1
    else:
        pcr_signal, pcr_dir = f"PCR {pcr_val} — balanced", 0

    return {
        **pcr, **ivs,
        "max_pain": mp,
        "max_pain_distance_pct": round((chain.spot - mp) / chain.spot * 100, 2) if chain.spot else 0.0,
        "oi_walls": walls,
        "buildup": label,
        "buildup_direction": direction,
        "buildup_note": explanation,
        "oi_change_pct": round(oi_change_pct, 2),
        "pcr_signal": pcr_signal,
        "pcr_direction": pcr_dir,
        "atm_strike": chain.atm_strike(),
        "spot": chain.spot,
        "expiry": chain.expiry,
    }
