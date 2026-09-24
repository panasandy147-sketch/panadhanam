"""Tradier market data: the same interface as YahooFeed, with real greeks.

Why this exists: Yahoo serves option chains from a different host and API
path than charts, and that endpoint has become unreliable — it can refuse
every request while the chart feed works perfectly, which leaves the desk
screening, charting and firing setups all day without ever pricing a single
contract.

Two things Tradier does better for this app:

  * **Greeks come with the chain.** Yahoo serves no delta at all, so the
    Yahoo path computes it from implied volatility with Black-Scholes. That
    estimate is decent and it is still an estimate; every delta band in this
    app is then applied to a number nobody quoted. Tradier returns the
    greeks the market is actually using.
  * **The chain endpoint is the product**, not a scraped side door, so it
    does not quietly start returning 403.

An account is needed. The sandbox is free and serves delayed data with real
chains — which is exactly right for paper trading, where the strategies are
being tested rather than the latency.

    TRADIER_TOKEN=...            in panaoptions/.env
    data:
      provider: tradier
      tradier_env: sandbox       # or "production"
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from panaoptions.logging import get_logger
from panaoptions.models import Candle, OptionContract, OptionRight

log = get_logger("tradier")

BASES = {
    "sandbox": "https://sandbox.tradier.com/v1",
    "production": "https://api.tradier.com/v1",
}

# Tradier names its intraday intervals differently from the rest of this app.
_INTERVALS = {"1m": "1min", "5m": "5min", "15m": "15min"}

# How far back to ask for intraday bars. The strategies need 20+ bars of
# history plus the pre-market tape, and Tradier's timesales is date-bounded
# rather than range-bounded like Yahoo's chart endpoint.
_LOOKBACK_DAYS = {"1m": 2, "5m": 5, "15m": 10}


class TradierFeed:
    """Drop-in replacement for YahooFeed.

    Every method matches YahooFeed's signature and return type, so the desk,
    the screener and the strategies cannot tell which one they are holding.
    """

    def __init__(self, token: str, environment: str = "sandbox",
                 timeout: float = 15.0) -> None:
        self.token = (token or "").strip()
        self.environment = environment if environment in BASES else "sandbox"
        self.base = BASES[self.environment]
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self.connected = False
        self.options_available: bool | None = None
        self.options_error: str = ""
        self.chart_error: str = ""

    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> TradierFeed:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> bool:
        if not self.token:
            log.error("no Tradier token. Put TRADIER_TOKEN in panaoptions/.env "
                      "— the sandbox one is free and serves real chains.")
            return False

        self._client = httpx.AsyncClient(
            base_url=self.base,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/json"},
            timeout=self.timeout, follow_redirects=True)

        payload = await self._get("/markets/quotes", symbols="SPY")
        if not payload:
            log.error("Tradier unreachable or the token was rejected (%s). "
                      "Check TRADIER_TOKEN and that data.tradier_env matches "
                      "where the token came from — a sandbox token does not "
                      "work against production.", self.chart_error or "no reply")
            return False
        self.connected = True

        # Chains are the product here, so probe them rather than assuming a
        # working quote endpoint means a working chain endpoint.
        self.options_error = ""
        probe = await self._get("/markets/options/expirations", symbol="SPY")
        self.options_available = bool(
            ((probe or {}).get("expirations") or {}).get("date"))
        if not self.options_available:
            log.error("Tradier quotes work but option expirations came back "
                      "empty (%s). Nothing can be bought until that answers.",
                      self.options_error or "empty response")
        return True

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self.connected = False

    # ------------------------------------------------------------------ #
    async def _get(self, path: str, **params: Any) -> dict[str, Any] | None:
        if not self._client:
            raise RuntimeError("feed used before connect()")
        try:
            r = await self._client.get(path, params=params)
            if r.status_code != 200:
                self._note(path, f"HTTP {r.status_code}")
                log.debug("%s -> HTTP %s", path, r.status_code)
                return None
            return r.json()
        except Exception as exc:                        # noqa: BLE001
            self._note(path, f"{type(exc).__name__}: {exc}")
            log.debug("%s failed: %s", path, exc)
            return None

    def _note(self, path: str, reason: str) -> None:
        if "/options/" in path:
            self.options_error = reason
        else:
            self.chart_error = reason

    # ------------------------------------------------------------------ #
    async def quote(self, symbol: str) -> dict[str, Any] | None:
        payload = await self._get("/markets/quotes", symbols=symbol,
                                  greeks="false")
        row = _first(((payload or {}).get("quotes") or {}).get("quote"))
        if not row:
            return None
        return {
            "symbol": symbol,
            "last_price": _num(row.get("last")),
            "previous_close": _num(row.get("prevclose")),
            # Tradier's `last` already reflects the most recent trade,
            # extended hours included, so there is no separate pre-market
            # field to report. None here makes the screener fall back to
            # last_price, which is the same number.
            "pre_market_price": None,
            "volume": int(_num(row.get("volume"))),
        }

    async def candles(self, symbol: str, interval: str = "5m",
                      include_prepost: bool = False) -> list[Candle]:
        if interval == "1d":
            end = date.today()
            payload = await self._get(
                "/markets/history", symbol=symbol, interval="daily",
                start=(end - timedelta(days=400)).isoformat(),
                end=end.isoformat())
            rows = _listify(((payload or {}).get("history") or {}).get("day"))
            return [c for c in (_daily_candle(r) for r in rows) if c]

        step = _INTERVALS.get(interval)
        if step is None:
            log.warning("unsupported interval %r for Tradier", interval)
            return []

        end = datetime.now(UTC).date()
        start = end - timedelta(days=_LOOKBACK_DAYS.get(interval, 5))
        payload = await self._get(
            "/markets/timesales", symbol=symbol, interval=step,
            start=start.isoformat(), end=end.isoformat(),
            # "all" includes the pre- and post-market tape, which the sweep
            # and gap rules need; "open" would silently drop it.
            session_filter="all" if include_prepost else "open")
        rows = _listify(((payload or {}).get("series") or {}).get("data"))
        return [c for c in (_intraday_candle(r) for r in rows) if c]

    # ------------------------------------------------------------------ #
    async def expiries(self, symbol: str) -> list[int]:
        """Expiry dates as epoch seconds, to match the Yahoo feed."""
        payload = await self._get("/markets/options/expirations",
                                  symbol=symbol, includeAllRoots="true")
        dates = _listify(((payload or {}).get("expirations") or {}).get("date"))
        out: list[int] = []
        for value in dates:
            parsed = _as_date(value)
            if parsed:
                out.append(int(datetime(parsed.year, parsed.month, parsed.day,
                                        tzinfo=UTC).timestamp()))
        return sorted(out)

    async def option_chain(self, symbol: str, expiry_epoch: int,
                           spot: float) -> list[OptionContract]:
        expiry = datetime.fromtimestamp(expiry_epoch, tz=UTC).date()
        payload = await self._get("/markets/options/chains", symbol=symbol,
                                  expiration=expiry.isoformat(), greeks="true")
        return parse_chain(payload, symbol, expiry)

    async def chain_for_window(self, symbol: str, spot: float,
                               min_dte: int, max_dte: int) -> list[OptionContract]:
        self.options_error = ""
        stamps = await self.expiries(symbol)
        today = datetime.now(UTC).date()

        if not stamps:
            self.options_error = (self.options_error
                                  or "Tradier returned no expiry list")
            log.warning("%s: no expiry list — %s", symbol, self.options_error)
            return []

        wanted = [e for e in stamps
                  if min_dte <= (datetime.fromtimestamp(e, tz=UTC).date()
                                 - today).days <= max_dte]
        if not wanted:
            nearest = sorted((datetime.fromtimestamp(e, tz=UTC).date()
                              - today).days for e in stamps)
            self.options_error = (
                f"no expiry between {min_dte} and {max_dte} days out; the "
                f"listed ones are {', '.join(str(d) for d in nearest[:6])} days")
            return []

        chains = await asyncio.gather(
            *[self.option_chain(symbol, e, spot) for e in wanted],
            return_exceptions=True)
        out: list[OptionContract] = []
        for chain in chains:
            if isinstance(chain, Exception):
                self.options_error = f"{type(chain).__name__}: {chain}"
                continue
            out.extend(chain)
        if not out and not self.options_error:
            self.options_error = "the expiries matched but returned no strikes"
        return out


# --------------------------------------------------------------------------- #
# Parsing, kept as free functions so they can be tested without a network.
# --------------------------------------------------------------------------- #
def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _listify(value: Any) -> list[Any]:
    """Tradier returns a bare object when a list has exactly one element.

    Iterating that without noticing walks the dict's KEYS, which is a very
    quiet way to end up with no data on the one expiry a symbol has.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _first(value: Any) -> dict[str, Any] | None:
    rows = _listify(value)
    return rows[0] if rows and isinstance(rows[0], dict) else None


def _as_date(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _daily_candle(row: Any) -> Candle | None:
    if not isinstance(row, dict):
        return None
    when = _as_date(row.get("date"))
    if not when:
        return None
    return Candle(ts=datetime(when.year, when.month, when.day, tzinfo=UTC),
                  open=_num(row.get("open")), high=_num(row.get("high")),
                  low=_num(row.get("low")), close=_num(row.get("close")),
                  volume=_num(row.get("volume")))


def _intraday_candle(row: Any) -> Candle | None:
    """One timesales bar.

    A bar with no price is a gap in the tape and is DROPPED rather than
    forward-filled: an invented candle prints a body and a wick nobody
    traded, and the pattern engine would read it as a signal.
    """
    if not isinstance(row, dict):
        return None
    close = _num(row.get("close"))
    if close <= 0:
        return None
    stamp = row.get("time") or row.get("timestamp")
    if isinstance(stamp, int | float):
        ts = datetime.fromtimestamp(float(stamp), tz=UTC)
    else:
        try:
            # Tradier's `time` is exchange-local and naive; it means New York.
            from zoneinfo import ZoneInfo
            ts = datetime.fromisoformat(str(stamp)).replace(
                tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
        except (TypeError, ValueError):
            return None
    return Candle(ts=ts, open=_num(row.get("open")) or close,
                  high=_num(row.get("high")) or close,
                  low=_num(row.get("low")) or close, close=close,
                  volume=_num(row.get("volume")))


def parse_chain(payload: dict[str, Any] | None, symbol: str,
                expiry: date) -> list[OptionContract]:
    """A chain response into contracts, using the greeks Tradier supplies.

    No Black-Scholes here, deliberately. The Yahoo path has to estimate delta
    because Yahoo serves none; these are the numbers the market is using, and
    every delta band in this app is meant to be applied to those.
    """
    rows = _listify(((payload or {}).get("options") or {}).get("option"))
    today = datetime.now(UTC).date()
    dte = (expiry - today).days

    out: list[OptionContract] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("option_type") or "").lower()
        if kind not in ("call", "put"):
            continue
        greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else {}
        out.append(OptionContract(
            symbol=symbol,
            right=OptionRight.CALL if kind == "call" else OptionRight.PUT,
            strike=_num(row.get("strike")),
            expiry=expiry.isoformat(),
            dte=dte,
            bid=_num(row.get("bid")),
            ask=_num(row.get("ask")),
            delta=_num((greeks or {}).get("delta")),
            implied_volatility=_num((greeks or {}).get("mid_iv")
                                    or (greeks or {}).get("smv_vol")),
            open_interest=int(_num(row.get("open_interest"))),
            volume=int(_num(row.get("volume"))),
        ))
    return out
