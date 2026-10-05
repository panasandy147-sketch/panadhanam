"""The hourly audit email (app/core/email_report.py): off without .env
credentials, hourly while open plus one after the close, the hour's trades in
the body and the day's audit attached as text — sent through SMTP STARTTLS."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core import email_report as m

ET = ZoneInfo("America/New_York")


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "desk@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("EMAIL_TO", "me@example.com, you@example.com")


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        assert self.tls and user == "desk@example.com" and password == "app-password"

    def send_message(self, msg):
        FakeSMTP.sent.append((self.host, self.port, msg))


def test_nothing_is_sent_without_the_env(cfg, monkeypatch):
    for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert m.smtp_settings() is None
    mailer = m.AuditEmailer(cfg)
    assert not mailer.enabled()
    assert asyncio.run(mailer.maybe_send(datetime(2026, 10, 5, 10, 0, tzinfo=ET), True)) is None


def test_hourly_while_open_then_once_after_the_close(cfg, smtp_env):
    mailer = m.AuditEmailer(cfg)
    cfg.switch_market("US")
    try:
        t = datetime(2026, 10, 5, 9, 31, tzinfo=ET)
        assert mailer.due(t, True) == "hourly"
        mailer.last_sent["US"], mailer.sent_today["US"] = t, "2026-10-05"
        assert mailer.due(t + timedelta(minutes=59), True) is None
        assert mailer.due(t + timedelta(minutes=60), True) == "hourly"
        close = datetime(2026, 10, 5, 16, 5, tzinfo=ET)
        assert mailer.due(close, False) == "close"
        mailer.closed_sent.add(("US", "2026-10-05"))
        assert mailer.due(close, False) is None
        # No email for a day the desk never opened.
        assert mailer.due(datetime(2026, 10, 6, 16, 5, tzinfo=ET), False) is None
        cfg.settings["email_report"]["enabled"] = False
        assert not mailer.enabled()
    finally:
        cfg.settings["email_report"]["enabled"] = True
        cfg.switch_market("IN")


def test_the_summary_counts_the_hour_and_the_day():
    events = [
        {"ts": "2026-10-05T14:00:00+00:00", "event": "BUY", "signal_id": "A", "symbol": "AAPL",
         "quantity": 10, "entry": 250.0, "stop_loss": 248.0, "target": 256.0,
         "market_time": "2026-10-05 10:00:00 EDT", "why": {"headline": "PD sweep"}},
        {"ts": "2026-10-05T14:40:00+00:00", "event": "SELL", "signal_id": "A", "symbol": "AAPL",
         "exit_price": 256.0, "pnl": 60.0, "r_multiple": 3.0, "status": "CLOSED_TARGET",
         "why_sold": "target", "market_time": "2026-10-05 10:40:00 EDT"},
        {"ts": "2026-10-05T15:10:00+00:00", "event": "BUY", "signal_id": "B", "symbol": "KO",
         "quantity": 5, "entry": 70.0, "stop_loss": 69.0, "target": 73.0,
         "market_time": "2026-10-05 11:10:00 EDT"},
    ]
    since = datetime.fromisoformat("2026-10-05T14:30:00+00:00")
    s = m.summary(events, since, "$")
    assert len(s["hour"]) == 2 and "SELL AAPL" in s["hour"][0] and "+$60.00" in s["hour"][0]
    assert (s["bought"], s["sold"], s["pnl"], s["wins"], s["losses"]) == (2, 1, 60.0, 1, 0)
    assert len(s["open"]) == 1 and s["open"][0].startswith("KO")


def test_the_email_goes_out_with_the_audit_attached(cfg, smtp_env, monkeypatch):
    from app.core import audit

    monkeypatch.setattr(m.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(audit, "entries", lambda day=None, market=None, **k: [])
    monkeypatch.setattr(audit, "day_markdown", lambda day, market="US": "# Audit — US")
    FakeSMTP.sent.clear()
    cfg.switch_market("US")
    try:
        mailer = m.AuditEmailer(cfg)
        now = datetime(2026, 10, 5, 10, 30, tzinfo=ET)
        assert asyncio.run(mailer.maybe_send(now, True)) == "hourly"
        assert asyncio.run(mailer.maybe_send(now + timedelta(minutes=5), True)) is None
    finally:
        cfg.switch_market("IN")
    [(host, port, msg)] = FakeSMTP.sent
    assert (host, port) == ("smtp.gmail.com", 587)
    assert msg["To"] == "me@example.com, you@example.com"
    assert msg["Subject"].startswith("panadhanam US 10:30")
    body = msg.get_body(("plain",)).get_content()
    assert "no buys or sells" in body and "Open now: nothing" in body
    [att] = list(msg.iter_attachments())
    assert att.get_filename() == "audit-US-2026-10-05.txt"
    assert att.get_content().strip() == "# Audit — US"
    assert "app-password" not in msg.as_string()


def test_a_failed_send_never_raises_and_waits_an_hour(cfg, smtp_env, monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(m.smtplib, "SMTP", boom)
    cfg.switch_market("US")
    try:
        mailer = m.AuditEmailer(cfg)
        now = datetime(2026, 10, 5, 10, 30, tzinfo=ET)
        assert asyncio.run(mailer.maybe_send(now, True)) is None
        assert mailer.due(now + timedelta(minutes=10), True) is None
    finally:
        cfg.switch_market("IN")
