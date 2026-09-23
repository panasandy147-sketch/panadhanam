"""The journal's optional coach. Optional means every failure is survivable."""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from panaoptions.ml import llm


class _Answer(BaseModel):
    headline: str


def _client(*, status=200, payload=None, raises=None):
    class _Response:
        status_code = status

        def json(self):
            return payload or {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, *a, **k):
            if raises:
                raise raises
            return _Response()

        async def get(self, *a, **k):
            if raises:
                raise raises
            return _Response()

    return _Client


@pytest.fixture(autouse=True)
def fresh_health():
    llm.health.mark_up()
    yield
    llm.health.mark_up()


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_valid_answer_is_parsed(cfg, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _client(
        payload={"message": {"content": json.dumps({"headline": "a quiet week"})}}))

    answer = await llm.structured_complete(
        system="s", prompt="p", schema=_Answer, cfg=cfg)
    assert answer is not None
    assert answer.headline == "a quiet week"


@pytest.mark.asyncio
async def test_the_schema_is_sent_so_a_small_model_can_obey_it(cfg, monkeypatch):
    sent = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, json=None, **k):
            sent.update(json or {})

            class _R:
                status_code = 200

                def json(self_inner):
                    return {"message": {"content": '{"headline": "ok"}'}}

            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await llm.structured_complete(system="s", prompt="p", schema=_Answer, cfg=cfg)

    assert "format" in sent, "without a schema a 7B model will not return JSON"
    assert "headline" in sent["format"]["properties"]
    assert sent["options"]["temperature"] <= 0.3, "analysis, not prose"


@pytest.mark.asyncio
async def test_output_that_does_not_match_the_schema_returns_none(cfg, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _client(
        payload={"message": {"content": "not json at all"}}))
    assert await llm.structured_complete(
        system="s", prompt="p", schema=_Answer, cfg=cfg) is None


@pytest.mark.asyncio
async def test_a_missing_model_is_reported_once_not_per_trade(cfg, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(httpx, "AsyncClient", _client(status=404))
    monkeypatch.setattr(logging.getLogger("panaoptions"), "propagate", True)

    with caplog.at_level(logging.WARNING, logger="panaoptions.llm"):
        for _ in range(5):
            assert await llm.structured_complete(
                system="s", prompt="p", schema=_Answer, cfg=cfg) is None

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, \
        "one line per trade would bury the one line that matters"
    assert "ollama pull" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_a_dead_host_is_not_dialled_again_during_the_cooldown(cfg, monkeypatch):
    attempts = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, *a, **k):
            attempts.append(1)
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    for _ in range(4):
        await llm.structured_complete(system="s", prompt="p", schema=_Answer, cfg=cfg)

    assert len(attempts) == 1, "a dead provider must not be dialled every trade"


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_probe_names_the_model_even_when_it_cannot_connect(cfg, monkeypatch):
    # A failure that cannot name the model is a failure you cannot act on.
    monkeypatch.setattr(httpx, "AsyncClient",
                        _client(raises=httpx.ConnectError("refused")))
    result = await llm.probe(cfg)

    assert result["ok"] is False
    assert result["model"], "the fix command needs the model name"
    assert result["host"]


@pytest.mark.asyncio
async def test_the_probe_lists_what_is_installed_when_the_model_is_missing(
        cfg, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _client(
        payload={"models": [{"name": "llama3.1:8b"}]}))
    result = await llm.probe(cfg)

    assert result["ok"] is False
    assert result["installed"] == ["llama3.1:8b"]
    assert "ollama pull" in result["error"]


@pytest.mark.asyncio
async def test_a_tag_without_its_version_still_counts_as_installed(cfg, monkeypatch):
    cfg.data["journal"]["ollama_model"] = "qwen2.5"
    monkeypatch.setattr(httpx, "AsyncClient", _client(
        payload={"models": [{"name": "qwen2.5:7b"}]}))
    assert (await llm.probe(cfg))["ok"] is True


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_dead_coach_never_blocks_a_card(cfg, monkeypatch):
    from panaoptions.journal.grade import build_card
    from panaoptions.journal.models import JournalEntry, Verdict
    from panaoptions.models import SetupType

    cfg.data["journal"]["use_llm"] = True
    monkeypatch.setattr(httpx, "AsyncClient",
                        _client(raises=httpx.ConnectError("refused")))

    entry = JournalEntry(id="J1", trade_id="T1",
                         ts=__import__("datetime").datetime(2026, 9, 23),
                         symbol="SPY", contract="SPY 110C",
                         strategy=SetupType.ORB_VWAP, pnl=46.0,
                         verdict=Verdict.GOOD_WIN, execution_score=10)
    card = await build_card(entry, cfg)

    assert card.generated_by == "rules"
    assert card.what_happened, "the rules version must still be complete"
    assert card.verdict is Verdict.GOOD_WIN, "the model never decides a verdict"
