"""CBOE delayed option chains — no account, no key, no KYC.

Why this exists: Yahoo's chain endpoint now returns 401, and the obvious
alternative (Tradier) funnels signup into opening a US brokerage account,
which means SSN and phone verification. That is a lot of identity to hand
over to paper-trade.

CBOE publishes delayed quotes for the options it lists as plain JSON on a
CDN, with no authentication of any kind:

    https://cdn.cboe.com/api/global/delayed_quotes/options/AAPL.json

It carries the greeks — delta, gamma, theta, vega and IV — which is the part
Yahoo never had, so the delta bands in the config stop being applied to an
estimate computed here.

Two limits, both honest:

  * **Delayed, about 15 minutes.** Fine for the default profile, whose setups
    develop over hours. Not fine for the scalp profile: a 1-minute desk
    buying same-day contracts on quarter-hour-old prices is testing a
    fiction, not a strategy.
  * **Chains only.** There are no intraday bars here, so this pairs with a
    chart source rather than replacing one — see `HybridFeed`.

The endpoint is undocumented and could change or go away. Every field is
read defensively and the parsing is tested against the recorded shape, so a
change shows up as a failing test and a clear message rather than as a quiet
day with no trades.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

import httpx

from panaoptions.logging import get_logger
from panaoptions.models import OptionContract, OptionRight

log = get_logger("cboe")

BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"

# Cash-settled index options are published under an underscore-prefixed root.
_INDEX_ROOTS = {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX"}

# An OCC symbol: root, then YYMMDD, then C or P, then the strike in
# thousandths padded to eight digits. AAPL261016C00350000 is the AAPL
# 2026-10-16 350 call.
_OCC = re.compile(r"^(?P<root>[A-Z0-9.\-]{1,6}?)"
                  r"(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})"
                  r"(?P<cp>[CP])(?P<strike>\d{8})$")


def cboe_symbol(symbol: str) -> str:
    """The path segment CBOE publishes this underlying under."""
    clean = symbol.upper().lstrip("^").replace("$", "")
    return f"_{clean}" if clean in _INDEX_ROOTS else clean


def parse_occ(occ: str) -> tuple[date, OptionRight, float] | None:
    """Expiry, right and strike out of an OCC option symbol.

    The strike is in thousandths, so a naive int() is off by a factor of a
    thousand — which would put every contract absurdly far out of the money
    and make the delta filter reject the entire chain.
    """
    match = _OCC.match((occ or "").strip().upper())
    if not match:
        return None
    try:
        expiry = date(2000 + int(match["y"]), int(match["m"]), int(match["d"]))
    except ValueError:
        return None
    right = OptionRight.CALL if match["cp"] == "C" else OptionRight.PUT
    return expiry, right, int(match["strike"]) / 1000.0


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_chain(payload: dict[str, Any] | None, symbol: str,
                today: date | None = None) -> list[OptionContract]:
    """Every contract in a delayed-quotes response."""
    rows = (((payload or {}).get("data") or {}).get("options")) or []
    if not isinstance(rows, list):
        return []
    today = today or datetime.now(UTC).date()

    out: list[OptionContract] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = parse_occ(str(row.get("option") or ""))
        if not parsed:
            continue
        expiry, right, strike = parsed
        out.append(OptionContract(
            symbol=symbol, right=right, strike=strike,
            expiry=expiry.isoformat(), dte=(expiry - today).days,
            bid=_num(row.get("bid")), ask=_num(row.get("ask")),
            delta=_num(row.get("delta")),
            implied_volatility=_num(row.get("iv")),
            open_interest=int(_num(row.get("open_interest"))),
            volume=int(_num(row.get("volume"))),
        ))
    return out


class CboeChains:
    """Option chains only. Pair it with a chart source via HybridFeed."""

    name = "cboe"
    delayed = True          # about 15 minutes
    greeks = True           # delta and IV come from CBOE

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self.options_error: str = ""
        # One response carries every expiry for a symbol, and they are large.
        # Fetching it once per cycle per symbol rather than once per expiry is
        # the difference between three requests a minute and thirty.
        self._cache: dict[str, tuple[float, list[OptionContract]]] = {}
        self.cache_seconds: float = 90.0

    async def open(self) -> None:
        if not self._client:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True,
                headers={"Accept": "application/json",
                         "User-Agent": "panaoptions/1.0"})

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def probe(self) -> bool:
        """Is the endpoint answering? Used at connect, on one liquid name."""
        await self.open()
        self.options_error = ""
        chain = await self.all_contracts("SPY")
        return bool(chain)

    async def all_contracts(self, symbol: str) -> list[OptionContract]:
        await self.open()
        assert self._client is not None

        import time
        cached = self._cache.get(symbol.upper())
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]

        url = BASE.format(sym=cboe_symbol(symbol))
        try:
            r = await self._client.get(url)
        except Exception as exc:                        # noqa: BLE001
            self.options_error = f"{type(exc).__name__}: {exc}"
            return []
        if r.status_code != 200:
            self.options_error = (
                f"HTTP {r.status_code} from CBOE for {symbol}"
                + (" — CBOE publishes chains only for the options it lists, "
                   "so a symbol it does not carry returns 404."
                   if r.status_code == 404 else ""))
            return []
        try:
            payload = r.json()
        except ValueError as exc:
            self.options_error = f"CBOE returned something that is not JSON: {exc}"
            return []

        chain = parse_chain(payload, symbol.upper())
        if not chain:
            self.options_error = (
                f"CBOE answered for {symbol} but no contract parsed — the "
                f"response shape may have changed")
        self._cache[symbol.upper()] = (time.monotonic(), chain)
        return chain

    async def expiries(self, symbol: str) -> list[int]:
        chain = await self.all_contracts(symbol)
        seen = {c.expiry for c in chain}
        out = []
        for value in sorted(seen):
            try:
                day = date.fromisoformat(value)
            except ValueError:
                continue
            out.append(int(datetime(day.year, day.month, day.day,
                                    tzinfo=UTC).timestamp()))
        return out

    async def option_chain(self, symbol: str, expiry_epoch: int,
                           spot: float) -> list[OptionContract]:
        expiry = datetime.fromtimestamp(expiry_epoch, tz=UTC).date().isoformat()
        return [c for c in await self.all_contracts(symbol)
                if c.expiry == expiry]

    async def chain_for_window(self, symbol: str, spot: float,
                               min_dte: int, max_dte: int) -> list[OptionContract]:
        self.options_error = ""
        chain = await self.all_contracts(symbol)
        if not chain:
            self.options_error = (self.options_error
                                  or "CBOE returned no contracts")
            return []

        inside = [c for c in chain if min_dte <= c.dte <= max_dte]
        if not inside:
            nearest = sorted({c.dte for c in chain if c.dte >= 0})
            self.options_error = (
                f"no expiry between {min_dte} and {max_dte} days out; the "
                f"listed ones are {', '.join(str(d) for d in nearest[:6])} days")
        return inside
