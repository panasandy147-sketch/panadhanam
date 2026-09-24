"""The desk's rules, written out from the live config.

This is what the Rules pop-up on the dashboard shows. Every number is read
from the configuration the desk is running with, for the market it is
trading, so the page cannot drift from what the desk does. Each rule names
the setting that controls it, which is what to quote when asking for a change.
"""
from __future__ import annotations

from typing import Any


def _rule(text: str, value: Any = None, setting: str = "") -> dict[str, Any]:
    return {"text": text, "value": "" if value is None else str(value),
            "setting": setting}


def _pct(value: Any) -> str:
    try:
        return f"{float(value):g}%"
    except (TypeError, ValueError):
        return "—"


def build(cfg, capital: float | None = None) -> dict[str, Any]:
    g = cfg.get
    cur = getattr(cfg.market, "currency_symbol", "") or ""
    capital = float(capital if capital is not None else g("risk.total_capital", 0) or 0)
    tz = str(g("system.timezone", ""))
    risk_pct = float(g("risk.risk_per_trade_pct", 1.0))
    loss_pct = float(g("risk.max_daily_loss_pct", 3.0))
    weights = g("weights", {}) or {}

    market_file = {"IN": "india", "US": "us"}.get(cfg.active_market,
                                                   cfg.active_market.lower())
    session_key = f"markets/{market_file}.yaml → session."

    def money(value: float) -> str:
        return f"{cur}{value:,.0f}"

    def weight(agent: str) -> str:
        return f"weight {weights.get(agent, '—')}"

    sections: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "How a trade happens",
        "intro": ("Paper trading. Every cycle, each symbol on the watchlist "
                  "goes through the same five steps, and a trade is placed "
                  "only when every step says yes. Times are in the market's "
                  f"own zone ({tz})."),
        "steps": [
            "Five analysts each read the symbol and vote a score from -1 "
            "(strongly bearish) to +1 (strongly bullish).",
            "The chief (CMIO) combines the votes and decides whether there is "
            "enough agreement to act at all.",
            "The risk manager places the stop and target and sizes the "
            "position, then checks it against the account limits.",
            "If the trading day is armed, the order goes to the paper broker. "
            "A paper account arms itself at the open.",
            "The outcome tracker watches the price and sells at the stop, the "
            "target, the time stop or the square-off, whichever comes first.",
        ],
        "rules": [
            _rule("A full cycle runs every", f"{g('system.cycle_seconds', 60)} s",
                  "system.cycle_seconds"),
            _rule("Market opens", g("system.market_open"), session_key + "market_open"),
            _rule("No new trades after", g("system.no_new_entry_after"),
                  session_key + "no_new_entry_after"),
            _rule("Everything still open is sold at", g("system.square_off_time"),
                  session_key + "square_off_time"),
            _rule("Paper account arms itself at the open",
                  "yes" if g("trading_day.auto_arm_on_open", True) else "no",
                  "trading_day.auto_arm_on_open"),
        ],
    })

    # ------------------------------------------------------------------ #
    surge = g("technical.volume_surge_multiplier", 1.8)
    sections.append({
        "title": "The analysts (who votes)",
        "intro": ("An analyst with no data this cycle (no option chain, no "
                  "news in the window) abstains rather than voting zero, so it "
                  "neither helps nor hurts. The weights are how much each vote "
                  "counts in the combined score."),
        "analysts": [
            {"name": "Candlestick & technicals", "weight": weight("candlestick"),
             "reads": [
                 "EMA stack on the primary timeframe: 9 > 21 > 50 is +0.25, "
                 "the reverse is -0.25.",
                 "Price above VWAP is +0.15, below is -0.15.",
                 "Candlestick patterns (engulfing, hammer, shooting star, "
                 "morning/evening star, inside bar, flag…), each adding its "
                 "own weight.",
                 "Higher timeframes agreeing adds up to ±0.2. Disagreement is "
                 "flagged as a conflict.",
                 f"Volume above {surge}x the average confirms the move. Thin "
                 "volume adds nothing.",
                 "RSI overbought pulls a long down (-0.12). Oversold pulls a "
                 "short down.",
                 "Names the structural level that would prove the idea wrong. "
                 "The stop is placed beyond it.",
             ]},
            {"name": "Options & futures", "weight": weight("derivatives"),
             "reads": [
                 "Open-interest build-up is the main read, ±0.35: a fresh long "
                 "or short build-up (short covering counts for less, 0.22).",
                 f"Put/call ratio above {g('derivatives.pcr_bullish_above', 1.2)} "
                 f"is bullish, below {g('derivatives.pcr_bearish_below', 0.7)} "
                 "is bearish (±0.2).",
                 "Pull towards max pain, IV skew and OI walls add small amounts.",
                 "Picks the option leg to trade when the symbol has options: "
                 f"{g('derivatives.preferred_moneyness', 'ATM')}, "
                 f"{g('derivatives.min_days_to_expiry', 1)}–"
                 f"{g('derivatives.max_days_to_expiry', 10)} days to expiry.",
             ]},
            {"name": "News", "weight": weight("news_sentiment"),
             "reads": [
                 "Scores recent headlines for this symbol, newer ones counting more.",
                 "A high-impact headline against the trade is a veto"
                 + (" (on)." if g("consensus.veto_on_high_impact_news", True)
                    else " (currently off)."),
             ]},
            {"name": "Macro & flows", "weight": weight("macro_flow"),
             "reads": [
                 "The session backdrop: global indices and futures, VIX, "
                 "dollar, yields, crude, and FII/DII cash flows in India.",
             ]},
            {"name": "Fundamental filter", "weight": "veto only",
             "reads": [
                 "Never votes a direction. A stock that fails the quality "
                 "screen (for example, debt/equity too high) is vetoed "
                 "whatever the others say. Indices and ETFs skip it.",
             ]},
        ],
    })

    # ------------------------------------------------------------------ #
    conflict = str(g("consensus.conflict_policy", "flat"))
    conflict_text = {
        "flat": "votes pulling in opposite directions damp the score toward zero",
        "side_with_technicals": "the technical analysts win a disagreement",
        "side_with_macro": "the macro analyst wins a disagreement",
    }.get(conflict, conflict)
    sections.append({
        "title": "The vote (when there is enough agreement)",
        "intro": ("This is the gate most setups stop at. Both numbers must be "
                  "met together. The desk ships with a high-risk paper profile: "
                  "one strong analyst is enough."),
        "rules": [
            _rule("Analysts agreeing, at least (each voting ±0.25 or stronger "
                  "in the trade's direction; the fundamental filter does not count)",
                  g("consensus.min_confirmations", 2), "consensus.min_confirmations"),
            _rule("Combined weighted score, at least (±)",
                  g("consensus.min_composite_score", 0.35),
                  "consensus.min_composite_score"),
            _rule("When analysts disagree", conflict_text, "consensus.conflict_policy"),
            _rule("Macro must agree with the direction",
                  "yes" if g("consensus.require_macro_alignment", False) else "no",
                  "consensus.require_macro_alignment"),
            _rule("The language model may change or veto the vote (it still "
                  "writes the journal and the weekly coach either way)",
                  "yes" if g("decisions.use_llm", False) else "no — the rules decide",
                  "decisions.use_llm"),
            _rule("High-impact news against the trade vetoes it",
                  "yes" if g("consensus.veto_on_high_impact_news", True) else "no",
                  "consensus.veto_on_high_impact_news"),
        ],
    })

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "What it buys, and the stop and target",
        "intro": ("The option leg the options analyst picked when the symbol "
                  "has options and a chain is available, otherwise the stock "
                  "itself. An index with no option leg is not traded, because "
                  "an index cannot be bought at spot."),
        "rules": [
            _rule("Stop: beyond the level the candlestick analyst named, by "
                  "this many ticks (a structural stop)",
                  g("risk.structural_stop_ticks", 2), "risk.structural_stop_ticks"),
            _rule("…if that level is on the wrong side, more than 5% away, or "
                  "more than 5 ATR away, the stop is this many ATR instead",
                  f"{g('risk.atr_stop_multiplier', 1.5)} × ATR(14)",
                  "risk.atr_stop_multiplier"),
            _rule("Stop distance must be between",
                  f"{_pct(g('risk.min_stop_distance_pct', 0.15))} and "
                  f"{_pct(g('risk.max_stop_distance_pct', 3.0))} of the price "
                  "(×4 and ×12 for options)",
                  "risk.min_stop_distance_pct / max_stop_distance_pct"),
            _rule("Target: reward at least this many times the risk",
                  f"{g('risk.min_risk_reward', 2.0)} : 1", "risk.min_risk_reward"),
            _rule("…and not more than (implausible targets are refused)",
                  f"{g('risk.max_risk_reward', 10.0)} : 1", "risk.max_risk_reward"),
            _rule("Options: refuse when implied volatility is richer than this "
                  "percentile", _pct(g("risk.reject_if_iv_percentile_above", 85.0)),
                  "risk.reject_if_iv_percentile_above"),
        ],
    })

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "How much it buys",
        "intro": ("Size is set by risk: how much is lost if the stop is hit. "
                  "The other caps can only make a position smaller, never "
                  "larger."),
        "rules": [
            _rule("Account size", money(capital),
                  "risk.total_capital (TOTAL_CAPITAL in .env, or Capital → edit)"),
            _rule("Risked per trade (entry to stop × quantity)",
                  f"{_pct(risk_pct)} = {money(capital * risk_pct / 100)}",
                  "risk.risk_per_trade_pct"),
            _rule("Hard ceiling on risk per trade",
                  _pct(g("risk.max_risk_per_trade_pct", 2.0)),
                  "risk.max_risk_per_trade_pct"),
            _rule("All open positions together, notional at most",
                  f"{_pct(g('risk.max_exposure_pct', 50.0))} of capital × "
                  f"{g('risk.intraday_leverage', 1.0)} leverage",
                  "risk.max_exposure_pct / intraday_leverage"),
            _rule("Option premium held at once, at most",
                  _pct(g("risk.options_max_premium_pct", 25.0)),
                  "risk.options_max_premium_pct"),
            _rule("Open positions at once, at most", g("risk.max_open_positions", 3),
                  "risk.max_open_positions"),
            _rule("Stop taking trades for the day after losing",
                  f"{_pct(loss_pct)} = {money(capital * loss_pct / 100)}",
                  "risk.max_daily_loss_pct"),
        ],
    })

    # ------------------------------------------------------------------ #
    setups = ", ".join(g("risk.time_stop_setups") or ["Mean Reversion"])
    sections.append({
        "title": "When it sells",
        "intro": "Checked every cycle. The first of these to happen closes the trade.",
        "rules": [
            _rule("Stop hit: sold at the stop (a planned loss of 1R)", "−1R",
                  "set per trade, above"),
            _rule("Target hit: sold at the target",
                  f"+{g('risk.min_risk_reward', 2.0)}R or more", "set per trade, above"),
            _rule(f"Time stop, for {setups} setups only: no bounce within",
                  f"{g('risk.time_stop_minutes', 30)} min",
                  "risk.time_stop_minutes / time_stop_setups"),
            _rule("Square-off: anything still open is sold at",
                  g("system.square_off_time"), session_key + "square_off_time"),
        ],
    })

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "After the trade",
        "intro": ("Every closed trade is graded in the journal on process, not "
                  "outcome: a winner that broke a rule is a bad trade, and a "
                  "loser that honoured its stop is a good one. The agents' "
                  "weights are nudged over time by how each analyst's votes "
                  "turned out."),
        "rules": [],
    })

    return {"desk": "panadhanam", "market": cfg.active_market,
            "market_name": getattr(cfg.market, "name", cfg.active_market),
            "sections": sections}
