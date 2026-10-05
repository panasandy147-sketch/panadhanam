"""The hourly audit email: what the desk bought and sold, every hour.

While a market is open, once every `email_report.every_minutes` (60), and
once more after the close, an email goes to EMAIL_TO with:

  the body        the last hour's buys, sells and pyramid adds, the day so far
                  (bought, sold, realised P&L, wins / losses), what is still
                  open, and the RSI(2) swing book (US);
  the attachment  the day's whole audit so far as a text file —
                  audit-<market>-<date>.txt, the same text as journal/audit/.

Credentials live ONLY in .env on this machine (never in the repo or a chat):

    SMTP_HOST=smtp.gmail.com        (the default)
    SMTP_PORT=587                   (the default; STARTTLS)
    SMTP_USER=you@gmail.com
    SMTP_PASSWORD=<a Gmail app password, not your normal password>
    EMAIL_TO=you@gmail.com          (several: comma-separated)

With any of SMTP_USER, SMTP_PASSWORD or EMAIL_TO blank nothing is sent and
nothing is attempted. A failed send is logged and never stops trading.

    .venv/Scripts/python.exe -m app.core.email_report --test --market US
                                    sends one now, to check the setup (Windows;
                                    .venv/bin/python on Mac / Linux)
"""
from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

from app.core.logging import get_logger

log = get_logger("email_report")

OPENS = {"BUY", "SELL_SHORT"}
CLOSES = {"SELL", "BUY_TO_COVER"}


def smtp_settings() -> dict[str, Any] | None:
    """The SMTP account from the environment (.env), or None when not set."""
    user = (os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    to = [a.strip() for a in (os.getenv("EMAIL_TO") or "").split(",") if a.strip()]
    if not (user and password and to):
        return None
    try:
        port = int(os.getenv("SMTP_PORT") or 587)
    except ValueError:
        port = 587
    return {"host": (os.getenv("SMTP_HOST") or "smtp.gmail.com").strip(), "port": port,
            "user": user, "password": password, "to": to}


def _ts(e: dict[str, Any]) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(e.get("ts")))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _money(cur: str, v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{'-' if x < 0 else '+'}{cur}{abs(x):,.2f}"


def _line(e: dict[str, Any], cur: str) -> str:
    ev, sym = e.get("event"), e.get("symbol")
    when = str(e.get("market_time") or "")[11:16]
    what = e.get("tradingsymbol") or ""
    if ev in OPENS:
        why = e.get("why") if isinstance(e.get("why"), dict) else {}
        return (f"{when}  {ev} {sym} {what} x{e.get('quantity')} @ {e.get('entry')} · "
                f"stop {e.get('stop_loss')} · target {e.get('target')} · "
                f"{why.get('headline', '')}").rstrip(" ·")
    if ev in CLOSES:
        return (f"{when}  {ev} {sym} {what} @ {e.get('exit_price')} · "
                f"P&L {_money(cur, e.get('pnl'))} ({float(e.get('r_multiple') or 0):+.2f}R) · "
                f"{e.get('status')} — {e.get('why_sold') or ''}").rstrip(" —")
    if ev == "ADD":
        return (f"{when}  ADD {sym} x{e.get('quantity')} @ {e.get('price')} · "
                f"stop now {e.get('stop_loss')}")
    return f"{when}  {ev} {sym}"


def summary(events: list[dict[str, Any]], since: datetime, cur: str) -> dict[str, Any]:
    """The hour's lines, the day's totals and what is still open."""
    hour = [e for e in events if (_ts(e) or since) >= since]
    opens = {e.get("signal_id"): e for e in events if e.get("event") in OPENS}
    closes = [e for e in events if e.get("event") in CLOSES]
    closed_ids = {e.get("signal_id") for e in closes}
    pnl = sum(float(e.get("pnl") or 0) for e in closes)
    wins = sum(1 for e in closes if float(e.get("pnl") or 0) > 0)
    return {"hour": [_line(e, cur) for e in hour],
            "bought": len(opens), "sold": len(closes), "pnl": pnl, "wins": wins,
            "losses": sum(1 for e in closes if float(e.get("pnl") or 0) < 0),
            "open": [f"{e.get('symbol')} {e.get('tradingsymbol') or ''} x{e.get('quantity')} "
                     f"@ {e.get('entry')} (stop {e.get('stop_loss')}, target {e.get('target')})"
                     for sid, e in opens.items() if sid not in closed_ids]}


class AuditEmailer:
    """Sends the hourly audit email for the market that is trading."""

    def __init__(self, cfg: Any, rsi2_book: Any = None, williams_book: Any = None) -> None:
        self.cfg = cfg
        self.rsi2_book = rsi2_book
        self.williams_book = williams_book
        self.last_sent: dict[str, datetime] = {}     # market -> last email
        self.sent_today: dict[str, str] = {}        # market -> day of the last hourly
        self.closed_sent: set[tuple[str, str]] = set()
        self._said_off = False

    def enabled(self) -> bool:
        if not bool(self.cfg.get("email_report.enabled", True)):
            return False
        if smtp_settings() is None:
            if not self._said_off:
                self._said_off = True
                log.info("hourly audit email off — set SMTP_USER, SMTP_PASSWORD and "
                         "EMAIL_TO in .env to switch it on")
            return False
        return True

    def due(self, now: datetime, market_open: bool) -> str | None:
        """'hourly', 'close' or None. `now` is market time."""
        market, day = str(self.cfg.active_market), now.date().isoformat()
        if market_open:
            every = timedelta(minutes=int(self.cfg.get("email_report.every_minutes", 60)))
            last = self.last_sent.get(market)
            if last is None or now - last >= every:
                return "hourly"
            return None
        if (bool(self.cfg.get("email_report.at_close", True))
                and self.sent_today.get(market) == day
                and (market, day) not in self.closed_sent):
            return "close"
        return None

    def build(self, now: datetime, kind: str) -> EmailMessage:
        from app.core import audit

        market = str(self.cfg.active_market)
        cur = self.cfg.market.currency_symbol
        day = now.date()
        events = audit.entries(day, market=market)
        since = (self.last_sent.get(market) or now - timedelta(hours=1)).astimezone(UTC)
        s = summary(events, since, cur)
        tz = now.strftime("%Z")
        label = "after the close" if kind == "close" else "hourly"
        lines = [f"panadhanam — {market} — {now:%Y-%m-%d %H:%M} {tz} ({label})", "",
                 f"Since {since.astimezone(now.tzinfo):%H:%M}:"]
        lines += [f"  {x}" for x in s["hour"]] or ["  no buys or sells"]
        lines += ["", f"Today so far: {s['bought']} bought, {s['sold']} sold, realised P&L "
                      f"{_money(cur, s['pnl'])} ({s['wins']} win, {s['losses']} loss)",
                  "Open now: " + ("; ".join(s["open"]) if s["open"] else "nothing")]
        if self.rsi2_book is not None and self.rsi2_book.enabled():
            b = self.rsi2_book.status()
            lines += ["", f"RSI(2) swing book: equity {cur}{b['equity']:,.2f}, "
                          f"{len(b['open'])} held ({', '.join(o['symbol'] for o in b['open']) or 'flat'}), "
                          f"{b['trades']} closed"]
        if self.williams_book is not None and self.williams_book.enabled():
            b = self.williams_book.status()
            lines += ["", f"Williams-Crabel swing book: equity {cur}{b['equity']:,.2f}, "
                          f"{len(b['open'])} held ({', '.join(o['symbol'] for o in b['open']) or 'flat'}), "
                          f"{b['trades']} closed"]
        lines += ["", f"The day's full audit is attached (audit-{market}-{day}.txt).",
                  "Paper trading only. Sent by panadhanam; switch off with "
                  "email_report.enabled: false or by clearing EMAIL_TO in .env."]
        msg = EmailMessage()
        msg["Subject"] = (f"panadhanam {market} {now:%H:%M} {tz} — {s['bought']} bought, "
                          f"{s['sold']} sold, P&L {_money(cur, s['pnl'])}")
        msg.set_content("\n".join(lines))
        if bool(self.cfg.get("email_report.attach_audit", True)):
            msg.add_attachment(audit.day_markdown(day, market), subtype="plain",
                               filename=f"audit-{market}-{day}.txt")
        return msg

    @staticmethod
    def send(msg: EmailMessage) -> None:
        smtp = smtp_settings()
        if smtp is None:
            raise RuntimeError("SMTP_USER, SMTP_PASSWORD and EMAIL_TO are not all set")
        msg["From"] = smtp["user"]
        msg["To"] = ", ".join(smtp["to"])
        with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(smtp["user"], smtp["password"])
            server.send_message(msg)

    async def maybe_send(self, now: datetime, market_open: bool) -> str | None:
        """Send if one is due. Never raises."""
        if not self.enabled():
            return None
        kind = self.due(now, market_open)
        if kind is None:
            return None
        market, day = str(self.cfg.active_market), now.date().isoformat()
        try:
            msg = self.build(now, kind)
            await asyncio.to_thread(self.send, msg)
        except Exception as exc:                          # noqa: BLE001 - never fatal
            log.warning("audit email not sent: %s", exc)
            # Try again at the next hour, not every cycle.
            self.last_sent[market] = now
            return None
        self.last_sent[market] = now
        if kind == "hourly":
            self.sent_today[market] = day
        else:
            self.closed_sent.add((market, day))
        log.info("audit email sent (%s, %s)", market, kind)
        return kind


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    try:
        from app.core import clock
        from app.core.config import get_config
    except Exception as exc:                              # noqa: BLE001
        # A bare `python` on Windows is the Microsoft Store build, without
        # this project's packages (tzdata, pandas, ...): say which to use.
        print(f"This Python ({sys.executable}) is missing the desk's packages ({exc}).\n"
              "Use the virtual environment beside the project:\n"
              "    .venv/Scripts/python.exe -m app.core.email_report --test --market US"
              "   (Windows)\n"
              "    .venv/bin/python -m app.core.email_report --test --market US"
              "       (Mac / Linux)")
        return 1

    parser = argparse.ArgumentParser(description="Send the audit email now.")
    parser.add_argument("--test", action="store_true", help="send one now")
    parser.add_argument("--market", default=None, choices=["US", "IN"])
    args = parser.parse_args(argv)
    cfg = get_config()
    if args.market:
        cfg.switch_market(args.market)
    if smtp_settings() is None:
        print("Not set up: put SMTP_USER, SMTP_PASSWORD (a Gmail app password) and "
              "EMAIL_TO in .env, then run this again.")
        return 1
    mailer = AuditEmailer(cfg)
    now = clock.market_now(str(cfg.get("system.timezone")))
    try:
        mailer.send(mailer.build(now, "hourly"))
    except Exception as exc:                              # noqa: BLE001
        print(f"Not sent: {exc}")
        return 1
    print(f"Sent the {cfg.active_market} audit email to {os.getenv('EMAIL_TO')}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

