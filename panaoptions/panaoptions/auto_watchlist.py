"""The auto watchlist: the day's top names, picked before the open and
re-checked every hour.

Where the names come from (all free, no account, each one best-effort):

  most_actives, day_gainers, day_losers   Yahoo Finance's own screeners
  trending                                what Yahoo's readers are looking at
  analyst rating                          Yahoo's averageAnalystRating — the
                                          mean of the brokers' ratings, 1.0
                                          (Strong Buy) to 5.0 (Strong Sell)
  always_consider                         a liquid base list from settings, so
                                          the ranking never has nothing to rank

India ranks the names in `data.lot_sizes` only: without a known lot the desk
cannot size a trade, so a name outside it could be picked but never bought.

How a name is scored, each part 0..1, weights in settings:

  trend      the size of today's move (pre-market: the pre-market move)
  volume     relative volume — today's pace against the average day
  analysts   how strongly the brokers lean, and more when they lean the way
             the stock is moving
  buzz       how many of the lists above it turned up on
  liquidity  average daily volume — tight option spreads follow it

The rules that keep it from doing damage:

  * A symbol with an open position is NEVER dropped. It stays until the trade
    is closed, and is only replaceable at the next hourly refresh after that.
  * Pinned names (SPY, QQQ; NIFTY, BANKNIFTY) always stay.
  * Hourly refreshes change the list only when a newcomer beats the weakest
    name by `swap_margin` — a list that churns every hour re-screens every
    hour and trades nothing.
  * If every source fails, the list in force stays. It is never emptied.
  * A list typed on the dashboard turns auto off; the Auto button turns it
    back on.

Like the typed watchlist, this chooses what to LOOK at, never what to buy:
every rule still has to agree before a position is opened.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from panaoptions import clock
from panaoptions.logging import get_logger

log = get_logger("auto_watchlist")

MODES = ("auto", "custom", "config")
DEFAULT_WEIGHTS = {"trend": 0.25, "volume": 0.25, "analysts": 0.20,
                   "buzz": 0.15, "liquidity": 0.15}
# Lists that count as "the market is talking about it". The base list and
# the pins are candidates, not evidence.
BUZZ_SOURCES = {"most_actives", "day_gainers", "day_losers", "trending"}


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    symbol: str
    price: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0
    avg_volume: float = 0.0
    rating: float | None = None        # 1.0 Strong Buy .. 5.0 Strong Sell
    rating_text: str = ""
    market_cap: float = 0.0
    quote_type: str = ""
    premarket: bool = False            # change_pct is the pre-market move
    sources: set[str] = field(default_factory=set)
    score: float = 0.0
    rvol: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def has_quote(self) -> bool:
        return self.price > 0

    def merge(self, other: Candidate) -> None:
        """Fill what this one lacks from `other`, and pool the sources."""
        self.sources |= other.sources
        for name in ("price", "change_pct", "volume", "avg_volume", "market_cap"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        if self.rating is None and other.rating is not None:
            self.rating, self.rating_text = other.rating, other.rating_text
        self.quote_type = self.quote_type or other.quote_type
        self.premarket = self.premarket or other.premarket

    def row(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "score": round(self.score, 3),
                "price": round(self.price, 2), "change_pct": round(self.change_pct, 2),
                "rvol": round(self.rvol, 2), "rating": self.rating,
                "rating_text": self.rating_text, "sources": sorted(self.sources),
                "reasons": list(self.reasons)}


def parse_rating(raw: Any) -> tuple[float | None, str]:
    """'1.8 - Buy' -> (1.8, 'Buy'). Anything else -> (None, '')."""
    if isinstance(raw, int | float):
        value = float(raw)
        return (value, "") if 1.0 <= value <= 5.0 else (None, "")
    text = str(raw or "").strip()
    if not text:
        return None, ""
    head, _, tail = text.partition("-")
    try:
        value = float(head.strip())
    except ValueError:
        return None, ""
    if not 1.0 <= value <= 5.0:
        return None, ""
    return value, tail.strip()


def _num(q: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = q.get(key)
        if isinstance(value, dict):                 # formatted=true shape
            value = value.get("raw")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return 0.0


def parse_quote(q: dict[str, Any], symbol: str | None = None,
                source: str = "") -> Candidate | None:
    """One Yahoo quote object (screener or v7 quote) as a Candidate."""
    sym = (symbol or str(q.get("symbol") or "")).upper()
    if not sym:
        return None
    state = str(q.get("marketState") or "").upper()
    pre_change = _num(q, "preMarketChangePercent")
    premarket = state in {"PRE", "PREPRE"} and bool(pre_change)
    rating, text = parse_rating(q.get("averageAnalystRating"))
    return Candidate(
        symbol=sym,
        price=_num(q, "preMarketPrice") if premarket else _num(q, "regularMarketPrice"),
        change_pct=pre_change if premarket else _num(q, "regularMarketChangePercent"),
        volume=_num(q, "regularMarketVolume"),
        avg_volume=_num(q, "averageDailyVolume10Day", "averageDailyVolume3Month"),
        rating=rating, rating_text=text,
        market_cap=_num(q, "marketCap"),
        quote_type=str(q.get("quoteType") or "").upper(),
        premarket=premarket,
        sources={source} if source else set())


def from_daily(symbol: str, daily: list[Any], source: str = "chart") -> Candidate | None:
    """A Candidate from daily candles, for when the quote service refuses.

    No analyst rating this way — the chart does not carry one.
    """
    bars = [c for c in daily or [] if getattr(c, "close", 0)]
    if len(bars) < 2:
        return None
    last, prev = bars[-1], bars[-2]
    history = [c.volume for c in bars[:-1] if c.volume > 0][-10:]
    return Candidate(
        symbol=symbol.upper(), price=float(last.close),
        change_pct=(last.close - prev.close) / prev.close * 100 if prev.close else 0.0,
        volume=float(last.volume or 0.0),
        avg_volume=sum(history) / len(history) if history else 0.0,
        sources={source})


# --------------------------------------------------------------------------- #
# Scoring and selection
# --------------------------------------------------------------------------- #
def session_fraction(cfg: Any, now: datetime) -> float:
    """How much of the regular session has traded. 1.0 outside it, because
    then the day's volume is a whole day's (yesterday's, before the open)."""
    opens = clock.parse_hhmm(str(cfg.get("session.market_open", "09:30")))
    closes = clock.parse_hhmm(str(cfg.get("session.market_close", "16:00")))
    total = (closes.hour * 60 + closes.minute) - (opens.hour * 60 + opens.minute)
    minutes = (now.hour * 60 + now.minute) - (opens.hour * 60 + opens.minute)
    if total <= 0 or minutes <= 0 or minutes >= total:
        return 1.0
    return max(minutes / total, 1 / total)


def score(c: Candidate, cfg: Any, fraction: float = 1.0) -> float:
    """Score one candidate in place: sets c.score, c.rvol and c.reasons."""
    weights = {**DEFAULT_WEIGHTS, **(cfg.get("auto_watchlist.weights", {}) or {})}
    move_full = float(cfg.get("auto_watchlist.move_full_pct", 3.0)) or 3.0
    liquid_full = float(cfg.get("auto_watchlist.liquidity_full", 20_000_000)) or 1.0
    reasons: list[str] = []

    move = abs(c.change_pct)
    trend = min(move / move_full, 1.0)
    when = "pre-market" if c.premarket else "today"
    if move >= 0.1:
        reasons.append(f"{c.change_pct:+.1f}% {when}")

    c.rvol = (c.volume / (c.avg_volume * fraction)) if c.avg_volume > 0 and c.volume > 0 else 0.0
    volume = min(c.rvol / 2.0, 1.0)
    if c.rvol >= 1.2:
        reasons.append(f"RVOL {c.rvol:.1f}x")

    if c.rating is not None:
        lean = max(-1.0, min((3.0 - c.rating) / 2.0, 1.0))    # + bullish, - bearish
        moving = 1 if c.change_pct > 0.25 else -1 if c.change_pct < -0.25 else 0
        leaning = 1 if lean > 0.05 else -1 if lean < -0.05 else 0
        agree = 1.0 if moving and moving == leaning else 0.6 if not moving else 0.4
        analysts = abs(lean) * agree
        label = f"analysts {c.rating:.1f}" + (f" {c.rating_text}" if c.rating_text else "")
        if moving and leaning:
            label += ", with the move" if moving == leaning else ", against the move"
        reasons.append(label)
    else:
        analysts = 0.3              # ETFs and indices carry no rating: neutral
    buzz_hits = sorted(c.sources & BUZZ_SOURCES)
    buzz = min(len(buzz_hits) / 2.0, 1.0)
    if buzz_hits:
        reasons.append("on " + ", ".join(s.replace("_", " ") for s in buzz_hits))
    liquidity = min(c.avg_volume / liquid_full, 1.0) if c.avg_volume > 0 else 0.0

    c.score = round(weights["trend"] * trend + weights["volume"] * volume
                    + weights["analysts"] * analysts + weights["buzz"] * buzz
                    + weights["liquidity"] * liquidity, 4)
    c.reasons = reasons
    return c.score


def eligible(c: Candidate, cfg: Any, pool: set[str] | None = None) -> str:
    """'' when the name may be listed, else why not."""
    from panaoptions.watchlist import _VALID

    if not _VALID.match(c.symbol):
        return "not a desk ticker"
    if pool is not None and c.symbol not in pool:
        return "no known lot size"
    if not c.has_quote:
        return "no quote"
    kinds = [str(k).upper() for k in cfg.get("auto_watchlist.quote_types", []) or []]
    if kinds and c.quote_type and c.quote_type not in kinds:
        return f"{c.quote_type.lower()}, not a stock or ETF"
    low = float(cfg.get("auto_watchlist.min_price", 0) or 0)
    high = float(cfg.get("auto_watchlist.max_price", 0) or 0)
    if low and c.price < low:
        return f"price {c.price:.2f} under {low:g}"
    if high and c.price > high:
        return f"price {c.price:.2f} over {high:g} — contracts too dear"
    min_avg = float(cfg.get("auto_watchlist.min_avg_volume", 0) or 0)
    if min_avg and c.avg_volume and c.avg_volume < min_avg:
        return f"average volume {c.avg_volume:,.0f} under {min_avg:,.0f}"
    min_cap = float(cfg.get("auto_watchlist.min_market_cap", 0) or 0)
    if min_cap and c.quote_type == "EQUITY" and c.market_cap and c.market_cap < min_cap:
        return f"market cap under {min_cap / 1e9:g}B"
    return ""


@dataclass
class Selection:
    symbols: list[str]
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)      # kept: position open
    notes: list[str] = field(default_factory=list)
    changed: bool = False


def _dedup(items: list[str]) -> list[str]:
    out: list[str] = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def select(current: list[str], candidates: dict[str, Candidate], held: list[str],
           cfg: Any, *, fresh: bool = False, fallback: list[str] | None = None,
           judged: set[str] | None = None) -> Selection:
    """The new list. `fresh` is the morning build: a full re-rank, no margin.

    `candidates` are the scored names that passed `eligible`; `current` is the
    list in force; `judged` every name a quote came back for, eligible or not.
    An incumbent that was judged and refused is dropped; one that was never
    judged (its quote did not come back) keeps its place rather than being
    dropped for a data gap. Held names and the pins always stay.
    """
    judged = set(candidates) if judged is None else judged
    size = int(cfg.get("auto_watchlist.size", 10))
    margin = 0.0 if fresh else float(cfg.get("auto_watchlist.swap_margin", 0.10))
    pinned = [str(p).upper() for p in cfg.get("auto_watchlist.pinned", []) or []]
    held = _dedup([s.upper() for s in held])
    must = _dedup(pinned + held)
    notes: list[str] = []

    if not candidates:
        # Nothing to judge with: keep what is in force, never empty the list.
        keep = _dedup(must + (current or fallback or []))
        notes.append("no source answered — keeping the list in force")
        return Selection(symbols=keep, held=[s for s in held if s in keep],
                         notes=notes, changed=keep != current)

    slots = max(size - len(must), 0)
    ranked = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    if fresh:
        chosen = [c.symbol for c in ranked if c.symbol not in must][:slots]
    else:
        # Incumbents keep their place unless a newcomer clearly beats them.
        chosen = [s for s in current if s not in must
                  and (s in candidates or s not in judged)]
        dropped = [s for s in current if s not in must and s not in chosen]
        if dropped:
            notes.append(f"dropped {', '.join(dropped)}: no longer qualifies")

        def worth(s: str) -> float:
            c = candidates.get(s)
            return c.score if c else -1.0

        chosen.sort(key=worth, reverse=True)
        chosen = chosen[:slots]
        challengers = [c for c in ranked if c.symbol not in must and c.symbol not in chosen]
        while challengers and len(chosen) < slots:
            chosen.append(challengers.pop(0).symbol)
        while challengers:
            # A name with no quote this hour is not judged on a data gap.
            swappable = [s for s in chosen if s in candidates]
            if not swappable:
                break
            weakest = min(swappable, key=worth)
            best = challengers[0]
            if best.score <= worth(weakest) + margin:
                break
            notes.append(f"{best.symbol} ({best.score:.2f}) replaces "
                         f"{weakest} ({max(worth(weakest), 0):.2f})")
            chosen[chosen.index(weakest)] = challengers.pop(0).symbol

    symbols = _dedup(must + chosen)
    # Short of names (a thin day, a source down): top up from what was there.
    for s in (current or []) + (fallback or []):
        if len(symbols) >= size:
            break
        if s not in symbols:
            symbols.append(s)
    added = [s for s in symbols if s not in (current or [])]
    removed = [s for s in current or [] if s not in symbols]
    kept_held = [s for s in held if s in symbols]
    if kept_held:
        notes.append(f"kept {', '.join(kept_held)} — position open, replaceable "
                     f"only after it closes")
    return Selection(symbols=symbols, added=added, removed=removed, held=kept_held,
                     notes=notes, changed=symbols != (current or []))


# --------------------------------------------------------------------------- #
# Where the candidates come from
# --------------------------------------------------------------------------- #
SCREENER = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
TRENDING = "https://query1.finance.yahoo.com/v1/finance/trending/{region}"
QUOTE = "https://query1.finance.yahoo.com/v7/finance/quote"
CRUMB = "https://query1.finance.yahoo.com/v1/test/getcrumb"
COOKIE = "https://fc.yahoo.com"


class YahooDiscovery:
    """Yahoo's screeners, trending list and batch quotes (with analyst ratings).

    Every request is best-effort: one refusing does not sink the others, and
    `status` says which answered.
    """

    def __init__(self, timeout: float = 10.0, transport: Any = None) -> None:
        self.timeout = timeout
        self.transport = transport
        self._client: Any = None
        self._crumb: str = ""
        self.status: dict[str, str] = {}

    async def _http(self) -> Any:
        if self._client is None:
            import httpx

            from panaoptions.data.feed import HEADERS
            kwargs: dict[str, Any] = {"headers": HEADERS, "timeout": self.timeout,
                                      "follow_redirects": True}
            if self.transport is not None:
                kwargs["transport"] = self.transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _crumb_for(self) -> str:
        if self._crumb:
            return self._crumb
        http = await self._http()
        try:
            await http.get(COOKIE)                  # sets the session cookie
            r = await http.get(CRUMB)
            text = r.text.strip() if r.status_code == 200 else ""
            if text and "<" not in text and len(text) < 64:
                self._crumb = text
        except Exception as exc:                    # noqa: BLE001
            log.debug("crumb failed: %s", exc)
        return self._crumb

    async def _json(self, name: str, url: str, **params: Any) -> dict[str, Any] | None:
        http = await self._http()
        try:
            r = await http.get(url, params=params)
            if r.status_code != 200:
                self.status[name] = f"HTTP {r.status_code}"
                return None
            return r.json()
        except Exception as exc:                    # noqa: BLE001
            self.status[name] = type(exc).__name__
            return None

    async def screener(self, scr_id: str, count: int = 25) -> list[Candidate]:
        params: dict[str, Any] = {"scrIds": scr_id, "count": count, "formatted": "false"}
        crumb = await self._crumb_for()
        if crumb:
            params["crumb"] = crumb
        data = await self._json(scr_id, SCREENER, **params)
        quotes = (((data or {}).get("finance") or {}).get("result") or [{}])[0].get("quotes") or []
        out = [c for c in (parse_quote(q, source=scr_id) for q in quotes) if c]
        if data is not None:
            self.status[scr_id] = f"{len(out)} names"
        return out

    async def trending(self, region: str, count: int = 25) -> list[str]:
        data = await self._json("trending", TRENDING.format(region=region), count=count)
        quotes = (((data or {}).get("finance") or {}).get("result") or [{}])[0].get("quotes") or []
        out = [str(q.get("symbol") or "").upper() for q in quotes if q.get("symbol")]
        if data is not None:
            self.status["trending"] = f"{len(out)} names"
        return out

    async def quotes(self, names: list[str]) -> dict[str, dict[str, Any]]:
        """Yahoo name -> quote object, in batches of 50."""
        out: dict[str, dict[str, Any]] = {}
        crumb = await self._crumb_for()
        for i in range(0, len(names), 50):
            batch = names[i:i + 50]
            params: dict[str, Any] = {"symbols": ",".join(batch), "formatted": "false"}
            if crumb:
                params["crumb"] = crumb
            data = await self._json("quote", QUOTE, **params)
            for q in ((data or {}).get("quoteResponse") or {}).get("result") or []:
                if q.get("symbol"):
                    out[str(q["symbol"]).upper()] = q
        if out:
            self.status["quote"] = f"{len(out)} quotes"
        return out

    async def candidates(self, cfg: Any, pool: list[str]) -> dict[str, Candidate]:
        """Every name worth ranking, desk symbol -> Candidate."""
        self.status = {}
        market = str(getattr(cfg, "market", "US")).upper()
        region = str(cfg.get("auto_watchlist.trending_region", market))
        screeners = list(cfg.get("auto_watchlist.screeners", []) or [])
        mapping = {str(k).upper(): str(v).upper()
                   for k, v in (cfg.get("data.yahoo_symbols", {}) or {}).items()}
        suffix = str(cfg.get("data.yahoo_suffix", "") or "") if market != "US" else ""

        def yahoo_name(sym: str) -> str:
            if sym in mapping:
                return mapping[sym]
            return f"{sym}{suffix.upper()}" if suffix else sym

        def desk_name(name: str) -> str:
            for k, v in mapping.items():
                if v == name:
                    return k
            if suffix and name.endswith(suffix.upper()):
                return name[: -len(suffix)]
            return name

        found: dict[str, Candidate] = {}

        def add(c: Candidate) -> None:
            c.symbol = desk_name(c.symbol)
            if c.symbol in found:
                found[c.symbol].merge(c)
            else:
                found[c.symbol] = c

        lists = await asyncio.gather(*(self.screener(s) for s in screeners),
                                     self.trending(region), return_exceptions=True)
        for batch in lists[:-1]:
            if isinstance(batch, list):
                for c in batch:
                    add(c)
        trending = lists[-1] if isinstance(lists[-1], list) else []
        for name in trending:
            add(Candidate(symbol=name, sources={"trending"}))

        wanted = _dedup([s.upper() for s in pool] + [s for s, c in found.items()
                                                     if not c.has_quote or c.rating is None])
        quotes = await self.quotes([yahoo_name(s) for s in wanted])
        for name, q in quotes.items():
            c = parse_quote(q, symbol=desk_name(name))
            if c:
                add(c)
        return found


async def gather(discovery: Any, feed: Any, cfg: Any, pool: list[str],
                 now: datetime) -> tuple[dict[str, Candidate], dict[str, str]]:
    """Candidates from `discovery`, topped up from the desk's own chart feed
    for any pool name the quote service did not price."""
    try:
        found = await discovery.candidates(cfg, pool)
    except Exception as exc:                        # noqa: BLE001
        log.warning("auto watchlist sources failed: %s", exc)
        found = {}
    status = dict(getattr(discovery, "status", {}) or {})
    for s in pool:
        found.setdefault(s, Candidate(symbol=s))
        found[s].sources.add("pool")
    missing = [s for s, c in found.items() if not c.has_quote and s in pool]
    if missing and feed is not None:
        dailies = await asyncio.gather(*(feed.candles(s, "1d") for s in missing),
                                       return_exceptions=True)
        filled = 0
        for s, daily in zip(missing, dailies, strict=False):
            if isinstance(daily, Exception):
                continue
            c = from_daily(s, daily)
            if c:
                found[s].merge(c)
                filled += 1
        if filled:
            status["chart"] = f"{filled} priced from charts"
    return {s: c for s, c in found.items() if c.has_quote}, status


# --------------------------------------------------------------------------- #
# State on disk
# --------------------------------------------------------------------------- #
def state_path() -> Path:
    # Next to the typed watchlist, so each market keeps its own.
    from panaoptions import watchlist
    return Path(watchlist.STORE).parent / "auto_watchlist.json"


def load_state() -> dict[str, Any]:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save the auto watchlist: %s", exc)


def available(cfg: Any) -> bool:
    """Switched on in settings, and not turned off for this process."""
    import os
    if os.getenv("PANAOPTIONS_AUTO_WATCHLIST", "").strip().lower() in {"0", "off", "false", "no"}:
        return False
    return bool(cfg.get("auto_watchlist.enabled", False))


def mode(cfg: Any) -> str:
    """auto, custom (typed on the dashboard) or config (settings.yaml)."""
    from panaoptions import watchlist
    if not available(cfg):
        return "custom" if watchlist.load() else "config"
    saved = load_state().get("mode")
    return saved if saved in MODES else "auto"


def set_mode(value: str) -> None:
    if value not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    state = load_state()
    state["mode"] = value
    if value == "auto":
        state.pop("last_refresh", None)             # refresh at the next chance
    save_state(state)


def log_refresh(now: datetime, record: dict[str, Any]) -> None:
    """Every refresh, to journal/watchlist/<date>.jsonl, for the weekly review."""
    from panaoptions.journal import store as journal_store
    try:
        folder = journal_store.JOURNAL_DIR / "watchlist"
        folder.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"),
                 "market_time": now.isoformat(timespec="minutes"), **record}
        with (folder / f"{now.date().isoformat()}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        log.warning("could not log the watchlist refresh: %s", exc)


# --------------------------------------------------------------------------- #
# When
# --------------------------------------------------------------------------- #
def due(cfg: Any, now: datetime, state: dict[str, Any]) -> str:
    """'morning', 'hourly' or '' — what kind of refresh, if any, is due now."""
    if now.weekday() >= 5:
        return ""
    tz = cfg.timezone
    start = str(cfg.get("auto_watchlist.start", "08:45"))
    stop = str(cfg.get("auto_watchlist.stop", "15:00"))
    if not clock.at_or_after(tz, start, now) or clock.at_or_after(tz, stop, now):
        return ""
    if state.get("day") != now.date().isoformat():
        return "morning"
    last = state.get("last_refresh")
    if not last:
        return "hourly"
    try:
        then = datetime.fromisoformat(str(last))
    except ValueError:
        return "hourly"
    every = int(cfg.get("auto_watchlist.refresh_minutes", 60))
    return "hourly" if now - then >= timedelta(minutes=every) else ""


def next_refresh(cfg: Any, state: dict[str, Any]) -> str:
    last = state.get("last_refresh")
    if not last:
        return ""
    try:
        then = datetime.fromisoformat(str(last))
    except ValueError:
        return ""
    nxt = then + timedelta(minutes=int(cfg.get("auto_watchlist.refresh_minutes", 60)))
    stop = clock.parse_hhmm(str(cfg.get("auto_watchlist.stop", "15:00")))
    if nxt.timetz().replace(tzinfo=None) >= stop:
        return "tomorrow, before the open"
    return nxt.strftime("%H:%M")


def pool_for(cfg: Any, current: list[str], held: list[str]) -> tuple[list[str], set[str] | None]:
    """What to always rank, and (India) the only names allowed at all."""
    pinned = [str(s).upper() for s in cfg.get("auto_watchlist.pinned", []) or []]
    base = [str(s).upper() for s in cfg.get("auto_watchlist.always_consider", []) or []]
    restrict: set[str] | None = None
    if bool(cfg.get("auto_watchlist.lot_sizes_only", False)):
        restrict = {str(s).upper() for s in (cfg.get("data.lot_sizes", {}) or {})}
        base = base + sorted(restrict)
    return _dedup(pinned + base + list(current) + list(held)), restrict


_liquidity_seen: dict[tuple[str, str], str] = {}


async def options_liquidity(feed: Any, cfg: Any, c: Candidate) -> str:
    """'' when the name's near-the-money options can be traded — at least one
    call AND one put with a bid and a spread within
    contracts.max_spread_pct_of_mid (7%), in the desk's expiry window — else
    why not. A chain that cannot be read is not held against the name."""
    min_dte = int(cfg.get("contracts.min_dte", 0) or 0)
    max_dte = int(cfg.get("contracts.max_dte", 14) or 14)
    cap = float(cfg.get("contracts.max_spread_pct_of_mid", 7.0) or 7.0)
    try:
        chain = await feed.chain_for_window(c.symbol, c.price, min_dte, max_dte)
    except Exception:                                   # noqa: BLE001 - fail open
        return ""
    if not chain:
        return ""
    near = [k for k in chain
            if (k.delta is not None and 0.25 <= abs(k.delta) <= 0.60)
            or (k.delta is None and c.price and abs(k.strike - c.price) <= 0.05 * c.price)]
    ok = {k.right.value for k in near if (k.bid or 0) > 0 and k.spread_pct_of_mid <= cap}
    if {"CALL", "PUT"} <= ok:
        return ""
    best = min((k.spread_pct_of_mid for k in near if (k.bid or 0) > 0), default=None)
    missing = " and ".join(sorted({"CALL", "PUT"} - ok)).lower()
    return (f"options too thin to trade: no {missing} near the money within the "
            f"{cap:g}% spread rule" + (f" (tightest {best:.0f}%)" if best is not None else
                                        " (no bids)"))


async def drop_illiquid(feed: Any, cfg: Any, ranked: dict[str, Candidate],
                        keep: list[str], day: str) -> tuple[dict[str, Candidate], dict[str, str]]:
    """Check the best-scored names' options (auto_watchlist.liquidity_check_top,
    default 2x the list size) and drop the thin ones. Pinned and held names are
    never checked; one answer per name per day."""
    import asyncio
    pinned = {str(s).upper() for s in cfg.get("auto_watchlist.pinned", []) or []}
    skip = pinned | {str(s).upper() for s in keep}
    top = int(cfg.get("auto_watchlist.liquidity_check_top", 0) or 0) or \
        2 * int(cfg.get("auto_watchlist.size", 10))
    order = sorted(ranked.values(), key=lambda c: c.score, reverse=True)
    todo = [c for c in order[:top] if c.symbol not in skip
            and (day, c.symbol) not in _liquidity_seen]

    async def one(c: Candidate) -> None:
        _liquidity_seen[(day, c.symbol)] = await options_liquidity(feed, cfg, c)

    if todo:
        try:
            await asyncio.wait_for(asyncio.gather(*(one(c) for c in todo)),
                                   timeout=float(cfg.get("auto_watchlist.liquidity_timeout", 30)))
        except TimeoutError:
            pass                                      # unchecked names stay in
    thin = {c.symbol: _liquidity_seen[(day, c.symbol)] for c in order[:top]
            if _liquidity_seen.get((day, c.symbol))}
    return {s: c for s, c in ranked.items() if s not in thin}, thin


def rank(found: dict[str, Candidate], cfg: Any, now: datetime,
         restrict: set[str] | None) -> tuple[dict[str, Candidate], dict[str, str]]:
    """Score every priced name; return the eligible ones and why others fell."""
    fraction = session_fraction(cfg, now)
    ok: dict[str, Candidate] = {}
    refused: dict[str, str] = {}
    for sym, c in found.items():
        why = eligible(c, cfg, restrict)
        if why:
            refused[sym] = why
            continue
        score(c, cfg, fraction)
        ok[sym] = c
    return ok, refused

