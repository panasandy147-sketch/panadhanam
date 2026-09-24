"""The sentences behind "why did it buy?" and "why did it sell?"."""
from __future__ import annotations

import json

from app.core.explain import why_bought, why_sold

ROW = {
    "side": "BUY", "entry": 2950.0, "stop_loss": 2920.0, "target": 3010.0,
    "risk_reward": 2.0,
    "confirmations": json.dumps(["candlestick: bullish (+0.70)",
                                 "derivatives: bullish (+0.60)"]),
    "rationale": "Weighted vote +0.45 (2 confirming) | Sizing: structural stop "
                 "2 ticks below 2921.50 | trimmed to fit 20% capital",
    "counter_argument": "A close back under VWAP undoes the engulfing bar.",
}


def test_why_bought_names_the_analysts_the_vote_and_the_stop():
    w = why_bought(ROW)
    assert w["headline"] == "2 analysts agreed on a long"
    assert w["confirmations"][0].startswith("Candlestick & technicals")
    assert w["vote"] == "Weighted vote +0.45 (2 confirming)"
    assert w["stop_basis"] == "structural stop 2 ticks below 2921.50"
    assert "2,950.00" in w["plan"] and "2.0R" in w["plan"]
    assert "VWAP" in w["counter_argument"]


def test_why_bought_survives_a_row_with_nothing_in_it():
    w = why_bought({})
    assert w["headline"] == "" and w["plan"] == "" and w["confirmations"] == []


def test_each_exit_says_what_happened():
    assert why_sold({**ROW, "status": "CLOSED_TARGET", "exit_price": 3010.0,
                     "r_multiple": 2.0}).startswith("Target 3,010.00 reached")
    stop = why_sold({**ROW, "status": "CLOSED_STOP", "exit_price": 2920.0,
                     "r_multiple": -1.0})
    assert stop.startswith("Stop 2,920.00 hit") and "-1.00R" in stop
    assert "time stop" in why_sold({**ROW, "status": "CLOSED_TIME",
                                    "exit_price": 2955.0, "r_multiple": 0.17,
                                    "exit_detail": "time_stop"})
    assert "square-off" in why_sold({**ROW, "status": "CLOSED_TIME",
                                     "exit_price": 2955.0,
                                     "exit_detail": "square_off"})
    assert why_sold({**ROW, "status": "OPEN"}).startswith("Still held")
    assert why_sold({**ROW, "status": "REJECTED"}) == "Not bought."
