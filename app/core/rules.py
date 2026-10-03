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

    # The watchlist, band by band — which names, and which bands are on.
    active = {str(b).upper() for b in (cfg.universe.get("active_bands") or [])}
    everything = [dict(i) for i in (cfg.universe.get("indices") or [])] + \
                 [dict(i) for i in (cfg.universe.get("stocks") or [])]
    band_rules = []
    for band in sorted({str(i.get("band", "")).upper() for i in everything} - {""}):
        names = [i["symbol"] for i in everything
                 if str(i.get("band", "")).upper() == band]
        on = not active or band in active
        band_rules.append(_rule(
            f"Band {band} ({len(names)}): " + ", ".join(names),
            "scanned" if on else "off",
            f"markets/{market_file}.yaml → universe.active_bands"))

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
            *band_rules,
            _rule("Of those, only the top of each band is watched and traded, "
                  "ranked by the desk's own vote and re-ranked every "
                  f"{g('focus.rerank_minutes', 15)} min",
                  f"top {g('focus.per_band', 5)} per band"
                  if g("focus.enabled", True) else "off — all scanned",
                  "focus.per_band / focus.rerank_minutes"),
            _rule("Market opens", g("system.market_open"), session_key + "market_open"),
            _rule("No new trades after", g("system.no_new_entry_after"),
                  session_key + "no_new_entry_after"),
            _rule("Everything still open is sold at", g("system.square_off_time"),
                  session_key + "square_off_time"),
            _rule("A general setup (no named strategy) needs this many analysts "
                  "voting its way at ±" f"{g('consensus.general_agree_min', 0.25)}",
                  g("consensus.general_min_analysts", 0) or "off",
                  "consensus.general_min_analysts"),
            _rule("A general setup: no short below this RSI, no long above",
                  (f"{(g('risk.rsi_guard') or {}).get('short_min', 25)} / "
                   f"{(g('risk.rsi_guard') or {}).get('long_max', 75)}")
                  if (g("risk.rsi_guard") or {}).get("enabled") else "off",
                  "risk.rsi_guard"),
            _rule("No new general setup after (named strategies keep their own "
                  "windows)", g("screener.windows.general_to") or "off",
                  "screener.windows.general_to"),
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
                 "morning/evening star, tweezer bottom/top, double rejection "
                 "bottom/top, inside bar, flag…), each adding its own weight. "
                 "A double rejection is the same high or low rejected twice, "
                 "2–8 bars apart, with a real pullback between.",
                 "Higher timeframes agreeing adds up to ±0.2. Disagreement is "
                 "flagged as a conflict.",
                 f"Volume above {surge}x the average confirms the move. Thin "
                 "volume adds nothing.",
                 "RSI overbought pulls a long down (-0.12). Oversold pulls a "
                 "short down.",
                 "Names the structural level that would prove the idea wrong. "
                 "The stop is placed beyond it.",
             ]},
            {"name": "Volume profile", "weight": weight("volume_profile"),
             "reads": [
                 "Builds each session's volume by price (regular hours): the POC, "
                 f"the {float(g('volume_profile.value_area_pct', 0.70)):.0%} value "
                 "area (VAL–VAH), and low/high volume nodes, for the prior and "
                 "the current session.",
                 "Value Area Rejection: a failed poke above the VAH closing back "
                 "inside (put), or a held VAL test with a bullish candle (call); "
                 "target the POC.",
                 "LVN Pocket Acceleration: a close from a shelf into a volume "
                 f"pocket on RVOL ≥ {g('volume_profile.lvn_min_rvol', 1.5)}x — "
                 "price crosses thin volume fast; target the pocket's far edge.",
                 "POC Bounce: back to the POC after a "
                 f"{g('volume_profile.poc_away_atr', 1.0)}-ATR move away, rejected "
                 "there.",
                 "Can lead a trade, and names its own structural stop.",
                 f"On every trade: an entry at a VAL/POC (long) or VAH/POC (short) "
                 f"adds {g('volume_profile.alignment_boost', 0.30)} to the "
                 f"composite; a thick volume node straight ahead subtracts "
                 f"{g('volume_profile.hvn_penalty', 0.30)} within "
                 f"{g('volume_profile.hvn_near_atr', 1.0)} ATR and vetoes within "
                 f"{g('volume_profile.hvn_veto_atr', 0.25)} ATR.",
             ]},
            {"name": "Options & futures", "weight": weight("derivatives"),
             "reads": [
                 "Abstains when the option chain is simulated (no feed served "
                 "one), instead of voting on invented open interest.",
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
                 f"News polarity beyond ±{g('consensus.news_veto_polarity', 0.60)} "
                 "against the trade is an absolute veto — nothing overrides it"
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
        "technicals_unless_derivatives_oppose": (
            "the chart wins against news or macro, but never trades against the "
            "options & futures read; with no real chain the chart may trade alone"),
        "flat": "votes pulling in opposite directions damp the score toward zero",
        "side_with_technicals": "the technical analysts win a disagreement",
        "side_with_macro": "the macro analyst wins a disagreement",
    }.get(conflict, conflict)
    sections.append({
        "title": "The vote (when there is enough agreement)",
        "intro": ("This is the gate most setups stop at. Both numbers must be "
                  "met together. The paper profile is active on entries (one "
                  "strong price-based analyst is enough) and strict on risk."),
        "rules": [
            _rule("Analysts agreeing, at least (each voting ±0.25 or stronger "
                  "in the trade's direction; the fundamental filter does not count)",
                  g("consensus.min_confirmations", 2), "consensus.min_confirmations"),
            _rule("Combined weighted score, at least (±)",
                  g("consensus.min_composite_score", 0.35),
                  "consensus.min_composite_score"),
            _rule("When analysts disagree", conflict_text, "consensus.conflict_policy"),
            _rule("An analyst scoring under this has nothing to say, and "
                  + ("still counts as a zero in the average"
                     if g("consensus.silent_analysts_dilute", True)
                     else "is left out of the average rather than watering it down"),
                  g("consensus.silent_below", 0.10),
                  "consensus.silent_analysts_dilute / silent_below"),
            _rule("No counter-trend trades: no long below VWAP or into a falling "
                  "15-minute trend, no short above VWAP or into a rising one"
                  + (" — except a volume-profile setup in the trade's direction "
                     "(a call at the VAL is counter to the move by design)"
                     if g("consensus.trend_filter_exempt_volume_profile", True) else ""),
                  "on" if g("consensus.trend_filter", False) else "off",
                  "consensus.trend_filter"),
            _rule("At least one of these must agree (news or macro alone never trades)",
                  ", ".join(g("consensus.lead_analysts") or []) or "any",
                  "consensus.lead_analysts"),
            _rule("2-analyst quorum: the "
                  f"{g('consensus.quorum.primary', 'candlestick')} trigger at "
                  f"±{g('consensus.quorum.primary_min', 0.35)} or stronger AND one of "
                  + ", ".join(g("consensus.quorum.confirmers") or
                              ["volume_profile", "derivatives", "macro_flow", "news_sentiment"])
                  + f" at ±{g('consensus.quorum.confirm_min', 0.25)} the same way",
                  "on" if g("consensus.quorum.enabled", False) else
                  "off (backtest: it would have left 5 of 4,085 India and 0 of 3,687 US "
                  "setups)", "consensus.quorum"),
            _rule("Macro must agree with the direction",
                  "yes" if g("consensus.require_macro_alignment", False) else "no",
                  "consensus.require_macro_alignment"),
            _rule("The language model may change or veto the vote (it still "
                  "writes the journal and the weekly coach either way)",
                  "yes" if g("decisions.use_llm", False) else "no — the rules decide",
                  "decisions.use_llm"),
            _rule("News blackout: no new entries this many minutes before and "
                  "after a macro release (FOMC and other scheduled events, or a "
                  "fresh headline naming one)",
                  (f"{g('news_blackout.minutes_before', 15)} / "
                   f"{g('news_blackout.minutes_after', 15)} min")
                  if g("news_blackout.enabled", True) else "off",
                  "news_blackout.events / headline_keywords"),
            _rule("High-impact news against the trade vetoes it",
                  "yes" if g("consensus.veto_on_high_impact_news", True) else "no",
                  "consensus.veto_on_high_impact_news"),
        ],
    })

    # ------------------------------------------------------------------ #
    if g("pd_sweep.enabled", True):
        sections.append({
            "title": "Strategy: Previous Day Liquidity Sweep (failed breakout)",
            "intro": ("Stops rest just beyond yesterday's high and low. When price "
                      "runs them and fails, the breakout traders are trapped and "
                      "their exit fuels the move back. US and India, intraday."),
            "rules": [
                _rule("The trigger: the latest candle pierces the previous-day high "
                      "(PDH) or low (PDL) and CLOSES BACK INSIDE yesterday's range, on",
                      " or ".join(g("pd_sweep.timeframes", ["5m", "15m"]) or []),
                      "pd_sweep.timeframes"),
                _rule("PDH sweep → SHORT (a put) only if the sweep candle is a "
                      "Shooting Star or a Bearish Engulfing", "confirmation",
                      "pd_sweep.enabled"),
                _rule("PDL sweep → LONG (a call) only if it is a Hammer or a "
                      "Bullish Engulfing", "confirmation", "pd_sweep.enabled"),
                _rule("The candlestick analyst's score for a confirmed sweep",
                      g("pd_sweep.score", 1.0), "pd_sweep.score"),
                _rule("Stop: exactly this many ticks beyond the sweep candle's wick "
                      "(₹0.05 ticks on NSE, $0.01 in the US) — no ATR widening",
                      g("pd_sweep.stop_ticks", 2), "pd_sweep.stop_ticks"),
                _rule("Target: the day's VWAP, or this many R — whichever is further",
                      f"{g('pd_sweep.min_reward_risk', 3.0)}R",
                      "pd_sweep.min_reward_risk"),
                _rule("No time stop: it runs to the target, the stop, or the "
                      "intraday square-off",
                      g("system.square_off_time", "15:15"),
                      "risk.no_time_stop_setups"),
                _rule("The trend filter stands aside for it (a PDH sweep short sits "
                      "above VWAP by design)",
                      "on" if g("consensus.trend_filter_exempt_pd_sweep", True) else "off",
                      "consensus.trend_filter_exempt_pd_sweep"),
                _rule("Also needs rising call (PDL) / put (PDH) open interest",
                      "yes" if g("pd_sweep.require_rising_oi", False) else "no",
                      "pd_sweep.require_rising_oi"),
            ],
        })

    # ------------------------------------------------------------------ #
    gold = [i["symbol"] for i in cfg.watchlist() if i.get("swing")]
    if gold:
        sections.append({
            "title": "The gold desk (held 1-4 days): " + ", ".join(gold),
            "intro": ("Gold is traded on its own rules: the shares (not options), "
                      "held overnight, on Larry Williams' volatility breakout — the "
                      "strongest result of every test (two years of hourly bars and "
                      "the last 60 days of 5-minute bars all positive). No morning "
                      "screener and no other strategy on it."),
            "rules": [
                _rule("Entry: the first 5m bar today whose high reaches today's open + "
                      "this share of yesterday's range (a long; a short: the low "
                      "reaches the open minus it)", f"{g('swing.k', 0.5)} x range",
                      "swing.k"),
                _rule("Only with the trend: yesterday's close against the close this "
                      "many sessions before", f"{g('swing.trend_days', 20)} days",
                      "swing.trend_days"),
                _rule("Stop: 2 ticks beyond today's open; target",
                      f"{g('risk.min_risk_reward', 3.0)}R", "risk.min_risk_reward"),
                _rule("Exit: the first later session that is in profit at the open "
                      "(Williams' bail-out)",
                      "on" if g("swing.first_profitable_open", True) else "off",
                      "swing.first_profitable_open"),
                _rule("…else the square-off of this session (the entry day is 0)",
                      g("swing.max_hold_days", 4), "swing.max_hold_days"),
            ],
        })

    # ------------------------------------------------------------------ #
    if g("vol_breakout.enabled", False):
        sections.append({
            "title": "Strategy: Volatility Breakout (Larry Williams, 1987 World Cup)",
            "intro": ("Yesterday's range says how far price can travel today. Once "
                      "it has moved a set share of that range away from today's open, "
                      "the day's expansion is under way."),
            "rules": [
                _rule("The trigger: the FIRST closed 5m candle beyond today's open "
                      "plus (long) or minus (short) this share of yesterday's range — "
                      "a level crossed earlier is a chase, not taken",
                      f"{g('vol_breakout.k', 0.5)} x range", "vol_breakout.k"),
                _rule("Price on the same side of VWAP, the 9 EMA over the 21 for a long "
                      "(under for a short)", "required", "vol_breakout.enabled"),
                _rule("Entries from this long after the open, until",
                      f"{g('vol_breakout.minutes_after_open', 30)} min – "
                      f"{g('vol_breakout.to', '14:30')}",
                      "vol_breakout.minutes_after_open / vol_breakout.to"),
                _rule("The candlestick analyst's score for it",
                      g("vol_breakout.score", 0.9), "vol_breakout.score"),
                _rule("Stop: exactly this many ticks beyond today's open — back there "
                      "and the expansion has failed", g("vol_breakout.stop_ticks", 2),
                      "vol_breakout.stop_ticks"),
                _rule("Target, with no room check against the previous-day high/low "
                      "and no time stop", f"{g('risk.min_risk_reward', 3.0)}R",
                      "risk.min_risk_reward"),
            ],
        })

    if g("sjk1.enabled", False):
        sections.append({
            "title": "Strategy: SJK 1 — 50 / 200 EMA pullback continuation (the user's)",
            "intro": ("A trial from 2 Oct 2026. Trade WITH the 200 EMA trend, after a "
                      "pullback to the 50 EMA, on the break of the swing made before "
                      "the pullback. One trade per swing point."),
            "rules": [
                _rule("Long: price above the slow EMA; short: below it",
                      f"{g('sjk1.slow', 200)} EMA (5m close)", "sjk1.slow"),
                _rule("The pullback's low (long) / high (short) comes within this much "
                      "of the fast EMA, or through it",
                      f"{g('sjk1.touch_pct', 0.15)}% of the {g('sjk1.fast', 50)} EMA",
                      "sjk1.touch_pct / sjk1.fast"),
                _rule("...without a single 5m close beyond the slow EMA", "required",
                      "sjk1.slow"),
                _rule("The trigger: the FIRST 5m close through the swing high (long) / "
                      "low (short) made before the pullback — once per swing",
                      "first close only", "sjk1.swing_lookback"),
                _rule("A swing high / low: beyond this many bars on each side",
                      g("sjk1.swing_lookback", 3), "sjk1.swing_lookback"),
                _rule("The pullback's swing at most this many 5m bars old",
                      g("sjk1.max_age_bars", 36), "sjk1.max_age_bars"),
                _rule("Entries between (market time)",
                      f"{g('sjk1.from', '09:45')} – {g('sjk1.to', '15:00')}",
                      "sjk1.from / sjk1.to"),
                _rule("Stop: AT the pullback's swing low (long) / high (short)",
                      f"{g('sjk1.stop_ticks', 0)} tick(s) beyond", "sjk1.stop_ticks"),
                _rule("Target, sold there — and the reward:risk it is judged at "
                      "(not the desk's 1:3)", f"1:{g('sjk1.rr', 2.5)}", "sjk1.rr"),
                _rule("Stop to breakeven at this R (0 = off), then trailed this far "
                      "behind the best R (0 = off)",
                      f"{g('sjk1.breakeven_r', 0)}R / {g('sjk1.trail_r', 0)}R",
                      "sjk1.breakeven_r / sjk1.trail_r"),
                _rule("The candlestick analyst's score for it",
                      g("sjk1.score", 0.9), "sjk1.score"),
            ],
        })

    if g("sjk_9_15_21.enabled", False):
        k = "sjk_9_15_21."
        sections.append({
            "title": "Strategy: SJK 9-15-21 — the 9 / 15 / 21 EMA fan (the user's)",
            "intro": ("A trial from 5 Oct 2026. Trade the fan-out of three EMAs — 9 "
                      "(purple), 15 (blue), 21 (dark grey) on the chart — when they are "
                      "in order, separated and trending; stand aside when they are "
                      "Sideways / Choppy. One trade per continuous alignment."),
            "rules": [
                _rule("Long: 9 EMA > 15 EMA > 21 EMA; short: 21 > 15 > 9 (5m close)",
                      f"{g(k + 'fast', 9)} / {g(k + 'mid', 15)} / {g(k + 'slow', 21)}",
                      "sjk_9_15_21.fast / .mid / .slow"),
                _rule("Chop filter: each EMA gap at least", f"{g(k + 'min_sep_pct', 0.02)}% "
                      "of the price", "sjk_9_15_21.min_sep_pct"),
                _rule("Chop filter: the EMA order changed at most this often in the "
                      "last N bars", f"{g(k + 'max_flips', 2)} in {g(k + 'chop_bars', 12)}",
                      "sjk_9_15_21.max_flips / .chop_bars"),
                _rule("Chop filter: the 21 EMA moved at least this much the trade's way "
                      "over those bars", f"{g(k + 'min_slope_pct', 0.05)}%",
                      "sjk_9_15_21.min_slope_pct"),
                _rule("Entry: the close of the candle where the fan-out confirms, or a "
                      "pullback that touches the 9/15 band, holds the 21 and closes back "
                      "beyond the 9", g(k + "entry_mode", "both"), "sjk_9_15_21.entry_mode"),
                _rule("Entries between (market time)",
                      f"{g(k + 'from', '09:45')} – {g(k + 'to', '15:45')}",
                      "sjk_9_15_21.from / .to"),
                _rule("Stop: the recent swing low (long) / high (short), else the 21 EMA",
                      f"{g(k + 'stop_mode', 'swing')}; swing = {g(k + 'swing_lookback', 3)} "
                      f"bars each side, at most {g(k + 'max_age_bars', 24)} bars old",
                      "sjk_9_15_21.stop_mode / .swing_lookback"),
                _rule("Target, sold there — and the reward:risk it is judged at "
                      "(not the desk's 1:3)", f"1:{g(k + 'rr', 2.0)}", "sjk_9_15_21.rr"),
                _rule("Stop to breakeven at this R (0 = off), then trailed this far "
                      "behind the best R (0 = off)",
                      f"{g(k + 'breakeven_r', 0)}R / {g(k + 'trail_r', 0)}R",
                      "sjk_9_15_21.breakeven_r / .trail_r"),
                _rule("The candlestick analyst's score for it", g(k + "score", 0.9),
                      "sjk_9_15_21.score"),
            ],
        })

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "Previous-day F&O confluence",
        "intro": ("Each symbol's previous session is mapped every day: high "
                  "(PDH), low (PDL), close (PDC), call and put open interest and "
                  "their change since the previous close, and the build-up — "
                  "Long Buildup (price up, OI up), Short Buildup (down, up), "
                  "Short Covering (up, down), Long Unwinding (down, down). It is "
                  "stored for the review."),
        "rules": [
            _rule("A trade driven by a reversal pattern is taken only at the "
                  "previous day's level: bullish after a sweep-and-reject of the "
                  "PDL with call OI rising; bearish after a test-and-reject of "
                  "the PDH with put OI rising",
                  "on" if g("fno_confluence.enabled", False) else "off",
                  "fno_confluence.enabled"),
            *([_rule("By regime — rangebound (and volatile): the reversal's extreme "
                     "within this of the PDL (longs) / PDH (shorts), closed back inside, "
                     "with the OI rule below",
                     f"{g('fno_confluence.rangebound_proximity_pct', 0.25)}%, in the last "
                     f"{g('fno_confluence.lookback_bars', 3)} bars",
                     "fno_confluence.regime_rules / rangebound_proximity_pct"),
               _rule("By regime — trending_up longs / trending_down shorts: no PDH/PDL "
                     "needed; the pattern must form on a pullback within this of the "
                     "intraday VWAP, the session POC or the 9/20 EMA, closing back on the "
                     "trend side (no OI condition). Against the trend: the rangebound rule",
                     f"{g('fno_confluence.trend_pullback_pct', 0.30)}%",
                     "fno_confluence.trend_pullback_pct")]
              if g("fno_confluence.regime_rules", False) else
              [_rule("'At' the PDH / PDL means within",
                     f"{g('fno_confluence.touch_atr', 0.15)} × ATR, in the last "
                     f"{g('fno_confluence.lookback_bars', 3)} bars",
                     "fno_confluence.touch_atr / lookback_bars")]),
            _rule("When the chain has no real open interest",
                  g("fno_confluence.when_oi_unknown", "block"),
                  "fno_confluence.when_oi_unknown"),
            _rule("The target needs open road: refused when the previous-day "
                  "high (long) or low (short) sits inside the 1:"
                  f"{g('risk.min_risk_reward', 3.0)} target",
                  "on" if g("fno_confluence.room_check", True) else "off",
                  "fno_confluence.room_check"),
            _rule("Target snapping: with less than 1:"
                  f"{g('risk.min_risk_reward', 3.0)} of room but at least this much, the "
                  f"trade is approved and the target snapped "
                  f"{g('risk.target_snap.ticks', 2)} ticks inside the PDH (long) / PDL "
                  f"(short); less room is refused",
                  (f"{g('risk.target_snap.min_r', 2.2)}R" if g("risk.target_snap.enabled", False)
                   else "off"), "risk.target_snap"),
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
            _rule("…and never closer than the larger of these (a level inside a "
                  "normal bar's range is hit by noise alone); the target is 3R from "
                  "the widened stop and the size shrinks to keep the 1% risk",
                  f"{g('risk.min_stop_atr', 0)} × ATR or "
                  f"{g('risk.min_stop_pct', 0)}% of the price",
                  "risk.min_stop_atr / risk.min_stop_pct"),
            _rule("Stop distance must be between",
                  f"{_pct(g('risk.min_stop_distance_pct', 0.15))} and "
                  f"{_pct(g('risk.max_stop_distance_pct', 3.0))} of the price "
                  "(×4 and ×12 for options)",
                  "risk.min_stop_distance_pct / max_stop_distance_pct"),
            _rule("Target: reward at least this many times the risk",
                  f"{g('risk.min_risk_reward', 2.0)} : 1", "risk.min_risk_reward"),
            _rule("…and not more than (implausible targets are refused)",
                  f"{g('risk.max_risk_reward', 10.0)} : 1", "risk.max_risk_reward"),
            _rule("Options: expiry between (0–2 day options are never bought — "
                  "intraday theta and an IV crush eat them)",
                  f"{g('derivatives.min_days_to_expiry', 3)}–"
                  f"{g('derivatives.max_days_to_expiry', 7)} days",
                  "derivatives.min_days_to_expiry / max_days_to_expiry"),
            _rule("Options: refuse when IV rank (today's IV in this symbol's own "
                  "past-year range) is above — until "
                  f"{g('risk.iv_rank_min_samples', 20)} days of IV are recorded, "
                  f"when IV is over {g('risk.iv_over_realised_max', 1.5)}x the "
                  "stock's realised volatility",
                  g("risk.reject_if_iv_rank_above", 80.0),
                  "risk.reject_if_iv_rank_above"),
        ],
    })

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "How much it buys",
        "intro": ("Size is set by risk: how much is lost if the stop is hit. "
                  "The other caps can only make a position smaller, never "
                  "larger."),
        "rules": [
            _rule("Account size — each market has its own (US in dollars, India "
                  "in rupees)", money(capital),
                  f"TOTAL_CAPITAL_{getattr(cfg, 'active_market', 'US')} in .env, "
                  "or Capital → edit; India's default is in config/markets/india.yaml"),
            _rule("Quantity = capital × risk % ÷ (entry − stop), then trimmed "
                  "by the caps below", "by the stop, not fixed",
                  "sizing formula (uses risk.risk_per_trade_pct)"),
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
            _rule("Each position, notional at most (so one tight stop cannot "
                  "crowd out the rest)",
                  "total ÷ max positions" if g("risk.split_exposure_across_positions", True)
                  else "no per-position cap",
                  "risk.split_exposure_across_positions"),
            _rule("Option premium per trade, at most (capital deployment cap)",
                  f"{_pct(g('risk.max_capital_deployed_pct', 20.0))} = "
                  f"{money(capital * float(g('risk.max_capital_deployed_pct', 20.0)) / 100)}",
                  "risk.max_capital_deployed_pct"),
            _rule("Index flex: high-notional index ETFs ("
                  + ", ".join(g("risk.index_symbols") or ["SPY", "QQQ", "DIA"])
                  + ") may deploy up to",
                  f"{_pct(g('risk.index_max_capital_deployed_pct', 25.0))} = "
                  f"{money(capital * float(g('risk.index_max_capital_deployed_pct', 25.0)) / 100)}",
                  "risk.index_max_capital_deployed_pct"),
            _rule("Option bid-ask spread, at most",
                  f"{_pct(g('risk.max_spread_pct_of_mid', 7.0))} of the mid",
                  "risk.max_spread_pct_of_mid"),
            _rule("Option stop: on the UNDERLYING — 5m swing low/high over the "
                  f"last {g('risk.swing_lookback_bars', 6)} bars ± "
                  f"{g('risk.structural_stop_ticks', 2)} ticks, else "
                  f"{g('risk.atr_stop_multiplier', 1.5)}x ATR; never the premium",
                  "underlying", "risk.swing_lookback_bars / atr_stop_multiplier"),
            _rule("Open positions at once, at most", g("risk.max_open_positions", 3),
                  "risk.max_open_positions"),
            _rule("Trades a day, at most (the over-trading throttle)",
                  g("risk.max_daily_trades") or "no limit", "risk.max_daily_trades"),
            _rule("Anti-stacking: one position per stock; a stock that closes a "
                  "trade goes on the cooldown blacklist for",
                  f"{'on' if g('risk.one_position_per_symbol', True) else 'off'}, "
                  f"{g('risk.reentry_cooldown_minutes', 0)} min",
                  "risk.one_position_per_symbol / reentry_cooldown_minutes"),
            _rule("A held symbol is not re-scanned for a new entry: the position "
                  "manager runs its stop, target and pyramid adds. 'One position per "
                  "symbol' blocks BASE entries only — pyramid adds are allowed",
                  "on" if g("system.skip_held_symbols", True) else "off",
                  "system.skip_held_symbols"),
            _rule("Portfolio heat: total lost if EVERY open position hit its stop "
                  "together, at most (a new trade takes what room is left)",
                  (f"{_pct(g('risk.max_portfolio_heat_pct'))} = "
                   f"{money(capital * float(g('risk.max_portfolio_heat_pct')) / 100)}")
                  if g("risk.max_portfolio_heat_pct") else "no cap",
                  "risk.max_portfolio_heat_pct"),
            _rule("Daily circuit breaker: at this loss (closed + open) every "
                  "position is closed at market and the desk is LOCKED OUT for the "
                  "rest of the calendar day — a restart does not clear it"
                  if g("risk.circuit_breaker", True)
                  else "Stop taking trades for the day after losing",
                  f"{_pct(loss_pct)} = {money(capital * loss_pct / 100)}",
                  "risk.max_daily_loss_pct"),
        ],
    })

    # ------------------------------------------------------------------ #
    if g("screener.enabled", False):
        a, b = g("screener.band_a") or {}, g("screener.band_b") or {}
        w = g("screener.windows") or {}
        sections.append({
            "title": "Today's watchlist: the pre-market screener (Band A / B)",
            "intro": ("Before the open, from YESTERDAY's daily bars across the index "
                      "universe: ATR% (14-day ATR / close), RVOL (yesterday's volume "
                      "/ 20-day average), NR7, inside day, and where it closed in its "
                      "range. Only the names it picks are traded that day — nothing "
                      "off the list, and no list means no entries."),
            "rules": [
                _rule("Runs at (market clock)", g("screener.run_at", "09:00"),
                      "screener.run_at"),
                _rule(f"Band A (top {a.get('size', 5)}): ATR% ≥ {a.get('min_atr_pct', 2.0)}, "
                      f"RVOL ≥ {a.get('min_rvol', 1.2)}, and NR7 or an inside day; "
                      f"ranked by RVOL", "both sides", "screener.band_a"),
                _rule(f"Band B (next {b.get('size', 5)}): ATR% ≥ {b.get('min_atr_pct', 2.0)}, "
                      f"RVOL ≥ {b.get('min_rvol', 1.5)}, and a close in the top "
                      f"{1 - float(b.get('long_close_location', 0.8)):.0%} of the range "
                      f"(longs only) or the bottom {float(b.get('short_close_location', 0.2)):.0%}"
                      f" (shorts only)", "one side", "screener.band_b"),
                _rule("Morning window: Band A entries only",
                      f"{w.get('morning_from')}–{w.get('morning_to')}", "screener.windows"),
                _rule("Band B promotion: a Band B name may enter in the morning window "
                      "when its regime trends its way (trending_up long / trending_down "
                      "short) and its composite is at least ± this",
                      (f"{g('screener.band_b_promotion.min_composite', 0.85)}"
                       if g("screener.band_b_promotion.enabled", False) else "off"),
                      "screener.band_b_promotion"),
                _rule("Midday freeze: no new entries (the chop filter)",
                      f"{w.get('morning_to')}–{w.get('afternoon_from')}", "screener.windows"),
                _rule("Afternoon window: Band A/B VWAP pullbacks only (trend side, "
                      f"within {g('screener.vwap_pullback_atr', 0.5)} ATR of VWAP)",
                      f"{w.get('afternoon_from')}–{w.get('afternoon_to')}",
                      "screener.windows / vwap_pullback_atr"),
                _rule("Trades a day (open or closed), then no new signals",
                      g("risk.max_daily_trades") or "no limit", "risk.max_daily_trades"),
                _rule("Everything still open is squared off at",
                      g("system.square_off_time"), session_key + "square_off_time"),
            ],
        })

    # ------------------------------------------------------------------ #
    if g("risk.pyramid.enabled", False):
        steps = g("risk.pyramid.levels") or []
        rows = [_rule("No averaging down: any add, or any new order on a symbol "
                      "already held, is refused while that position is in a drawdown",
                      "always", "risk desk (app/agents/risk.py)")]
        for i, step in enumerate(steps, 1):
            stop = ("the base entry price" if str(step.get("stop")) == "base"
                    else "the Level 1 fill price")
            rows.append(_rule(
                f"Level {i}: at +{float(step.get('at_r', i)):g}R open profit, ADD "
                f"{float(step.get('size', 0)):.0%} of the base quantity, and move the "
                f"stop for the WHOLE position to {stop}",
                f"+{float(step.get('at_r', i)):g}R → +{float(step.get('size', 0)):.0%}",
                "risk.pyramid.levels"))
        rows.append(_rule("One take-profit for the whole (175%) position, from the "
                          "BASE entry; the average entry is re-blended after each add",
                          f"+{g('risk.pyramid.target_r', 3.0):g}R", "risk.pyramid.target_r"))
        sections.append({
            "title": "The Standard Pyramid (Base-50-25)",
            "intro": ("Add to winners, never to losers. The base is sized at 1R "
                      "(risk per trade / stop distance). After Level 1 the worst "
                      "case is -0.5R; after Level 2 +0.75R is locked. An add is "
                      "also refused when the desk is halted, past the entry "
                      "cutoff or over the exposure cap — the stop still steps up. "
                      "Results are measured in the BASE trade's R."),
            "rules": rows,
        })

    # ------------------------------------------------------------------ #
    setups = ", ".join(g("risk.time_stop_setups") or ["Mean Reversion"])
    sections.append({
        "title": "When it sells",
        "intro": "Checked every cycle. The first of these to happen closes the trade.",
        "rules": [
            _rule("Stop hit: sold at the stop (a planned loss of 1R)", "−1R",
                  "set per trade, above"),
            _rule("Target hit: sold at the target"
                  + (" — the pyramid's single target, the whole position"
                     if g("risk.pyramid.enabled", False) else ""),
                  (f"+{g('risk.pyramid.target_r', 3.0):g}R from the base entry"
                   if g("risk.pyramid.enabled", False)
                   else f"+{g('risk.min_risk_reward', 2.0)}R or more"),
                  "risk.pyramid.target_r" if g("risk.pyramid.enabled", False)
                  else "set per trade, above"),
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
