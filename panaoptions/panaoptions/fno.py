"""The previous day's F&O picture: levels, open interest, and who built it.

For every symbol the desk watches, once a day (and again whenever a chain is
read):

  PDH / PDL / PDC   the previous session's high, low and close, from the
                    desk's own 5-minute tape (regular hours only).
  open interest     total call OI and total put OI across the near expiries,
                    recorded per day: the first reading and the latest.
  change in OI      latest against the previous day's reading. US chains
                    (CBOE, Yahoo) carry OI as of the previous close, so the
                    day-over-day change IS the previous session's change;
                    NSE updates OI intraday, so there it moves live too.
  build-up          the previous session's price change against its change
                    in OI — the standard F&O read of who is positioning:

                        price up,   OI up    Long Buildup
                        price down, OI up    Short Buildup
                        price up,   OI down  Short Covering
                        price down, OI down  Long Unwinding

The confluence filter (engine/confluence.py) reads "rising call OI" and
"rising put OI" from here. When a chain carries no OI (India on estimated
prices), the read is "unknown", and the filter says so.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from panaoptions.logging import get_logger

log = get_logger("fno")


@dataclass
class OiRead:
    call_oi: int = 0
    put_oi: int = 0
    call_change: int | None = None       # None: nothing to compare against yet
    put_change: int | None = None
    basis: str = ""                      # "vs the previous day" / "since the first reading today"

    @property
    def known(self) -> bool:
        return (self.call_oi + self.put_oi) > 0 and self.call_change is not None

    @property
    def calls_rising(self) -> bool | None:
        return None if self.call_change is None else self.call_change > 0

    @property
    def puts_rising(self) -> bool | None:
        return None if self.put_change is None else self.put_change > 0

    def line(self) -> str:
        if not (self.call_oi or self.put_oi):
            return "no open interest in the chain"
        def ch(v: int | None) -> str:
            return "?" if v is None else f"{v:+,}"
        return (f"call OI {self.call_oi:,} ({ch(self.call_change)}), put OI "
                f"{self.put_oi:,} ({ch(self.put_change)}) {self.basis}".strip())


@dataclass
class DayPicture:
    symbol: str
    day: str
    pdh: float = 0.0
    pdl: float = 0.0
    pdc: float = 0.0
    close_before: float = 0.0
    call_oi: int = 0
    put_oi: int = 0
    call_oi_change: int | None = None
    put_oi_change: int | None = None
    price_change_pct: float | None = None
    bias: str = "unknown"
    ingested_at: str = ""                # when it was cached (before the open)

    def line(self) -> str:
        pc = "" if self.price_change_pct is None else f", {self.price_change_pct:+.2f}%"
        return (f"{self.symbol}: PDH {self.pdh:,.2f} · PDL {self.pdl:,.2f} · PDC "
                f"{self.pdc:,.2f}{pc} · {self.bias}")


def buildup(price_change: float | None, oi_change: int | None) -> str:
    if price_change is None or oi_change is None or oi_change == 0 or price_change == 0:
        return "unknown" if price_change is None or oi_change is None else "neutral"
    if price_change > 0:
        return "Long Buildup" if oi_change > 0 else "Short Covering"
    return "Short Buildup" if oi_change > 0 else "Long Unwinding"


def oi_totals(chain: list[Any], max_dte: int | None = None) -> tuple[int, int]:
    """Total call and put open interest across the chain (optionally only
    expiries within `max_dte`). Spreads' synthetic entries are skipped."""
    calls = puts = 0
    for c in chain or []:
        if getattr(c, "is_spread", False):
            continue
        if max_dte is not None and c.dte > max_dte:
            continue
        oi = int(c.open_interest or 0)
        if c.right.value == "CALL":
            calls += oi
        else:
            puts += oi
    return calls, puts


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def record_oi(symbol: str, now: datetime, chain: list[Any],
              max_dte: int | None = None) -> OiRead:
    """Keep today's first and latest OI reading for `symbol`; return the read."""
    from panaoptions.ledger import store

    call_oi, put_oi = oi_totals(chain, max_dte)
    day = now.date().isoformat()
    # NSE publishes each strike's change in OI since the previous close:
    # the exact "rising call / put OI" when the chain carries it.
    exchange = [c for c in chain or [] if getattr(c, "oi_change", 0)
                and (max_dte is None or c.dte <= max_dte)]
    if call_oi or put_oi:
        try:
            conn = store.get_conn()
            conn.execute(
                "INSERT INTO fno_oi (symbol, day, first_ts, call_oi_first, put_oi_first, "
                "last_ts, call_oi, put_oi) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol, day) DO UPDATE SET last_ts=excluded.last_ts, "
                "call_oi=excluded.call_oi, put_oi=excluded.put_oi",
                (symbol, day, now.isoformat(), call_oi, put_oi, now.isoformat(),
                 call_oi, put_oi))
            conn.commit()
        except Exception as exc:                        # noqa: BLE001
            log.warning("could not record OI for %s: %s", symbol, exc)
    if exchange:
        calls = sum(int(c.oi_change) for c in exchange if c.right.value == "CALL")
        puts = sum(int(c.oi_change) for c in exchange if c.right.value == "PUT")
        return OiRead(call_oi, put_oi, calls, puts, "since the previous close (exchange)")
    return read_oi(symbol, now)


def read_oi(symbol: str, now: datetime) -> OiRead:
    """Today's OI and its change: against the previous day's latest reading
    when there is one, else against today's first reading."""
    from panaoptions.ledger import store

    day = now.date().isoformat()
    try:
        conn = store.get_conn()
        today = conn.execute(
            "SELECT call_oi_first, put_oi_first, call_oi, put_oi FROM fno_oi "
            "WHERE symbol=? AND day=?", (symbol, day)).fetchone()
        before = conn.execute(
            "SELECT call_oi, put_oi, day FROM fno_oi WHERE symbol=? AND day<? "
            "ORDER BY day DESC LIMIT 1", (symbol, day)).fetchone()
    except Exception:                                   # noqa: BLE001
        return OiRead()
    if not today:
        return OiRead()
    first_c, first_p, call_oi, put_oi = (int(x or 0) for x in today)
    if before:
        return OiRead(call_oi, put_oi, call_oi - int(before[0] or 0),
                      put_oi - int(before[1] or 0), f"vs {before[2]}")
    if (call_oi, put_oi) != (first_c, first_p):
        return OiRead(call_oi, put_oi, call_oi - first_c, put_oi - first_p,
                      "since the first reading today")
    return OiRead(call_oi, put_oi, None, None, "(first reading — no change yet)")


def picture(symbol: str, now: datetime, levels: Any) -> DayPicture:
    """The previous day's levels plus the OI read, with the build-up."""
    oi = read_oi(symbol, now)
    pic = DayPicture(symbol=symbol, day=now.date().isoformat(),
                     pdh=float(getattr(levels, "previous_high", 0.0) or 0.0),
                     pdl=float(getattr(levels, "previous_low", 0.0) or 0.0),
                     pdc=float(getattr(levels, "previous_close", 0.0) or 0.0),
                     close_before=float(getattr(levels, "close_before", 0.0) or 0.0),
                     call_oi=oi.call_oi, put_oi=oi.put_oi,
                     call_oi_change=oi.call_change, put_oi_change=oi.put_change)
    if pic.pdc and pic.close_before:
        pic.price_change_pct = round((pic.pdc - pic.close_before) / pic.close_before * 100, 2)
    total_change = (None if oi.call_change is None or oi.put_change is None
                    else oi.call_change + oi.put_change)
    pic.bias = buildup(pic.price_change_pct, total_change)
    return pic


def save_picture(pic: DayPicture) -> None:
    from panaoptions.ledger import store
    try:
        conn = store.get_conn()
        conn.execute("INSERT OR REPLACE INTO fno_daily (symbol, day, payload) VALUES (?,?,?)",
                     (pic.symbol, pic.day, json.dumps(asdict(pic))))
        conn.commit()
    except Exception as exc:                            # noqa: BLE001
        log.warning("could not save the F&O picture for %s: %s", pic.symbol, exc)


def pictures(day: str) -> list[dict[str, Any]]:
    from panaoptions.ledger import store
    try:
        rows = store.get_conn().execute(
            "SELECT payload FROM fno_daily WHERE day=? ORDER BY symbol", (day,)).fetchall()
    except Exception:                                   # noqa: BLE001
        return []
    return [json.loads(r[0]) for r in rows]
