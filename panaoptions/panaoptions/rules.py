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
            _rule("Symbols the desk watches",
                  ", ".join(g("universe.symbols", []) or []), "universe.symbols"),
            _rule("Pre-market screen: relative volume at least",
                  f"{g('premarket.min_rvol', 1.5)}x the 20-day average",
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
    orb_mult = g("strategies.orb_vwap.volume_multiple", 1.5)
    orb_ext = g("strategies.orb_vwap.target_range_multiple", 1.5)
    touch = g("strategies.vwap_ema_pullback.touch_atr", 0.35)
    sweep_mult = g("strategies.liquidity_sweep.volume_multiple", 1.5)
    tol = g("strategies.candlestick_at_level.level_tolerance_atr", 0.5)
    within = g("strategies.candlestick_at_level.trigger_within_bars", 2)
    tf = g("strategies.candlestick_at_level.timeframe", "15m")
    strategies = [
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
                f"A reversal pattern on the {tf} chart. Calls: hammer, bullish "
                "engulfing, morning star, tweezer bottom, piercing line, three "
                "white soldiers, bullish three-line strike, bullish abandoned "
                "baby. Puts: the bearish mirror of each.",
                f"It prints AT a level the market has turned at before, within "
                f"{tol} ATR: a swing high or low, the pre-market extreme, the "
                "opening range edge, yesterday's close, VWAP or a moving average. "
                "A pattern anywhere else is ignored.",
                f"Price then takes out the pattern's trigger (the high of a hammer, "
                f"the low of a shooting star) within {within} bars. The pattern "
                "alone is not the entry.",
                "Each pattern picks its own contract (delta and days to expiry). "
                "See the table below.",
            ],
            "wrong": "Price breaks the structure the pattern formed at.",
            "target": "The desk's standard exits (below).",
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
    for key, block in (g("strategies.candlestick_at_level.patterns", {}) or {}).items():
        if not isinstance(block, dict):
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
        "title": "Which contract it buys",
        "intro": ("Calls for a long setup, puts for a short one. Always a "
                  "single-leg option you buy: no selling, no spreads."),
        "rules": [
            _rule("Days to expiry (strategies 1–3)",
                  f"{g('contracts.min_dte', 7)}–{g('contracts.max_dte', 14)} days",
                  "contracts.min_dte / max_dte"),
            _rule("Delta (strategies 1–3)",
                  f"{g('contracts.min_delta', 0.45)}–{g('contracts.max_delta', 0.60)}",
                  "contracts.min_delta / max_delta"),
            _rule("Bid/ask spread no wider than",
                  f"{_pct(g('contracts.max_spread_pct_of_mid', 5.0))} of the mid price",
                  "contracts.max_spread_pct_of_mid"),
            _rule("Contract price between",
                  f"${g('contracts.min_contract_price', 0.10)} and "
                  f"${g('contracts.max_contract_price', 20.0)} "
                  f"(×{g('contracts.contract_multiplier', 100)} per contract)",
                  "contracts.min_contract_price / max_contract_price"),
        ],
        "patterns": pattern_rows,
    })

    # ------------------------------------------------------------------ #
    sections.append({
        "title": "How much it buys",
        "intro": ("Size is set by capital deployed: the premium paid as a "
                  "share of the account. The stop then decides how much of "
                  "that premium is actually at risk."),
        "rules": [
            _rule("Account size", _money(capital, cur),
                  "account.starting_capital (or PANAOPTIONS_CAPITAL in .env)"),
            _rule("Premium per trade, at most",
                  f"{_pct(per_trade)} = {_money(capital * per_trade / 100, cur)}",
                  "risk.max_capital_deployed_pct"),
            _rule("All open trades together, at most",
                  f"{_pct(total)} = {_money(capital * total / 100, cur)}",
                  "risk.max_total_deployed_pct"),
            _rule("Open trades at once, at most", g("risk.max_open_trades", 1),
                  "risk.max_open_trades"),
            _rule("Stop trading for the day after losing",
                  _money(loss_fixed, cur) if loss_fixed
                  else f"{_pct(loss_pct)} = {_money(capital * float(loss_pct) / 100, cur)}",
                  "risk.daily_loss_limit_pct"),
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
    if exit_style in {"auto", "trail"}:
        exits.append(_rule(
            "Single contract (or exit_style 'trail'): at this gain the stop "
            "moves to breakeven, then the desk holds until a 5m candle closes "
            "beyond the 9 EMA",
            _pct(g("risk.breakeven_trigger_pct", 35.0)), "risk.exit_style / "
            "risk.breakeven_trigger_pct"))
    exits += [
        _rule("Two or more contracts: first target. Sell this share of the "
              "position and move the stop to breakeven",
              f"+{_pct(g('risk.take_profit_1_pct', 40.0))}, sell "
              f"{_pct(g('risk.take_profit_1_size_pct', 50.0))}",
              "risk.take_profit_1_pct"),
        _rule("Second target (or the 9-EMA trail, whichever comes first)",
              f"+{_pct(g('risk.take_profit_2_pct', 70.0))}", "risk.take_profit_2_pct"),
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
        ],
    })

    return {"desk": "panaoptions",
            "profile": getattr(cfg, "profile_label", "") or "default",
            "sections": sections}
