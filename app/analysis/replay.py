"""Historical replay — "what would this system have caught last week?"

Walks real historical candles bar by bar, rebuilding the analysis using ONLY
data available at that bar (no lookahead), and grades every setup that fired.

Two honest limitations, stated up front because they change how you read it:

  1. It replays the DETERMINISTIC rule engines, not the Claude pass. Replaying
     thousands of bars through an LLM would cost more than the information is
     worth, and the rule engines are what generate the setups anyway.
  2. Fills are assumed at the stop or target price with no slippage, and an
     intrabar sequence ambiguity (both levels touched in one candle) is always
     resolved AGAINST the trade. Real results will be worse, not better.

With BROKER=paper the candles are synthetic, so the output is a mechanical
demo. Connect a real broker and it replays genuine market history.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import Candle, MarketContext, Quote
from app.indicators import patterns as pattern_mod
from app.indicators import ta

log = get_logger("analysis.replay")


class WeeklyReplay:
    """Replay the desk's rules over recent history and report what worked."""

    def __init__(self, engine: Any, cfg: Config | None = None) -> None:
        self.engine = engine
        self.cfg = cfg or get_config()
        self.last_result: dict[str, Any] = {}

    async def run(self, days: int = 5, timeframe: str = "5m",
                  symbols: list[str] | None = None,
                  warmup: int = 60) -> dict[str, Any]:
        targets = symbols or [w["symbol"] for w in self.cfg.watchlist()]

        # ~75 five-minute bars per session.
        bars_per_day = {"1m": 375, "5m": 75, "15m": 25, "1d": 1}.get(timeframe, 75)
        needed = warmup + bars_per_day * days

        results = await asyncio.gather(
            *[self._replay_symbol(s, timeframe, needed, warmup) for s in targets],
            return_exceptions=True)

        all_trades: list[dict[str, Any]] = []
        per_symbol: list[dict[str, Any]] = []
        for symbol, res in zip(targets, results, strict=False):
            if isinstance(res, Exception):
                log.warning("replay failed for %s: %s", symbol, res)
                continue
            if not res["trades"]:
                continue
            all_trades.extend(res["trades"])
            per_symbol.append(res["summary"])

        # Most winners land on exactly the same +2.00R target, so ranking by R
        # alone produces ten identical rows from whichever symbol sorted first.
        # Break the tie on how FAST the target was reached — a 3-bar winner is a
        # materially better trade than a 30-bar one for the same R.
        winners = sorted([t for t in all_trades if t["r_multiple"] > 0],
                         key=lambda t: (-t["r_multiple"], t["bars_held"]))
        losers = sorted([t for t in all_trades if t["r_multiple"] <= 0],
                        key=lambda t: t["r_multiple"])

        total_r = sum(t["r_multiple"] for t in all_trades)
        wins = len(winners)

        self.last_result = {
            "ts": datetime.now().isoformat(),
            "data_source": self._provenance(),
            "window": {"days": days, "timeframe": timeframe,
                       "from": (datetime.now() - timedelta(days=days)).date().isoformat(),
                       "to": datetime.now().date().isoformat()},
            "totals": {
                "trades": len(all_trades),
                "wins": wins,
                "losses": len(all_trades) - wins,
                "win_rate": round(wins / len(all_trades) * 100, 1) if all_trades else 0.0,
                "total_r": round(total_r, 2),
                "avg_r": round(total_r / len(all_trades), 3) if all_trades else 0.0,
                "expectancy": "positive" if total_r > 0 else "negative",
            },
            "best_trades": winners[:10],
            "worst_trades": losers[:5],
            "by_symbol": sorted(per_symbol, key=lambda s: s["total_r"], reverse=True),
            "caveats": [
                "Replays the deterministic rule engines only — no LLM reasoning.",
                "Assumes fills exactly at stop/target with zero slippage or brokerage.",
                "When one candle touches both stop and target, the STOP is assumed "
                "to have hit first. Real sequencing needs tick data.",
                "This is a threshold sanity check, not a tradeable backtest: no "
                "survivorship-bias-free universe and no corporate-action handling.",
            ],
        }
        return self.last_result

    # ------------------------------------------------------------------ #
    async def _replay_symbol(self, symbol: str, timeframe: str,
                             count: int, warmup: int) -> dict[str, Any]:
        candles: list[Candle] = await self.engine.broker.get_candles(
            symbol, timeframe, count)
        if len(candles) < warmup + 20:
            return {"trades": [], "summary": {"symbol": symbol, "total_r": 0.0,
                                              "trades": 0}}

        from app.agents.candlestick import CandlestickAgent
        from app.agents.derivatives import DerivativesAgent  # noqa: F401 (registry)

        agent = CandlestickAgent(self.cfg)
        tech = self.cfg.get("technical", {}) or {}
        min_score = float(self.cfg.get("consensus.min_composite_score", 0.35))
        min_rr = float(self.cfg.get("risk.min_risk_reward", 2.0))
        atr_mult = float(self.cfg.get("risk.atr_stop_multiplier", 1.5))
        meta = self.cfg.instrument_meta(symbol)

        trades: list[dict[str, Any]] = []
        open_trade: dict[str, Any] | None = None

        for i in range(warmup, len(candles)):
            bar = candles[i]

            # ---- manage an open position first ----
            if open_trade:
                long = open_trade["side"] == "BUY"
                hit_stop = bar.low <= open_trade["stop"] if long else bar.high >= open_trade["stop"]
                hit_tgt = bar.high >= open_trade["target"] if long else bar.low <= open_trade["target"]

                if hit_stop and hit_tgt:
                    # Ambiguous bar — resolve against the trade. Pessimism here
                    # is the only defensible default without tick data.
                    hit_tgt = False
                if hit_stop or hit_tgt:
                    exit_px = open_trade["stop"] if hit_stop else open_trade["target"]
                    direction = 1 if long else -1
                    risk = abs(open_trade["entry"] - open_trade["stop"]) or 1.0
                    r = (exit_px - open_trade["entry"]) * direction / risk
                    trades.append({
                        **open_trade,
                        "exit": round(exit_px, 2),
                        "exit_ts": bar.ts.isoformat(),
                        "bars_held": i - open_trade["_i"],
                        "outcome": "TARGET" if hit_tgt else "STOP",
                        "r_multiple": round(r, 2),
                        "pnl_per_unit": round((exit_px - open_trade["entry"]) * direction, 2),
                    })
                    open_trade = None
                continue

            # ---- look for a new setup, using only bars up to `i` ----
            window = candles[: i + 1]
            df = ta.candles_to_df(window)
            snapshot = ta.compute_all(df, tech)
            if not snapshot:
                continue
            snapshot["patterns"] = pattern_mod.scan(df, tech.get("patterns_enabled"))

            ctx = MarketContext(symbol=symbol, cycle_id=f"replay-{i}")
            ctx.indicators = {
                "primary": snapshot,
                "by_timeframe": {timeframe: snapshot},
                "mtf_alignment": {"aligned": False, "direction": 0},
            }
            ctx.quote = Quote(symbol=symbol, last_price=bar.close)

            report = agent.analyse_rules(ctx)
            if not report.data_available or abs(report.score) < min_score:
                continue

            entry = bar.close
            atr = snapshot.get("atr", 0.0)
            if atr <= 0:
                continue
            long = report.score > 0
            stop = entry - atr_mult * atr if long else entry + atr_mult * atr
            risk_pts = abs(entry - stop)
            target = entry + risk_pts * min_rr * (1 if long else -1)

            open_trade = {
                "_i": i,
                "symbol": symbol,
                "side": "BUY" if long else "SELL",
                "entry_ts": bar.ts.isoformat(),
                "entry": round(entry, 2),
                "stop": round(stop, 2),
                "target": round(target, 2),
                "score": report.score,
                "regime": snapshot.get("regime", "unknown"),
                "setup": ", ".join(p["name"] for p in snapshot["patterns"][:3]) or "score threshold",
                "lot_size": meta.get("lot_size", 1),
            }

        for t in trades:
            t.pop("_i", None)

        total_r = sum(t["r_multiple"] for t in trades)
        wins = sum(1 for t in trades if t["r_multiple"] > 0)
        return {
            "trades": trades,
            "summary": {
                "symbol": symbol,
                "trades": len(trades),
                "wins": wins,
                "win_rate": round(wins / len(trades) * 100, 1) if trades else 0.0,
                "total_r": round(total_r, 2),
                "avg_r": round(total_r / len(trades), 3) if trades else 0.0,
            },
        }

    def _provenance(self) -> dict[str, Any]:
        broker = self.engine.broker.name
        feed = getattr(self.engine.broker, "data_source", None)
        simulated = broker == "paper" and feed is None
        sources = getattr(feed, "sources", None) if feed else None
        return {
            "broker": broker,
            "simulated": simulated,
            "sources": sources or ([broker] if not simulated else []),
            "label": ("SIMULATED DATA — this is a mechanical demo on a synthetic "
                      "market, NOT real historical performance"
                      if simulated
                      else f"Real historical data via {', '.join(sources or [broker])}"),
        }
