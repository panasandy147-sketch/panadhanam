"""The pre-market universe screener: today's tradeable names, chosen before
the open from YESTERDAY's end-of-day bars, instead of scanning the whole
universe all session.

Run once a day at `screener.run_at` (09:00 IST; 09:00 ET on the US desk) over
the market's index universe. For each symbol, from its daily bars:

  ATR_pct         14-day ATR / close x 100
  RVOL            yesterday's volume / the 20-day average volume
  Is_NR7          yesterday's range (high - low) is smaller than each of the
                  prior 6 days' ranges
  Is_Inside_Day   yesterday's high < the day before's high AND its low > that
                  day's low
  Close_Location  (close - low) / (high - low)

Bands (config `screener`):
  A (top 5)   ATR_pct >= 2.0, RVOL >= 1.2, and NR7 or an inside day — a coiled
              name with participation; ranked by RVOL, highest first
  B (next 5)  ATR_pct >= 2.0, RVOL >= 1.5, and a close in the top 20% of the
              range (LONGS only) or the bottom 20% (SHORTS only); by RVOL

The result is `todays_watchlist.json` (in the runtime data folder), one entry
per market. The risk desk trades nothing else that day, and uses the bands
for its time windows: Band A in the morning, Band A/B VWAP pullbacks in the
afternoon, nothing in the midday freeze.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger("analysis.screener")


def watchlist_path() -> Path:
    from app.core import config as config_mod
    return config_mod.DATA_DIR / "todays_watchlist.json"


# --------------------------------------------------------------------------- #
# The metrics
# --------------------------------------------------------------------------- #
def _true_range(h: float, lo: float, prev_close: float | None) -> float:
    if prev_close is None:
        return h - lo
    return max(h - lo, abs(h - prev_close), abs(lo - prev_close))


def metrics(daily: list[Any], atr_days: int = 14, vol_days: int = 20) -> dict[str, Any] | None:
    """The five numbers for one symbol from its completed daily bars (oldest
    first; the LAST bar is yesterday). None without enough history."""
    bars = list(daily)
    if len(bars) < max(atr_days + 1, vol_days + 1, 7):
        return None
    y = bars[-1]
    trs = [_true_range(float(b.high), float(b.low), float(bars[i - 1].close))
           for i, b in enumerate(bars) if i > 0][-atr_days:]
    atr = sum(trs) / len(trs)
    close = float(y.close)
    prior_vol = [float(b.volume or 0) for b in bars[-vol_days - 1:-1]]
    avg_vol = sum(prior_vol) / len(prior_vol) if prior_vol else 0.0
    rng = float(y.high) - float(y.low)
    prior_ranges = [float(b.high) - float(b.low) for b in bars[-7:-1]]
    d2 = bars[-2]
    loc = (close - float(y.low)) / rng if rng > 0 else 0.5
    return {
        "atr_pct": round(atr / close * 100, 3) if close else 0.0,
        "rvol": round(float(y.volume or 0) / avg_vol, 3) if avg_vol else 0.0,
        "is_nr7": bool(prior_ranges) and all(rng < r for r in prior_ranges),
        "is_inside_day": float(y.high) < float(d2.high) and float(y.low) > float(d2.low),
        "close_location": round(loc, 3),
        "close": close, "atr": round(atr, 4),
        "day": _day(y),
    }


def _day(bar: Any) -> str:
    ts = getattr(bar, "ts", None)
    return ts.date().isoformat() if hasattr(ts, "date") else ""


def bands(cfg: Any, rows: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Band A and Band B from the per-symbol metrics."""
    g = cfg.get
    a_min_atr = float(g("screener.band_a.min_atr_pct", 2.0))
    a_min_rvol = float(g("screener.band_a.min_rvol", 1.2))
    a_size = int(g("screener.band_a.size", 5))
    b_min_atr = float(g("screener.band_b.min_atr_pct", 2.0))
    b_min_rvol = float(g("screener.band_b.min_rvol", 1.5))
    b_long = float(g("screener.band_b.long_close_location", 0.80))
    b_short = float(g("screener.band_b.short_close_location", 0.20))
    b_size = int(g("screener.band_b.size", 5))

    def row(sym: str, m: dict[str, Any], band: str, side: str, why: str) -> dict[str, Any]:
        return {"symbol": sym, "band": band, "side": side, "why": why, **m}

    a = sorted((s for s, m in rows.items()
                if m["atr_pct"] >= a_min_atr and m["rvol"] >= a_min_rvol
                and (m["is_nr7"] or m["is_inside_day"])),
               key=lambda s: -rows[s]["rvol"])[:a_size]
    band_a = [row(s, rows[s], "A", "BOTH",
                  f"ATR {rows[s]['atr_pct']:.1f}%, RVOL {rows[s]['rvol']:.2f}, "
                  + ("NR7" if rows[s]["is_nr7"] else "inside day")) for s in a]
    taken = set(a)
    b_pool = []
    for s, m in rows.items():
        if s in taken or m["atr_pct"] < b_min_atr or m["rvol"] < b_min_rvol:
            continue
        if m["close_location"] > b_long:
            b_pool.append((s, "LONG"))
        elif m["close_location"] < b_short:
            b_pool.append((s, "SHORT"))
    b_pool.sort(key=lambda x: -rows[x[0]]["rvol"])
    band_b = [row(s, rows[s], "B", side,
                  f"ATR {rows[s]['atr_pct']:.1f}%, RVOL {rows[s]['rvol']:.2f}, closed at "
                  f"{rows[s]['close_location']:.0%} of the range → {side.lower()}s only")
              for s, side in b_pool[:b_size]]
    return {"A": band_a, "B": band_b}


# --------------------------------------------------------------------------- #
# Running it and reading it back
# --------------------------------------------------------------------------- #
async def run(cfg: Any, broker: Any, today: date, symbols: list[str] | None = None
              ) -> dict[str, Any]:
    """Screen the universe on yesterday's bars and save today's watchlist."""
    import asyncio

    names = symbols or [w["symbol"] for w in cfg.watchlist()]
    count = int(cfg.get("screener.history_days", 40))

    async def one(sym: str) -> tuple[str, dict[str, Any] | None]:
        try:
            daily = await broker.get_candles(sym, "1d", count)
        except Exception as exc:                        # noqa: BLE001
            log.debug("no daily bars for %s: %s", sym, exc)
            return sym, None
        # Yesterday's EOD: drop a bar for today (a partial session) if present.
        done = [b for b in daily or [] if _day(b) and _day(b) < today.isoformat()]
        return sym, metrics(done)

    got = await asyncio.gather(*(one(s) for s in names))
    rows = {s: m for s, m in got if m}
    picked = bands(cfg, rows)
    entry = {
        "date": today.isoformat(), "market": str(cfg.active_market),
        "screened": len(rows), "universe": len(names),
        "at": datetime.now().isoformat(timespec="seconds"),
        "band_a": picked["A"], "band_b": picked["B"],
        "symbols": [r["symbol"] for r in picked["A"] + picked["B"]],
        "no_data": sorted(set(names) - set(rows)),
    }
    save(entry)
    log.info("screener %s: %d screened — Band A %s; Band B %s", entry["market"],
             len(rows), [r["symbol"] for r in picked["A"]] or "none",
             [f"{r['symbol']}({r['side'][0]})" for r in picked["B"]] or "none")
    return entry


def save(entry: dict[str, Any]) -> None:
    path = watchlist_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    data[entry["market"]] = entry
    try:
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save today's watchlist: %s", exc)


def load(market: str) -> dict[str, Any] | None:
    try:
        return json.loads(watchlist_path().read_text(encoding="utf-8")).get(market)
    except (OSError, ValueError):
        return None


def todays(cfg: Any, today: date) -> dict[str, Any] | None:
    """Today's screened list for the active market, or None (not run yet)."""
    entry = load(str(cfg.active_market))
    return entry if entry and entry.get("date") == today.isoformat() else None


def band_of(entry: dict[str, Any] | None, symbol: str) -> dict[str, Any] | None:
    for r in (entry or {}).get("band_a", []) + (entry or {}).get("band_b", []):
        if r["symbol"].upper() == symbol.upper():
            return r
    return None


def _minutes(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h) * 60 + int(m or 0)


def window(cfg: Any, now: datetime) -> str:
    """'morning', 'freeze', 'afternoon' or 'closed' on the market's clock."""
    g = cfg.get
    t = now.hour * 60 + now.minute
    if _minutes(g("screener.windows.morning_from", "09:30")) <= t < _minutes(
            g("screener.windows.morning_to", "11:15")):
        return "morning"
    if _minutes(g("screener.windows.morning_to", "11:15")) <= t < _minutes(
            g("screener.windows.afternoon_from", "13:30")):
        return "freeze"
    if _minutes(g("screener.windows.afternoon_from", "13:30")) <= t < _minutes(
            g("screener.windows.afternoon_to", "14:45")):
        return "afternoon"
    return "closed"


def is_vwap_pullback(indicators: dict[str, Any], long: bool, max_atr: float = 0.5) -> bool:
    """On the trend side of VWAP and pulled back to it: within `max_atr` ATR."""
    p = (indicators or {}).get("primary") or indicators or {}
    close = float(p.get("last_close") or p.get("close") or 0.0)
    vwap, atr = float(p.get("vwap") or 0.0), float(p.get("atr") or 0.0)
    if not (close and vwap and atr) or math.isnan(vwap):
        return False
    gap = (close - vwap) if long else (vwap - close)
    return 0 <= gap <= max_atr * atr
