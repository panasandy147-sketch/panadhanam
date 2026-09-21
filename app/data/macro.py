"""Global macro snapshot: GIFT Nifty, US futures, crude, DXY, USD/INR, VIX, FII/DII.

Uses Yahoo Finance's public quote endpoint (no key). If it's unreachable the
snapshot degrades to empty and the macro agent correctly reports
`data_available=False` — which the CMIO treats as abstention, never agreement.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

import httpx

from app.core.config import Config, get_config
from app.core.logging import get_logger
from app.core.models import MacroSnapshot

log = get_logger("data.macro")

YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"

# Our internal key -> Yahoo ticker
YF_MAP = {
    "gift_nifty": "^NSEI",       # NSE index as the practical proxy
    "us_sp500": "ES=F",
    "us_nasdaq": "NQ=F",
    "dow": "YM=F",
    "crude": "BZ=F",
    "dxy": "DX-Y.NYB",
    "usdinr": "INR=X",
    "india_vix": "^INDIAVIX",
    "nifty": "^NSEI",
    "sensex": "^BSESN",
}


class MacroCollector:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self._last: MacroSnapshot | None = None

    async def fetch(self) -> MacroSnapshot:
        # The profile names each market's own macro dashboard: India watches
        # GIFT Nifty, crude and USD/INR; the US watches ES, the 10Y and the VIX.
        entries = self.cfg.get("macro.global_indices") or []
        self._tickers = {e["key"]: e.get("yahoo") for e in entries if e.get("key")}
        self._labels = {e["key"]: e.get("label", e["key"]) for e in entries if e.get("key")}
        keys = list(self._tickers) or list(YF_MAP)

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            results = await asyncio.gather(
                *[self._one(client, k) for k in keys], return_exceptions=True)

        snap = MacroSnapshot()
        for key, res in zip(keys, results):
            if isinstance(res, Exception) or res is None:
                continue
            level, change = res
            snap.values[key] = level
            snap.changes_pct[key] = change

        # Whichever volatility gauge this market uses.
        snap.india_vix = snap.values.get("india_vix") or snap.values.get("vix")
        snap.labels = getattr(self, "_labels", {})
        snap.notes = self._interpret(snap)
        self._last = snap
        return snap

    async def _one(self, client: httpx.AsyncClient, key: str) -> tuple[float, float] | None:
        ticker = getattr(self, "_tickers", {}).get(key) or YF_MAP.get(key)
        if not ticker:
            return None
        try:
            r = await client.get(YF_CHART.format(sym=ticker), params={"range": "2d", "interval": "1d"})
            if r.status_code != 200:
                return None
            meta = r.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None or not prev:
                return None
            return round(float(price), 2), round((price - prev) / prev * 100, 2)
        except Exception as exc:
            log.debug("macro fetch %s failed: %s", key, exc)
            return None

    def _interpret(self, snap: MacroSnapshot) -> list[str]:
        return interpret(snap, self.cfg)

    @property
    def last(self) -> MacroSnapshot | None:
        return self._last


def interpret(snap: MacroSnapshot, cfg: Config) -> list[str]:
    """Turn raw levels into the sentences a macro analyst would actually say."""
    notes: list[str] = []
    ch = snap.changes_pct

    gift = ch.get("gift_nifty")
    if gift is None and cfg.active_market == "US":
        # The US "pre-open read" is the S&P future, not GIFT Nifty.
        sp = ch.get("us_sp500")
        if sp is not None:
            if sp > 0.3:
                notes.append(f"S&P futures +{sp:.2f}% — gap-up bias into the open")
            elif sp < -0.3:
                notes.append(f"S&P futures {sp:.2f}% — gap-down bias into the open")
            else:
                notes.append(f"S&P futures {sp:+.2f}% — flat open expected")
        ten_year = ch.get("us10y")
        if ten_year is not None and abs(ten_year) > 1.5:
            notes.append(f"10Y yield {ten_year:+.2f}% — "
                         f"{'a headwind for growth names' if ten_year > 0 else 'supportive for duration'}")
    if gift is not None:
        if gift > 0.4:
            notes.append(f"GIFT Nifty proxy +{gift:.2f}% — gap-up bias into the open")
        elif gift < -0.4:
            notes.append(f"GIFT Nifty proxy {gift:.2f}% — gap-down bias into the open")
        else:
            notes.append(f"GIFT Nifty proxy {gift:+.2f}% — flat open expected")

    sp = ch.get("us_sp500")
    nq = ch.get("us_nasdaq")
    if sp is not None and nq is not None:
        if sp > 0.3 and nq > 0.3:
            notes.append("US futures firm overnight — risk-on backdrop")
        elif sp < -0.3 and nq < -0.3:
            notes.append("US futures soft overnight — risk-off backdrop")

    crude = snap.values.get("crude")
    crude_ch = ch.get("crude")
    if crude and crude_ch is not None:
        if crude_ch > 2:
            notes.append(f"Brent +{crude_ch:.1f}% to {crude:.1f} — an import-cost headwind for India")
        elif crude_ch < -2:
            notes.append(f"Brent {crude_ch:.1f}% to {crude:.1f} — a tailwind for India's deficit")

    dxy_ch = ch.get("dxy")
    if dxy_ch is not None and dxy_ch > 0.4:
        notes.append(f"Dollar index +{dxy_ch:.2f}% — pressure on EM flows")

    if cfg.active_market == "US":
        gold = ch.get("gold")
        if gold is not None and abs(gold) > 1.5:
            notes.append(f"Gold {gold:+.2f}% — "
                         f"{'risk-off rotation' if gold > 0 else 'risk appetite returning'}")

    usdinr = snap.values.get("usdinr")
    usdinr_ch = ch.get("usdinr")
    if usdinr and usdinr_ch is not None and usdinr_ch > 0.3:
        notes.append(f"USD/INR up {usdinr_ch:.2f}% to {usdinr:.2f} — rupee weakness, FII headwind")

    vix = snap.india_vix
    if vix:
        label = "India VIX" if cfg.active_market == "IN" else "VIX"
        panic = float(cfg.get("macro.vix_panic_level", 20.0))
        calm = float(cfg.get("macro.vix_calm_level", 12.0))
        if vix >= panic:
            notes.append(f"{label} {vix:.1f} above {panic} — elevated fear, cut position size")
        elif vix <= calm:
            notes.append(f"{label} {vix:.1f} below {calm} — complacency, watch for volatility expansion")
        else:
            notes.append(f"{label} {vix:.1f} — normal volatility regime")

    return notes or ["Macro data unavailable this cycle"]


def macro_bias_score(snap: MacroSnapshot, cfg: Config) -> tuple[float, list[str]]:
    """Deterministic macro score used as the no-LLM fallback and as an LLM prior."""
    if not snap.changes_pct:
        return 0.0, []

    ch = snap.changes_pct
    reasons: list[str] = []
    score = 0.0

    for key, weight in (("gift_nifty", 0.35), ("us_sp500", 0.2), ("us_nasdaq", 0.15)):
        val = ch.get(key)
        if val is not None:
            contribution = max(-1.0, min(1.0, val / 1.0)) * weight
            score += contribution
            if abs(val) > 0.3:
                reasons.append(f"{key} {val:+.2f}%")

    # Crude and the dollar are inverse for India.
    for key, weight in (("crude", -0.12), ("dxy", -0.10), ("usdinr", -0.08)):
        val = ch.get(key)
        if val is not None:
            score += max(-1.0, min(1.0, val / 2.0)) * weight
            if abs(val) > 1.0:
                reasons.append(f"{key} {val:+.2f}%")

    vix = snap.india_vix
    if vix:
        panic = float(cfg.get("macro.vix_panic_level", 20.0))
        if vix >= panic:
            score -= 0.2
            reasons.append(f"VIX {vix:.1f} elevated")

    return round(max(-1.0, min(1.0, score)), 3), reasons


class MacroReplay:
    """The macro dashboard as it stood at each moment of a past session.

    Practice Day replays a real day bar by bar. Handing the macro analyst
    *today's* numbers while it looks at last Tuesday's chart would be lookahead
    of the worst kind — the answer leaking into the question. So this fetches
    the same macro tickers' intraday bars for the replayed day and serves the
    last print at or before the replay clock, with the change measured against
    that day's own previous close.

    If the tape cannot be loaded the snapshot is `None`, the macro analyst
    abstains, and the desk votes with one fewer voice. That is the honest
    failure mode.
    """

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self._labels: dict[str, str] = {}
        # key -> sorted [(ts, level)] for the replayed day
        self._tape: dict[str, list[tuple[datetime, float]]] = {}
        self._prev_close: dict[str, float] = {}

    @property
    def loaded(self) -> bool:
        return bool(self._tape)

    @property
    def keys(self) -> list[str]:
        return sorted(self._tape)

    async def load(self, day: date, interval: str = "5m") -> bool:
        """Pull each macro ticker's bars for `day` plus its prior daily close."""
        entries = self.cfg.get("macro.global_indices") or []
        tickers = {e["key"]: e.get("yahoo") for e in entries if e.get("key")}
        self._labels = {e["key"]: e.get("label", e["key"]) for e in entries if e.get("key")}
        if not tickers:
            tickers = dict(YF_MAP)
            self._labels = {k: k for k in tickers}

        self._tape, self._prev_close = {}, {}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            results = await asyncio.gather(
                *[self._one(client, t, day, interval) for t in tickers.values()],
                return_exceptions=True)

        for key, res in zip(tickers, results):
            if isinstance(res, Exception) or not res:
                continue
            bars, prev = res
            if not bars or not prev:
                continue
            self._tape[key] = bars
            self._prev_close[key] = prev

        log.info("macro replay tape for %s: %d/%d tickers",
                 day.isoformat(), len(self._tape), len(tickers))
        return self.loaded

    async def _one(self, client: httpx.AsyncClient, ticker: str | None,
                   day: date, interval: str
                   ) -> tuple[list[tuple[datetime, float]], float | None] | None:
        if not ticker:
            return None
        try:
            intraday = await client.get(YF_CHART.format(sym=ticker),
                                        params={"range": "60d", "interval": interval})
            daily = await client.get(YF_CHART.format(sym=ticker),
                                     params={"range": "6mo", "interval": "1d"})
            if intraday.status_code != 200 or daily.status_code != 200:
                return None
            bars = [(ts, px) for ts, px in _closes(intraday.json())
                    if ts.date() == day]
            prev = next((px for ts, px in reversed(_closes(daily.json()))
                         if ts.date() < day), None)
            return bars, prev
        except Exception as exc:
            log.debug("macro replay %s failed: %s", ticker, exc)
            return None

    def snapshot_at(self, ts: datetime) -> MacroSnapshot | None:
        """The macro picture as of `ts`, using nothing printed after it."""
        if not self._tape:
            return None
        cutoff = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

        snap = MacroSnapshot(fetched_at=cutoff, labels=dict(self._labels))
        for key, bars in self._tape.items():
            level = next((px for bar_ts, px in reversed(bars) if bar_ts <= cutoff), None)
            if level is None:
                continue
            prev = self._prev_close.get(key)
            snap.values[key] = round(level, 2)
            if prev:
                snap.changes_pct[key] = round((level - prev) / prev * 100, 2)

        if not snap.values:
            return None
        snap.india_vix = snap.values.get("india_vix") or snap.values.get("vix")
        snap.notes = interpret(snap, self.cfg)
        return snap


def _closes(payload: dict) -> list[tuple[datetime, float]]:
    """(timestamp, close) pairs from a Yahoo chart response, nulls dropped."""
    try:
        result = payload["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for epoch, close in zip(stamps, closes):
        if close is None:
            continue    # a gap, not a zero — filling it would invent a price
        out.append((datetime.fromtimestamp(epoch, tz=timezone.utc), float(close)))
    return sorted(out)
