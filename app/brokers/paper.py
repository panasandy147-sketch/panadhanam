"""Paper broker — the default. Runs the entire system with zero credentials.

It generates a coherent synthetic market (trending/mean-reverting regimes, an
opening-range, volume smile, a matching option chain) so you can watch every
agent work end to end before you ever point it at real money.

Fills are simulated with slippage. Positions are marked to the synthetic market
so the risk state, P&L and learning loop all exercise real code paths.
"""
from __future__ import annotations

import math
import random
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.brokers.base import BrokerAdapter, OrderResult
from app.core.config import get_config
from app.core.logging import get_logger
from app.core.models import Candle, Instrument, OptionChain, OptionLeg, Quote, Side
from app.core.registry import register_broker
from app.indicators.derivatives import black_scholes_greeks

log = get_logger("broker.paper")

_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60, "1d": 375}

# Rough starting levels so the simulation looks like the real tape.
_SEED_PRICES = {
    "NIFTY": 24200.0, "NIFTY 50": 24200.0, "BANKNIFTY": 51500.0, "NIFTY BANK": 51500.0,
    "FINNIFTY": 23100.0, "RELIANCE": 2950.0, "HDFCBANK": 1680.0, "ICICIBANK": 1240.0,
    "INFY": 1870.0, "TCS": 4100.0, "SBIN": 815.0, "TATAMOTORS": 975.0,
    "AXISBANK": 1150.0, "LT": 3600.0, "BHARTIARTL": 1580.0,
}
_STRIKE_STEP = {"NIFTY": 50.0, "BANKNIFTY": 100.0, "FINNIFTY": 50.0}

# The watchlist calls the indices "NIFTY 50" / "NIFTY BANK"; the F&O world calls
# them NIFTY / BANKNIFTY. Normalise so seed price, strike step and the option
# chain all agree on one instrument.
_ALIASES = {
    "NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
}


def canonical(symbol: str) -> str:
    return _ALIASES.get(symbol.strip().upper(), symbol.strip().upper())


class _SyntheticMarket:
    """A per-symbol synthetic tape.

    `self.price` is the single source of truth for this symbol's spot. History is
    generated BACKWARD from it and rescaled so the final close always equals the
    current price — which means the chart, the quote and the option chain can
    never disagree about where the instrument is trading. (The first version
    walked the price forward on every call, so each timeframe ended up at its own
    "spot"; that is exactly the bug this shape prevents.)
    """

    def __init__(self, symbol: str, seed_price: float) -> None:
        self.symbol = symbol
        self.seed_price = seed_price
        self.price = seed_price
        self.rng = random.Random(hash(symbol) & 0xFFFFFFFF)
        self.drift = self.rng.uniform(-0.0003, 0.0003)
        self.vol = self.rng.uniform(0.0025, 0.0055)
        self._cache: dict[str, tuple[float, list[Candle]]] = {}
        self._last_tick = datetime.now()

    def tick(self) -> float:
        """Advance the spot by wall-clock time, with mean reversion to the seed so
        a long-running session never drifts into nonsense."""
        now = datetime.now()
        elapsed = (now - self._last_tick).total_seconds()
        if elapsed < 1.0:
            return self.price
        self._last_tick = now

        steps = min(elapsed / 60.0, 30.0)          # cap a long idle gap
        shock = self.rng.gauss(self.drift, self.vol) * math.sqrt(steps)
        reversion = 0.02 * (self.seed_price - self.price) / self.seed_price
        self.price = max(self.price * (1 + shock + reversion), 0.01)
        return self.price

    def candles(self, timeframe: str, count: int) -> list[Candle]:
        price = self.tick()
        key = f"{timeframe}:{count}"

        cached = self._cache.get(key)
        # Reuse the shape while the spot has barely moved; just re-anchor the tail.
        if cached and abs(cached[0] - price) / max(price, 1e-9) < 0.0005:
            return cached[1]

        minutes = _TF_MINUTES.get(timeframe, 5)
        rng = random.Random(f"{self.symbol}:{timeframe}:{count}")
        now = datetime.now().replace(second=0, microsecond=0)
        start = now - timedelta(minutes=minutes * count)

        # 1. Build a shape with its own regimes.
        closes: list[float] = []
        level = 1.0
        bars_left, drift, vol = 0, 0.0, self.vol
        for _ in range(count):
            if bars_left <= 0:
                bars_left = rng.randint(20, 60)
                drift = rng.uniform(-0.0005, 0.0005)
                vol = rng.uniform(0.002, 0.007)
            bars_left -= 1
            level *= 1 + rng.gauss(drift, vol) * math.sqrt(minutes / 5)
            closes.append(level)

        # 2. Rescale so the last close IS the current price.
        scale = price / closes[-1]
        closes = [c * scale for c in closes]

        out: list[Candle] = []
        for i, close in enumerate(closes):
            ts = start + timedelta(minutes=minutes * i)
            open_ = closes[i - 1] if i else close / (1 + rng.gauss(0, vol))
            body_hi, body_lo = max(open_, close), min(open_, close)
            wick = abs(close - open_) * rng.uniform(0.3, 1.4) + close * 0.0004
            high = body_hi + wick * rng.random()
            low = max(body_lo - wick * rng.random(), 0.01)

            # U-shaped intraday volume smile: busy at the open and the close.
            minute_of_day = (ts.hour * 60 + ts.minute) - 555
            frac = min(max(minute_of_day / 375.0, 0.0), 1.0)
            smile = 1.6 - 2.2 * frac + 2.2 * frac ** 2
            base_vol = 90_000 if price > 5000 else 400_000
            move = abs(close - open_) / max(open_, 1e-9)
            volume = base_vol * smile * rng.uniform(0.6, 1.5) * (1 + move * 60)

            out.append(Candle(ts=ts, open=round(open_, 2), high=round(high, 2),
                              low=round(low, 2), close=round(close, 2),
                              volume=round(volume)))

        self._cache[key] = (price, out)
        return out

    def quote(self) -> Quote:
        candles = self.candles("5m", 75)
        last = candles[-1]
        prev = candles[-2] if len(candles) > 1 else last
        change = (last.close - prev.close) / prev.close * 100 if prev.close else 0.0
        spread = max(last.close * 0.0002, 0.05)
        return Quote(symbol=self.symbol, last_price=last.close,
                     change_pct=round(change, 3), volume=last.volume,
                     bid=round(last.close - spread, 2), ask=round(last.close + spread, 2))


@register_broker("paper")
class PaperBroker(BrokerAdapter):
    name = "paper"
    supports_options = True
    supports_live_orders = False

    def __init__(self, credentials: dict[str, str] | None = None,
                 config: dict[str, Any] | None = None) -> None:
        super().__init__(credentials, config)
        self._markets: dict[str, _SyntheticMarket] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self._positions: dict[str, dict[str, Any]] = {}
        self._starting_cash = float((config or {}).get("total_capital", 100_000))
        self._cash = self._starting_cash
        # True once a chain has been fabricated because no feed could serve one.
        self.synthetic_chain = False
        # An upstream real-data feed can be injected; paper then only simulates fills.
        self.data_source: BrokerAdapter | None = None

    async def connect(self) -> bool:
        self._connected = True
        log.info("paper broker ready (simulated fills, capital %.0f)", self._starting_cash)
        return True

    def _market(self, symbol: str) -> _SyntheticMarket:
        key = canonical(symbol)
        if key not in self._markets:
            self._markets[key] = _SyntheticMarket(key, self._seed_price(symbol, key))
        return self._markets[key]

    @staticmethod
    def _seed_price(symbol: str, key: str) -> float:
        """Start each symbol near a plausible level for its own market.

        The profile carries `seed_price` per symbol, so switching to the US
        gives SPY around 585 rather than an Indian index level.
        """
        cfg = get_config()
        for item in cfg.watchlist():
            if item.get("symbol") in {symbol, key} or \
               item.get("trading_symbol") in {symbol, key}:
                seed = item.get("seed_price")
                if seed:
                    return float(seed)
        if key in _SEED_PRICES:
            return _SEED_PRICES[key]
        return random.Random(key).uniform(50, 500)

    # ----------------------- market data -----------------------
    async def get_quote(self, symbol: str) -> Quote | None:
        if self.data_source:
            q = await self.data_source.get_quote(symbol)
            if q:
                return q
        return self._market(symbol).quote()

    async def get_candles(self, symbol: str, timeframe: str, count: int = 200) -> list[Candle]:
        if self.data_source:
            c = await self.data_source.get_candles(symbol, timeframe, count)
            if c:
                return c
        return self._market(symbol).candles(timeframe, count)

    async def get_expiries(self, underlying: str) -> list[str]:
        """Real expiries when a feed is attached, otherwise synthetic weeklies.

        NSE weeklies land on Thursday; US weeklies on Friday.
        """
        if self.data_source:
            real = await self.data_source.get_expiries(underlying)
            if real:
                return real

        weekday = get_config().market.weekly_expiry_weekday
        out: list[str] = []
        d = datetime.now().date()
        while len(out) < 4:
            d += timedelta(days=1)
            if d.weekday() == weekday:
                out.append(d.isoformat())
        return out

    async def get_option_chain(self, underlying: str, expiry: str | None = None) -> OptionChain | None:
        if self.data_source:
            chain = await self.data_source.get_option_chain(underlying, expiry)
            if chain:
                self.synthetic_chain = False
                return chain

        # No feed could serve a chain. Fabricating OI, PCR and Max Pain while
        # the header says "real market data" would be the worst kind of lie —
        # those numbers drive the derivatives analyst. Flag it, and let the
        # config decide whether to fabricate at all.
        self.synthetic_chain = True
        if not bool(get_config().get("data.synthetic_chain_fallback", True)):
            return None

        quote = await self.get_quote(underlying)
        if not quote:
            return None
        spot = quote.last_price
        expiries = await self.get_expiries(underlying)
        exp = expiry or expiries[0]
        dte = max((datetime.fromisoformat(exp).date() - datetime.now().date()).days, 1)

        # Strike ladders are a market convention: NIFTY moves in 50s, SPY in 1s.
        step = get_config().market.strike_step(canonical(underlying), spot)
        atm = round(spot / step) * step
        rng = random.Random(f"{canonical(underlying)}{exp}")
        base_iv = rng.uniform(0.12, 0.28)

        legs: list[OptionLeg] = []
        for i in range(-10, 11):
            strike = atm + i * step
            if strike <= 0:
                continue
            # Volatility smile: wings carry higher IV.
            iv = base_iv * (1 + 0.035 * abs(i))
            for opt in ("CE", "PE"):
                g = black_scholes_greeks(spot, strike, dte, iv, option_type=opt)
                # OI peaks near ATM and at round-number walls.
                oi = max(0.0, rng.gauss(1.0, 0.25)) * 900_000 * math.exp(-(i ** 2) / 22)
                if strike % (step * 5) == 0:
                    oi *= 1.7
                legs.append(OptionLeg(
                    strike=strike, option_type=opt,
                    ltp=max(g["price"], 0.05),
                    oi=round(oi),
                    oi_change=round(oi * rng.uniform(-0.18, 0.22)),
                    volume=round(oi * rng.uniform(0.05, 0.4)),
                    iv=round(iv, 4),
                    delta=g["delta"], gamma=g["gamma"], theta=g["theta"], vega=g["vega"],
                ))
        return OptionChain(underlying=underlying, spot=spot, expiry=exp, legs=legs,
                           synthetic=True)

    # ----------------------- account -----------------------
    async def get_funds(self) -> dict[str, float]:
        return {"available": round(self._cash, 2), "used": round(self._starting_cash - self._cash, 2),
                "starting": self._starting_cash}

    async def get_positions(self) -> list[dict[str, Any]]:
        out = []
        for pos in self._positions.values():
            quote = await self.get_quote(pos["underlying"])
            ltp = quote.last_price if quote else pos["entry"]
            if pos.get("is_option"):
                # Crude but honest: move premium by delta * underlying move.
                ltp = max(pos["entry"] + (ltp - pos["underlying_at_entry"]) * pos.get("delta", 0.5), 0.05)
            direction = 1 if pos["side"] == Side.BUY.value else -1
            pnl = (ltp - pos["entry"]) * pos["quantity"] * direction
            out.append({**pos, "ltp": round(ltp, 2), "pnl": round(pnl, 2)})
        return out

    # ----------------------- orders -----------------------
    async def place_order(self, instrument: Instrument, side: Side, quantity: int,
                          price: float, order_type: str = "LIMIT",
                          product: str = "MIS", stop_loss: float | None = None,
                          tag: str = "") -> OrderResult:
        if quantity <= 0:
            return OrderResult(False, message="quantity must be positive")

        # Simulated slippage: market orders pay more than limits.
        slip_bps = 6 if order_type == "MARKET" else 2
        direction = 1 if side == Side.BUY else -1
        fill = price * (1 + direction * slip_bps / 10_000)

        order_id = f"PAPER-{uuid.uuid4().hex[:10].upper()}"
        underlying_quote = await self.get_quote(instrument.symbol)
        is_option = instrument.instrument_type.value in {"CE", "PE"}

        record = {
            "order_id": order_id,
            "tradingsymbol": instrument.tradingsymbol,
            "underlying": instrument.symbol,
            "side": side.value,
            "quantity": quantity,
            "entry": round(fill, 2),
            "price": price,
            "order_type": order_type,
            "product": product,
            "stop_loss": stop_loss,
            "status": "COMPLETE",
            "tag": tag,
            "is_option": is_option,
            "delta": 0.5,
            "underlying_at_entry": underlying_quote.last_price if underlying_quote else fill,
            "placed_at": datetime.now().isoformat(),
        }
        self._orders[order_id] = record
        self._positions[order_id] = record
        self._cash -= fill * quantity * (1 if side == Side.BUY else 0)
        log.info("paper fill %s %s x%d @ %.2f", side.value, instrument.tradingsymbol, quantity, fill)
        return OrderResult(True, order_id=order_id,
                           message=f"simulated fill @ {fill:.2f}", raw=record)

    async def cancel_order(self, order_id: str) -> OrderResult:
        rec = self._orders.get(order_id)
        if not rec:
            return OrderResult(False, message="unknown order")
        rec["status"] = "CANCELLED"
        self._positions.pop(order_id, None)
        return OrderResult(True, order_id=order_id, message="cancelled")

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        return self._orders.get(order_id, {})

    async def close_position(self, order_id: str, exit_price: float) -> float:
        """Close and realise P&L. Returns the realised amount."""
        pos = self._positions.pop(order_id, None)
        if not pos:
            return 0.0
        direction = 1 if pos["side"] == Side.BUY.value else -1
        pnl = (exit_price - pos["entry"]) * pos["quantity"] * direction
        self._cash += exit_price * pos["quantity"] * (1 if pos["side"] == Side.BUY.value else 0)
        return round(pnl, 2)
