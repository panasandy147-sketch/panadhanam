"""Picking a working chain source automatically.

The reason this exists: which endpoint answers is a property of the machine
and of the week. Yahoo's chain host returns 401 for some people and works for
others, and it changes without notice. Making somebody read a log, understand
the difference between two Yahoo hosts and then run a command to switch is a
design failure — the desk can find out in one request.
"""
from __future__ import annotations

import pytest

from panaoptions.data.auto import AutoChains


class _Source:
    def __init__(self, name, ok, error="", delayed=False, greeks=False):
        self.name, self._ok = name, ok
        self.options_error = error
        self.delayed, self.greeks = delayed, greeks
        self.opened = self.closed = False
        self.asked: list[str] = []

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    async def probe(self):
        return self._ok

    async def chain_for_window(self, symbol, spot, min_dte, max_dte):
        self.asked.append(symbol)
        return [object()] if self._ok else []

    async def expiries(self, symbol):
        return [1] if self._ok else []

    async def option_chain(self, symbol, epoch, spot):
        return []


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_first_source_that_answers_wins():
    first, second = _Source("yahoo", True), _Source("cboe", True)
    auto = AutoChains([first, second])
    assert await auto.probe() is True
    assert auto.chosen == "yahoo"

    await auto.chain_for_window("META", 700.0, 14, 30)
    assert first.asked == ["META"] and second.asked == []


@pytest.mark.asyncio
async def test_a_dead_source_falls_through_to_the_next():
    """The 401 case: Yahoo refuses, CBOE answers, nothing to configure."""
    dead = _Source("yahoo", False, error="HTTP 401")
    alive = _Source("cboe", True, delayed=True, greeks=True)
    auto = AutoChains([dead, alive])

    assert await auto.probe() is True
    assert auto.chosen == "cboe"
    assert auto.delayed and auto.greeks
    # And it remembers why the preferred one was skipped, so the fallback is
    # explicable rather than mysterious.
    assert ("yahoo", "HTTP 401") in auto.attempts


@pytest.mark.asyncio
async def test_a_source_that_raises_does_not_take_the_others_down():
    class _Explodes(_Source):
        async def probe(self):
            raise ConnectionError("boom")

    auto = AutoChains([_Explodes("yahoo", False), _Source("cboe", True)])
    assert await auto.probe() is True
    assert auto.chosen == "cboe"
    assert any("ConnectionError" in why for _n, why in auto.attempts)


@pytest.mark.asyncio
async def test_when_nothing_answers_it_says_so_with_every_reason():
    """One line naming each source and why, rather than a bare empty list."""
    auto = AutoChains([_Source("yahoo", False, error="HTTP 401"),
                       _Source("cboe", False, error="HTTP 403")])
    assert await auto.probe() is False
    assert auto.chosen == ""
    assert "yahoo: HTTP 401" in auto.options_error
    assert "cboe: HTTP 403" in auto.options_error


@pytest.mark.asyncio
async def test_asking_for_a_chain_with_nothing_chosen_is_empty_not_a_crash():
    auto = AutoChains([_Source("yahoo", False)])
    await auto.probe()
    assert await auto.chain_for_window("META", 700.0, 14, 30) == []
    assert await auto.expiries("META") == []
    assert auto.options_error


@pytest.mark.asyncio
async def test_the_chosen_sources_reason_reaches_the_dashboard():
    """The desk clears options_error before a lookup and reads it after."""
    alive = _Source("cboe", True)
    auto = AutoChains([alive])
    await auto.probe()

    # A real source writes its reason DURING the call, after the desk has
    # cleared the field — so the fake has to do the same or the test would
    # pass on a value that never survives.
    async def _explains(symbol, spot, min_dte, max_dte):
        alive.options_error = "no expiry between 14 and 30 days out"
        return []

    alive.chain_for_window = _explains
    await auto.chain_for_window("META", 700.0, 14, 30)
    assert "no expiry" in auto.options_error


# --------------------------------------------------------------------------- #
def test_auto_is_the_shipped_default(cfg):
    """Because the alternative is the user reading a log and running a
    command, which is what this replaces."""
    assert cfg.get("data.provider") == "auto"


def test_auto_builds_a_hybrid_that_can_fall_back(cfg, monkeypatch):
    from panaoptions.data.auto import AutoChains as AC
    from panaoptions.data.hybrid import HybridFeed
    from panaoptions.data.provider import make_feed

    monkeypatch.delenv("TRADIER_TOKEN", raising=False)
    feed = make_feed(cfg)
    assert isinstance(feed, HybridFeed)
    assert isinstance(feed.chains, AC)
    assert [getattr(c, "name", "") for c in feed.chains.candidates] == \
        ["yahoo", "cboe"]


def test_a_configured_token_is_tried_before_the_public_feeds(cfg, monkeypatch):
    """Setting one up is a deliberate choice for real-time data; a delayed
    fallback should not quietly win over it."""
    from panaoptions.data.provider import make_feed

    monkeypatch.setenv("TRADIER_TOKEN", "test-token")
    feed = make_feed(cfg)
    assert [getattr(c, "name", "") for c in feed.chains.candidates][0] == \
        "tradier"
