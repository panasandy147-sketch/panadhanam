"""The desk's rules and strategies, written out from the live config.

This is what the Rules pop-up on the dashboard shows. Every number comes
from the configuration the desk is running with at that moment, not from a
copy in a document, so the page cannot drift from what the desk does. Each
rule carries the setting that controls it, which is what to name when asking
for a change.
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


def _money(value: Any, currency: str = "$") -> str:
    try:
        return f"{currency}{float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def build(cfg, capital: float | None = None) -> dict[str, Any]:
    g = cfg.get
    cur = str(g("account.currency", "$") or "$")
    capital = float(capital if capital is not None
                    else g("account.starting_capital", 0) or 0)
    per_trade = float(g("risk.max_capital_deployed_pct", 20.0))
    total = float(g("risk.max_total_deployed_pct", per_trade) or per_trade)
    stop_mode = str(g("risk.stop_mode", "premium"))
    exit_style = str(g("risk.exit_style", "scale"))
    loss_pct = g("risk.daily_loss_limit_pct", 10.0)
    loss_fixed = g("risk.daily_loss_limit")
    hold = g("risk.max_hold_minutes")
    index_pct = float(g("risk.index_max_capital_deployed_pct", per_trade) or per_trade)

    def window(key: str) -> str:
        return f"{g(f'strategies.{key}.from', '—')} – {g(f'strategies.{key}.to', '—')} ET"

    def enabled(key: str) -> bool:
        return bool(g(f"strategies.{key}.enabled", True))

    sections: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "How a trade happens",
        "intro": ("Paper only. Every cycle the desk reads each symbol that "
                  "passed the pre-market screen at the same moment, runs every "
                  "strategy whose window is open, and buys the option contract "
                  "for the first setup that triggers, as long as there is room "
                  "under the position and capital limits. Everything is in New "
                  "York time."),
        "rules": [
            _rule("Market (the US / India / Auto toggle in the header)",
                  f"{getattr(cfg, 'market_name', 'United States')} — "
                  f"{g('session.market_open', '09:30')}–{g('session.market_close', '16:00')} "
                  f"{g('session.timezone', 'America/New_York')}, account in "
                  f"{g('account.currency', '$')}",
                  "the header toggle (PANAOPTIONS_MARKET in .env)"),
            _rule("Desk profile",
                  {"zerodte": "zerodte — same-day options, 5-minute triggers",
                   "scalp": "scalp — 1-minute, SPY/QQQ/IWM 0DTE"}.get(
                      getattr(cfg, "profile", "") or "", "default — 7–30 day options"),
                  "PANAOPTIONS_PROFILE in .env"),
            _rule("Symbols the desk watches",
                  ", ".join(getattr(cfg, "symbols", None) or g("universe.symbols", []) or []),
                  "the Watchlist box on the dashboard (or universe.symbols)"),
            _rule("Auto watchlist: only names whose options can be bought — a call AND a "
                  "put near the money with a bid and a spread within "
                  f"{g('contracts.max_spread_pct_of_mid', 7.0)}% (SPY, QQQ and held "
                  "names are not checked)",
                  "on" if g("auto_watchlist.options_liquidity_check", True) else "off",
                  "auto_watchlist.options_liquidity_check"),
            _rule("Pre-market screen: relative volume at least",
                  f"{g('premarket.min_rvol', 1.5)}x the 20-day average, paced on the "
                  f"market's own session ({g('session.market_open', '09:30')}–"
                  f"{g('session.market_close', '16:00')}). An index with no volume in "
                  f"the feed (NIFTY, BANKNIFTY, FINNIFTY) is judged on the gap alone",
                  "premarket.min_rvol"),
            _rule("Pre-market screen: gap from yesterday's close at least",
                  _pct(g("premarket.min_gap_pct", 1.0)), "premarket.min_gap_pct"),
            _rule("Screen runs from, and re-runs while nothing passes, every",
                  f"{g('premarket.screen_from', '09:00')}, every "
                  f"{g('premarket.rescreen_minutes', 5)} min",
                  "premarket.screen_from"),
            _rule("First entry allowed at", g("session.entry_open", "09:35"),
                  "session.entry_open"),
            _rule("Last entry allowed at (the last strategy window's close)",
                  getattr(cfg, "last_entry_hhmm", g("session.entry_close")),
                  "strategies.*.to"),
            _rule("Everything is closed at", g("session.force_exit_at", "15:45"),
                  "session.force_exit_at"),
        ],
    })

    # ------------------------------------------------------------------ #
    weights = {**{"technical": 0.35, "derivatives": 0.35, "macro": 0.30},
               **(g("agents.weights") or {})}
    learned = g("strategy_weights") or {}
    if bool(g("agents.enabled", True)):
        sections.append({
            "title": "Who approves it",
            "intro": ("A triggered strategy is only a signal: symbol, direction, "
                      "trigger, invalidation level on the stock, confidence. It "
                      "knows nothing about money. Three agents vote on it, the "
                      "CMIO combines the votes, and the Risk Gatekeeper checks "
                      "the contract before any order exists. With Ollama running "
                      "each agent also asks the local model and blends its view "
                      "in; without it the rules score stands."),
            "rules": [
                _rule("Technical agent: 5m trigger vs the 15m trend; relative "
                      "volume (trigger bar or session, whichever is higher) must "
                      "be at least",
                      f"{g('agents.technical.min_rvol', 1.5)}x — hard veto below "
                      f"(not applied to an index with no volume in the feed)",
                      "agents.technical.min_rvol"),
                _rule("Midday RVOL: between these times (market clock) every volume "
                      "gate asking for more asks for this instead — the Technical "
                      "agent's veto and the strategies' volume multiples",
                      (f"{g('technical.midday_rvol.from', '10:30')}–"
                       f"{g('technical.midday_rvol.to', '14:00')}: "
                       f"{g('technical.midday_rvol.min', 1.2)}x"
                       if g("technical.midday_rvol.enabled", False) else "off"),
                      "technical.midday_rvol"),
                _rule("A live 5m bar with NO volume (0 or missing — feed lag, or an "
                      "NSE index) is 'not measured', not 'quiet'",
                      "the volume gates are bypassed for it, and the log says so",
                      "engine/strategies.py volume_unreported"),
                _rule("Volume profile confluence on every signal: a call at the "
                      "VAL or POC, or a put at the VAH or POC, scores "
                      f"+{g('volume_profile.alignment_boost', 0.30)}; a thick High "
                      "Volume Node straight ahead costs "
                      f"{g('volume_profile.hvn_penalty', 0.30)} within "
                      f"{g('volume_profile.hvn_near_atr', 1.0)} ATR and is a veto "
                      f"within {g('volume_profile.hvn_veto_atr', 0.25)} ATR",
                      "boost / penalty / veto", "volume_profile.*"),
                _rule("Extreme options flow (a contract at "
                      f"{g('agents.derivatives.extreme_flow_ratio', 10.0):g}x+ its open "
                      "interest): AGAINST the trade = strict veto (no calls into "
                      "heavy put buying); WITH the trade = the RVOL gate relaxes to "
                      f"{g('agents.technical.flow_confluence_min_rvol', 1.3)}x. The "
                      "Derivatives vote counts "
                      f"{g('agents.derivatives.extreme_flow_weight_multiplier', 2.0):g}x "
                      "meanwhile",
                      "veto / override", "agents.derivatives.extreme_flow_ratio"),
                _rule("Derivatives & Flow agent: the contract, put/call ratio, IV "
                      "percentile (penalised above), unusual flow; no contract = veto",
                      f"IV ceiling {g('agents.derivatives.iv_percentile_ceiling', 80)}th "
                      f"percentile, 0–{g('agents.derivatives.preferred_max_dte', 1)} DTE "
                      "preferred", "agents.derivatives.*"),
                _rule("Macro & Sentiment agent: a high-impact headline against the "
                      "trade (downgrade, guidance cut, investigation under a call; "
                      "buyout, upgrade under a put) or index futures moving hard "
                      "against it is a hard veto",
                      f"futures veto at {_pct(g('agents.macro.futures_veto_pct', 1.5))}",
                      "agents.macro.*"),
                _rule("CMIO: weighted score × strategy weight must reach, with no veto",
                      f"{g('agents.approve_threshold', 0.55)} (weights "
                      + ", ".join(f"{k} {v}" for k, v in weights.items()) + ")",
                      "agents.approve_threshold / agents.weights"),
                _rule("Strategy weights (tuned by the Friday reflection)",
                      ", ".join(f"{k} {float(v):.2f}" for k, v in learned.items()) or "all 1.00",
                      "strategy_weights (config/learned.yaml)"),
                _rule("Ollama second opinion",
                      f"{'on' if g('agents.use_llm', True) else 'off'} — blended at "
                      f"{_pct(float(g('agents.llm_weight', 0.4)) * 100)}, "
                      f"{g('agents.llm_timeout_seconds', 20)} s timeout",
                      "agents.use_llm / llm_weight"),
            ],
        })

    # ------------------------------------------------------------------ #
    orb_mult = g("strategies.orb_vwap.volume_multiple", 1.5)
    orb_ext = g("strategies.orb_vwap.target_range_multiple", 1.5)
    touch = g("strategies.vwap_ema_pullback.touch_atr", 0.35)
    sweep_mult = g("strategies.liquidity_sweep.volume_multiple", 1.5)
    tol = g("strategies.candlestick_at_level.level_tolerance_atr", 0.5)
    within = g("strategies.candlestick_at_level.trigger_within_bars", 2)
    tf = g("strategies.candlestick_at_level.timeframe", "15m")
    allowed_patterns = list(g("strategies.candlestick_at_level.allowed_patterns") or [])
    sw = "strategies.pd_liquidity_sweep"
    strategies = [
        {
            "key": "pd_liquidity_sweep",
            "name": "0 · Previous Day Liquidity Sweep (failed breakout) — first in line",
            "window": window("pd_liquidity_sweep"), "enabled": enabled("pd_liquidity_sweep"),
            "buy": [
                f"LONG_CALL: a 5m candle sweeps BELOW the previous-day low (PDL) by no "
                f"more than {g(f'{sw}.proximity_pct', 0.25)}%, and the very next "
                "candle closes back inside yesterday's range — a tweezer bottom or "
                "swing low at the PDL.",
                f"LONG_PUT: the mirror — a sweep ABOVE the previous-day high (PDH) "
                f"within {g(f'{sw}.proximity_pct', 0.25)}% and the next candle closes "
                "back inside, as a tweezer top or swing high.",
                "Call open interest rising (a PDL sweep) / put open interest rising "
                "(a PDH sweep) — the F&O confluence filter.",
                f"The target must be at least 1:{g(f'{sw}.min_reward_risk', 3.0):g} "
                "against the stop at the sweep wick — checked before the order.",
            ],
            "wrong": (f"A trade through the sweep candle's wick — the stop sits "
                      f"{g(f'{sw}.stop_ticks', 2)} ticks beyond it."),
            "target": "The day's VWAP or 3R, whichever is further. No time stop.",
        },
        {
            "key": "orb_vwap", "name": "1 · Opening Range Breakout + VWAP",
            "window": window("orb_vwap"), "enabled": enabled("orb_vwap"),
            "buy": [
                "The 15-minute opening range (09:30–09:45 high and low) has formed.",
                "A 5-minute candle CLOSES above the range high (calls) or below "
                "the range low (puts).",
                "Price is on the same side of VWAP, and the 9 EMA is above the 21 EMA "
                "for calls (below for puts).",
                f"Volume on the break is at least {orb_mult}x the average. Without "
                "it, the break is recorded as unbacked and not taken.",
            ],
            "wrong": "A 5-minute close back inside the opening range.",
            "target": f"{orb_ext}x the range height, measured from the broken edge.",
        },
        {
            "key": "vwap_ema_pullback", "name": "2 · VWAP / 9-EMA Pullback",
            "window": window("vwap_ema_pullback"),
            "enabled": enabled("vwap_ema_pullback"),
            "buy": [
                "The 15-minute chart is in a stacked trend: price > 20 EMA > 50 EMA "
                "for calls, the reverse for puts (needs 50 bars of 15m history).",
                f"Price pulls back to the 9 EMA or VWAP (within {touch} ATR).",
                "A rejection candle prints at the line (engulfing, hammer or similar).",
                "That candle closes beyond the previous candle's high (calls) or "
                "low (puts). The desk enters on the rejection and does not chase.",
            ],
            "wrong": "A 5-minute close on the wrong side of VWAP.",
            "target": "The desk's standard exits (below).",
        },
        {
            "key": "liquidity_sweep", "name": "3 · Liquidity Sweep Reversal",
            "window": window("liquidity_sweep"),
            "enabled": enabled("liquidity_sweep"),
            "buy": [
                "A 5-minute candle pokes below the pre-market low (or above the "
                "pre-market high), taking the stops resting there.",
                "The NEXT candle closes back inside the pre-market range.",
                "Price crosses VWAP in the new direction: above for calls, below for puts.",
                f"Volume is at least {sweep_mult}x the average.",
            ],
            "wrong": "Price goes back through the extreme of the sweep.",
            "target": "The desk's standard exits (below).",
        },
        {
            "key": "candlestick_at_level", "name": "4 · Candlestick at a Key Level",
            "window": window("candlestick_at_level"),
            "enabled": enabled("candlestick_at_level"),
            "buy": [
                (f"A reversal pattern on the {tf} chart — only these: "
                 + ", ".join(allowed_patterns)
                 + " (a hammer or shooting star that pierced the level and closed "
                   "back counts as a liquidity sweep rejection)."
                 if allowed_patterns else
                 f"A reversal pattern on the {tf} chart. LONG_CALL: hammer, "
                 "bullish engulfing, morning star, tweezer bottom, double "
                 "rejection bottom, piercing line, three white soldiers, bullish "
                 "three-line strike, bullish abandoned baby. LONG_PUT: the bearish "
                 "mirror of each (tweezer top, double rejection top …)."),
                "Double rejection: the same high (put) or low (call) rejected "
                "twice, 2–8 bars apart, with price leaving the level by a full "
                "average bar in between.",
                f"It prints AT a level the market has turned at before, within "
                f"{tol} ATR: the previous-day high or low, a swing high or low, "
                "the pre-market extreme, the opening range edge, yesterday's "
                "close, VWAP or a moving average. VWAP, the averages and "
                "yesterday's close are resistance from below and support from "
                "above. A pattern anywhere else is ignored.",
                "When the newest pattern is refused (its trend, its level, or its "
                "trigger not broken yet) the next pattern on the same bars is "
                "judged.",
                *([("F&O confluence (strict): a call only where the pattern "
                    "swept the previous-day LOW and closed back above it with "
                    "call open interest rising; a put only where it tested the "
                    "previous-day HIGH and closed back below it with put open "
                    "interest rising. Unknown OI: "
                    + str(g("fno.confluence.when_oi_unknown", "block")) + ".")]
                  if g("fno.confluence.enabled", False) else []),
                f"Price then takes out the pattern's trigger (the high of a hammer, "
                f"the low of a shooting star) within {within} bars. The pattern "
                "alone is not the entry.",
                "Each pattern picks its own contract (delta and days to expiry). "
                "See the table below.",
            ],
            "wrong": "Price breaks the structure the pattern formed at.",
            "target": "The desk's standard exits (below).",
        },
        {
            "key": "va_rejection", "name": "5 · Value Area Rejection (failed auction)",
            "window": window("va_rejection"), "enabled": enabled("va_rejection"),
            "buy": [
                "Built on the session volume profile (RTH 09:30–16:00): the POC "
                f"and the {float(g('volume_profile.value_area_pct', 0.70)):.0%} "
                "value area (VAL–VAH), for the prior and the current session.",
                "Puts: price pushes clearly above the VAH, fails to hold (a "
                "one-sided rejection wick or a bearish engulfing) and closes back "
                "inside value.",
                "Calls: price drops to the VAL, tests it, and rejects with a "
                "hammer or bullish engulfing, closing back above it.",
            ],
            "wrong": "Beyond the poke's high (puts) or the test's low (calls).",
            "target": "The POC.",
        },
        {
            "key": "lvn_acceleration", "name": "6 · LVN Pocket Acceleration",
            "window": window("lvn_acceleration"), "enabled": enabled("lvn_acceleration"),
            "buy": [
                "A Low Volume Node is a price pocket inside the profile where "
                f"almost nothing traded (≤ {g('volume_profile.lvn_ratio', 0.30)}x the "
                "average bin) — price tends to travel through it fast.",
                f"After a {g('strategies.lvn_acceleration.shelf_bars', 3)}-bar "
                "consolidation shelf, a 5-minute candle closes cleanly INTO the "
                f"pocket on relative volume ≥ "
                f"{g('strategies.lvn_acceleration.min_rvol', 1.5)}x.",
                "Calls on an upside break, puts on a downside one.",
            ],
            "wrong": "Back inside the shelf it broke out of.",
            "target": "The far edge of the pocket.",
        },
        {
            "key": "poc_bounce", "name": "7 · POC Magnet / Bounce",
            "window": window("poc_bounce"), "enabled": enabled("poc_bounce"),
            "buy": [
                f"Price moves at least {g('strategies.poc_bounce.away_atr', 1.0)} ATR "
                "away from the POC, then comes back to retest it.",
                "A clean rejection candle prints at the POC: calls when it came "
                "back down from above, puts when it came back up from below.",
            ],
            "wrong": "A close through the POC.",
            "target": "The swing it came back from.",
        },
        {
            "key": "volatility_breakout",
            "name": "8 · Volatility Breakout (Larry Williams, 1987 World Cup)",
            "window": window("volatility_breakout"),
            "enabled": enabled("volatility_breakout"),
            "buy": [
                f"The FIRST 5m close above today's open + "
                f"{g('strategies.volatility_breakout.k', 0.5)} x yesterday's range "
                "(calls), or below the open minus the same (puts). A level crossed "
                "earlier in the day is not taken — that is a chase.",
                "Price on the same side of VWAP, and the 9 EMA above the 21 EMA for "
                "calls (below for puts).",
            ],
            "wrong": (f"Back {g('strategies.volatility_breakout.stop_fraction', 1.0)} of "
                      "the way from the entry to today's open — the expansion failed."),
            "target": f"{g('strategies.volatility_breakout.target_r', 3.0)}R.",
        },
    ]
    sections.append({
        "title": "The strategies",
        "intro": ("Every condition listed must be true together. If two "
                  "strategies trigger on the same symbol in the same cycle, the "
                  "one listed first wins. Setups that do not trigger are still "
                  "logged with the reason, so a quiet day can be told apart "
                  "from a broken one."),
        "strategies": strategies,
    })

    # ------------------------------------------------------------------ #
    pattern_rows = []
    allowed_keys = {a.lower().replace(" ", "_").replace("-", "_") for a in allowed_patterns}
    if allowed_keys & {"hammer", "shooting_star"}:
        allowed_keys.add("liquidity_sweep_rejection")
    for key, block in (g("strategies.candlestick_at_level.patterns", {}) or {}).items():
        if not isinstance(block, dict):
            continue
        if allowed_keys and key not in allowed_keys:
            continue
        delta = block.get("delta") or ["—", "—"]
        dte = block.get("dte") or ["—", "—"]
        claimed = block.get("claimed_accuracy")
        pattern_rows.append({
            "pattern": key.replace("_", " ").title(),
            "delta": f"{delta[0]}–{delta[1]}",
            "dte": f"{dte[0]}–{dte[1]} days",
            "claimed": f"{float(claimed) * 100:.0f}% (published claim, unverified here)"
                       if claimed else "",
        })
    sections.append({
        "title": "Previous-day F&O and reward to risk",
        "intro": ("Once a day after the screen the desk maps each watched symbol's "
                  "previous session: high (PDH), low (PDL), close (PDC), total "
                  "call and put open interest and their change, and the build-up "
                  "— Long Buildup (price up, OI up), Short Buildup (down, up), "
                  "Short Covering (up, down), Long Unwinding (down, down). US "
                  "chains carry the previous close's OI, so the change is the "
                  "previous session's; NSE updates it during the day."),
        "rules": [
            _rule("Map the previous day's F&O picture",
                  "on" if g("fno.ingest", True) else "off", "fno.ingest"),
            _rule("Reversal calls only at a PDL sweep with rising call OI; "
                  "reversal puts only at a PDH test with rising put OI",
                  ("on — " + ", ".join(g("fno.confluence.strategies",
                                         ["candlestick_at_level"]) or []))
                  if g("fno.confluence.enabled", False) else "off",
                  "fno.confluence.enabled / strategies"),
            _rule("GO / NO-GO before any 5-minute candlestick pattern is read: "
                  "LONG CALL only after a clean sweep below the PDL that closed "
                  "back inside; LONG PUT only after a sweep above the PDH. No "
                  "sweep: skipped — \"No institutional sweep of previous day "
                  "extremes.\"",
                  (", ".join(str(x).replace("_", " ")
                             for x in (g("fno.go_no_go.strategies") or [])) or "none")
                  if g("fno.go_no_go.enabled", True) else "off",
                  "fno.go_no_go"),
            _rule("Cached before the open, from",
                  f"{g('fno.ingest_from', g('premarket.screen_from', '09:00'))} "
                  f"({'ET' if cfg.market == 'US' else 'IST'})", "fno.ingest_from"),
            _rule("How a PDH / PDL is judged"
                  + (": a 5-minute candle sweeps through the level by no more "
                     f"than {g('fno.confluence.proximity_pct', 0.25)}% and the "
                     "very next candle closes back inside yesterday's range (the "
                     "stop goes beyond that candle's wick)"
                     if str(g("fno.confluence.mode", "sweep")) == "sweep"
                     else f": the pattern's extreme within "
                          f"{g('fno.confluence.touch_atr', 0.15)} ATR of the level"),
                  str(g("fno.confluence.mode", "sweep")),
                  "fno.confluence.mode / proximity_pct / touch_atr"),
            _rule("When the chain carries no open interest",
                  g("fno.confluence.when_oi_unknown", "block"),
                  "fno.confluence.when_oi_unknown"),
            _rule("Every trade's projected target is at least this multiple of "
                  "the distance to its invalidation, with no previous-day, "
                  "opening-range or pre-market level in the way",
                  f"1:{g('risk.min_reward_risk', 3.0):g}" if g("risk.min_reward_risk", 3.0)
                  else "off", "risk.min_reward_risk / reward_room_levels"),
            _rule("Stop floor: the stop on the underlying sits at least the larger of "
                  "this many 5m ATRs or this % of the price from the entry (a closer "
                  "one is widened, and the target kept at 1:3 from it)",
                  (f"{g('risk.min_stop_atr', 0)} × ATR or {g('risk.min_stop_pct', 0)}%"
                   if (g("risk.min_stop_atr", 0) or g("risk.min_stop_pct", 0)) else "off"),
                  "risk.min_stop_atr / risk.min_stop_pct"),
            _rule("Target snapping: with less than 1:3 of room to the nearest level but "
                  "at least this much, the trade is taken with the target 2 ticks "
                  "inside that level",
                  (f"{g('risk.target_snap.min_r', 2.2)}R" if g("risk.target_snap.enabled", False)
                   else "off"), "risk.target_snap"),
            _rule("Verified 1:3 — these must reach it with their OWN target (the "
                  "POC for a value-area rejection, VWAP-or-3R for the sweep), not "
                  "a projected one",
                  ", ".join(str(s).replace("_", " ")
                            for s in (g("risk.own_target_strategies") or [])) or "none",
                  "risk.own_target_strategies"),
        ],
    })

    sections.append({
        "title": "Which contract it buys",
        "intro": ("Calls for a LONG_CALL setup, puts for a LONG_PUT one — both "
                  "bought, never sold naked. Each trade is logged as executed "
                  "outright, converted to a debit spread, or skipped by a hard "
                  "risk gate (with which gate and why). A single "
                  "option you buy — or, when that is over budget, a debit "
                  "spread: buy the target-delta option and sell one further "
                  "out of the money, so the most it can lose is what was paid."),
        "rules": [
            _rule("Days to expiry (strategies 1–3)"
                  + (" — the NEAREST expiry first: 0DTE where the symbol lists "
                     "one (SPY/QQQ/IWM daily; most stocks only on Friday), "
                     "otherwise that week's"
                     if g("contracts.prefer_nearest_expiry", False) else ""),
                  f"{g('contracts.min_dte', 7)}–{g('contracts.max_dte', 14)} days",
                  "contracts.min_dte / max_dte"),
            _rule("Delta — the primary tier (strategies 1–3)",
                  f"{g('contracts.min_delta', 0.40)}–{g('contracts.max_delta', 0.50)}",
                  "contracts.min_delta / max_delta"),
            _rule("Bid/ask spread no wider than (judged on the rolling "
                  "1-minute volume-weighted spread"
                  + (", re-sampled inside the minute before refusing"
                     if g("contracts.rolling_spread.enabled", True) else "") + ")",
                  f"{_pct(g('contracts.max_spread_pct_of_mid', 7.0))} of the mid price",
                  "contracts.max_spread_pct_of_mid / rolling_spread"),
            _rule("Opening window: for expiries within "
                  f"{g('contracts.opening_spread.max_dte', 4)} days, the spread "
                  "allowance widens from "
                  f"{g('contracts.opening_spread.from', '09:15')} to "
                  f"{g('contracts.opening_spread.to', '10:00')} "
                  "(opening quotes are wide; after that, the normal limit)",
                  f"{_pct(g('contracts.opening_spread.max_spread_pct', 12.0))}"
                  if g("contracts.opening_spread.enabled", False) else "off",
                  "contracts.opening_spread"),
            _rule("Over budget, in order: the same delta with less time; a "
                  "debit spread (buy the primary-tier option, sell a strike "
                  "further out, reward:risk ≥ "
                  f"{g('contracts.debit_spread.min_reward_risk', 0.8)}); then the "
                  "secondary tier down to this delta, liquid contracts only "
                  f"(open interest ≥ {g('contracts.liquidity.min_open_interest', 100)} "
                  f"or volume ≥ {g('contracts.liquidity.min_volume', 50)}). "
                  "Nothing funded = skipped as a hard risk failure",
                  g("contracts.budget_fallback_min_delta", 0.30) or "off",
                  "contracts.fallback_order / debit_spread / liquidity"),
            _rule("Single-leg grace: when the debit spread fails only because "
                  "the short leg has no liquid, priced strike, buy the long leg "
                  "outright if premium x lot fits this share of the budget",
                  f"{_pct(g('contracts.single_leg_grace.max_budget_pct', 100))}"
                  if g("contracts.single_leg_grace.enabled", True) else "off",
                  "contracts.single_leg_grace"),
            _rule("Unusual options flow (volume ≥ "
                  f"{g('flow.min_volume_to_oi', 3.0)}x open interest, ≥ "
                  f"{g('flow.min_volume', 1000)} contracts) passes the screen "
                  "and scores for or against a setup in the Derivatives vote",
                  "on" if g("flow.pass_screen", True) else "off",
                  "flow.pass_screen / min_volume_to_oi"),
            _rule("Contract price between",
                  f"{cur}{g('contracts.min_contract_price', 0.10)} and "
                  f"{cur}{g('contracts.max_contract_price', 20.0)} "
                  f"(×{g('contracts.contract_multiplier', 100)} per contract)",
                  "contracts.min_contract_price / max_contract_price"),
            *([_rule("One contract is one exchange lot: its cost is premium x "
                     "lot, and that total is what the per-trade cap, the budget "
                     "and every fallback are judged on",
                     ", ".join(f"{k} {v}" for k, v in list(
                         (g("data.lot_sizes") or {}).items())[:8]) + " …",
                     "data.lot_sizes")] if g("data.lot_sizes") else []),
        ],
        "patterns": pattern_rows,
    })

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "How much it buys",
        "intro": ("Two caps size every trade and the tighter wins: the premium "
                  "paid as a share of the account, and what the trade loses at "
                  "its stop. Then the tournament throttles: few positions, few "
                  "trades a day, and a hard daily lockout."),
        "rules": [
            _rule("Account size", _money(capital, cur),
                  "account.starting_capital (or PANAOPTIONS_CAPITAL in .env)"),
            _rule("Premium per trade, at most",
                  f"{_pct(per_trade)} = {_money(capital * per_trade / 100, cur)}",
                  "risk.max_capital_deployed_pct"),
            _rule("Index ETFs (" + ", ".join(g("risk.index_symbols") or ["SPY", "QQQ", "DIA"])
                  + ") — high-notional contracts, so a higher cap",
                  f"{_pct(index_pct)} = {_money(capital * index_pct / 100, cur)}",
                  "risk.index_max_capital_deployed_pct"),
            _rule("Risk Gatekeeper refuses a bid/ask spread wider than",
                  f"{_pct(g('risk.max_spread_pct_of_mid', 7.0))} of the mid",
                  "risk.max_spread_pct_of_mid"),
            _rule("All open trades together, at most",
                  f"{_pct(total)} = {_money(capital * total / 100, cur)}",
                  "risk.max_total_deployed_pct"),
            *([_rule("Loss at the stop per trade, at most — the first exit to fire "
                     "(delta x the distance to the underlying stop, or the premium "
                     "backstop); with the premium cap above, the tighter one sizes it",
                     f"{_pct(g('risk.max_risk_per_trade_pct'))} = "
                     f"{_money(capital * float(g('risk.max_risk_per_trade_pct')) / 100, cur)}",
                     "risk.max_risk_per_trade_pct")]
              if g("risk.max_risk_per_trade_pct") else []),
            _rule("Open trades at once, at most", g("risk.max_open_trades", 1),
                  "risk.max_open_trades"),
            _rule("Trades a day, at most (no over-trading)",
                  g("risk.max_daily_trades") or "no limit", "risk.max_daily_trades"),
            _rule("Of those, the last ones are kept for the strategies with the best "
                  "backtested edge (the last `run.py --backtest`, else the priority "
                  "list: " + ", ".join(str(s).replace("_", " ") for s in
                                       (g("ranking.priority") or [])[:int(
                                           g("ranking.preferred_top", 2))]) + "); "
                  "setups firing together are taken best edge first",
                  g("risk.reserved_slots") or "none",
                  "risk.reserved_slots / ranking"),
            _rule("Circuit breaker: once the day's loss, closed plus open, "
                  "reaches this, pending signals are cancelled, everything is sold, "
                  "orders are refused and the desk is LOCKED OUT for the rest of "
                  "the calendar day — a restart does not clear it",
                  _money(loss_fixed, cur) if loss_fixed
                  else f"{_pct(loss_pct)} = {_money(capital * float(loss_pct) / 100, cur)}",
                  "risk.daily_loss_limit_pct"),
            _rule("History validation (python run.py --backtest): expectancy per "
                  "trade at least, and max drawdown no more than",
                  f"{g('backtest.validation.min_expectancy_r', 0.5)}R · "
                  f"{_pct(g('backtest.validation.max_drawdown_pct', 5.0))}",
                  "backtest.validation"),
            _rule("Simulated cost per contract, each side",
                  f"${g('risk.slippage_per_contract', 0.02)}",
                  "risk.slippage_per_contract"),
        ],
    })

    # ------------------------------------------------------------------ #
    exits = []
    if stop_mode == "underlying":
        exits.append(_rule(
            "Stop: the strategy's own invalidation on the STOCK decides "
            "(see each strategy's 'wrong if'). The option's price is not "
            "used, because an IV drop or a wide spread says nothing about "
            "whether the idea was wrong.", stop_mode, "risk.stop_mode"))
        exits.append(_rule(
            "Disaster backstop: out regardless if the option falls this far",
            _pct(g("risk.disaster_stop_pct", 45.0)), "risk.disaster_stop_pct"))
    else:
        exits.append(_rule("Stop: the option falls by",
                           _pct(g("risk.stop_loss_pct", 20.0)), "risk.stop_loss_pct"))
    if exit_style == "r_multiple" and not float(g("risk.scale_out_r", 1.5) or 0):
        exits.append(_rule(
            "Hold: no scale-out, no breakeven, no trail — the trade keeps its "
            "original stop on the UNDERLYING (and the premium backstop) until "
            "the square-off", "on (scale_out_r 0)", "risk.exit_style / risk.scale_out_r"))
    elif exit_style == "r_multiple":
        exits.append(_rule(
            "Scale out at this multiple of the trade's risk, measured on the "
            "UNDERLYING: sell half and move the stop to breakeven (one contract "
            "cannot be halved — then only the stop moves)",
            f"+{g('risk.scale_out_r', 1.5)}R, sell "
            f"{_pct(g('risk.take_profit_1_size_pct', 50.0))}",
            "risk.exit_style / risk.scale_out_r"))
        exits.append(_rule(
            "Then trail the rest this far behind the best level the underlying "
            "has reached; whatever is left goes at the square-off",
            f"{g('risk.runner_trail_r', 1.0)}R", "risk.runner_trail_r"))
    if exit_style in {"auto", "trail"}:
        exits.append(_rule(
            "Single contract (or exit_style 'trail'): at this gain the stop "
            "moves to breakeven, then the desk holds until a 5m candle closes "
            "beyond the 9 EMA",
            _pct(g("risk.breakeven_trigger_pct", 35.0)), "risk.exit_style / "
            "risk.breakeven_trigger_pct"))
    if exit_style != "r_multiple":
        exits += [
            _rule("Two or more contracts: first target. Sell this share of the "
                  "position and move the stop to breakeven",
                  f"+{_pct(g('risk.take_profit_1_pct', 40.0))}, sell "
                  f"{_pct(g('risk.take_profit_1_size_pct', 50.0))}",
                  "risk.take_profit_1_pct"),
            _rule("Second target (or the 9-EMA trail, whichever comes first)",
                  f"+{_pct(g('risk.take_profit_2_pct', 70.0))}", "risk.take_profit_2_pct"),
        ]
    exits += [
        _rule("Morning trades that are green at this time get their stop "
              "moved to breakeven before the lunch slump",
              g("session.tighten_stops_at", "10:45"), "session.tighten_stops_at"),
        _rule("Time limit on a trade that has not reached its first target",
              f"{hold} min" if hold else "none (time is what the contract bought)",
              "risk.max_hold_minutes"),
        _rule("Everything still open is sold at", g("session.force_exit_at", "15:45"),
              "session.force_exit_at"),
    ]
    sections.append({
        "title": "When it sells",
        "intro": "The first of these to happen closes the trade.",
        "rules": exits,
    })

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "After the trade",
        "intro": ("Every closed trade is graded on process, not outcome. A "
                  "winner that broke a rule grades low, and a loser that "
                  "respected its invalidation grades high. The daily and "
                  "weekly reviews are built from these grades."),
        "rules": [
            _rule("Grade each trade as it closes",
                  "on" if g("journal.auto_grade", True) else "off",
                  "journal.auto_grade"),
            _rule("Friday reflection: after the close, the week's graded "
                  "trades go to Ollama, which returns strategy weight "
                  "adjustments; they are bounded and written to "
                  "config/learned.yaml (also `python run.py --reflect`)",
                  (f"on — at least {g('reflection.min_trades', 5)} trades, at most "
                   f"±{g('reflection.max_step', 0.15)} a week"
                   if g("reflection.enabled", True) else "off"),
                  "reflection.*"),
        ],
    })

    return {"desk": "panaoptions",
            "profile": getattr(cfg, "profile_label", "") or "default",
            "sections": sections}
