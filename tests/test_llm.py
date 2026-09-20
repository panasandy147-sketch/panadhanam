"""LLM provider abstraction: Claude and local Ollama are interchangeable."""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from app.core import llm


class _Verdict(BaseModel):
    answer: str = Field(description="a word")
    score: float = Field(description="0..1")


@pytest.fixture
def ollama_cfg(cfg, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    return cfg


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #
def test_no_config_means_no_llm(cfg, monkeypatch):
    for var in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "OLLAMA_MODEL", "USE_OLLAMA"):
        monkeypatch.delenv(var, raising=False)
    assert cfg.llm_provider == "none"
    assert cfg.llm_enabled is False
    assert cfg.llm_label == "rule-based"


def test_an_anthropic_key_selects_claude(cfg, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert cfg.llm_provider == "anthropic"
    assert cfg.llm_enabled is True


def test_ollama_is_inferred_without_an_explicit_provider(cfg, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    assert cfg.llm_provider == "ollama"
    assert "local" in cfg.llm_label


def test_explicit_provider_wins_over_inference(cfg, monkeypatch):
    """A key present but Ollama chosen deliberately must stay on Ollama —
    otherwise someone testing locally gets billed by surprise."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert cfg.llm_provider == "ollama"


def test_ollama_defaults_are_sane(cfg, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("OLLAMA_TIMEOUT", raising=False)
    assert cfg.ollama_host == "http://127.0.0.1:11434"
    # Local models on CPU are slow; a short timeout would look like a crash.
    assert cfg.ollama_timeout >= 60


# --------------------------------------------------------------------------- #
# The Ollama transport
# --------------------------------------------------------------------------- #
# Capture the real class ONCE, before any patching: a lambda that patches
# httpx.AsyncClient and then calls httpx.AsyncClient recurses forever.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_httpx(monkeypatch, handler):
    """Route every AsyncClient the code under test builds to our handler."""
    transport = httpx.MockTransport(handler)

    def _factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_ollama_returns_a_validated_object(ollama_cfg, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # The JSON Schema must be sent — that is what constrains a small model.
        assert "format" in body and body["format"]["type"] == "object"
        assert body["stream"] is False
        return httpx.Response(200, json={
            "message": {"content": json.dumps({"answer": "pong", "score": 0.9})}})

    _patch_httpx(monkeypatch, handler)
    out = await llm.structured_complete(
        system="s", prompt="p", schema=_Verdict, cfg=ollama_cfg)
    assert out is not None
    assert out.answer == "pong"


@pytest.mark.asyncio
async def test_ollama_retries_once_when_the_schema_is_missed(ollama_cfg, monkeypatch):
    """Small local models fumble structured output. One corrective retry
    recovers most of those without falling back to the rule engine."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"message": {"content": "Sure! pong."}})
        body = json.loads(request.content)
        # The correction must include the failed attempt and the error.
        assert len(body["messages"]) >= 4
        return httpx.Response(200, json={
            "message": {"content": json.dumps({"answer": "pong", "score": 1.0})}})

    _patch_httpx(monkeypatch, handler)
    out = await llm.structured_complete(
        system="s", prompt="p", schema=_Verdict, cfg=ollama_cfg)
    assert calls["n"] == 2
    assert out.answer == "pong"


@pytest.mark.asyncio
async def test_ollama_gives_up_after_one_retry(ollama_cfg, monkeypatch):
    """Returning None is correct: the caller then uses its rule engine rather
    than acting on garbage."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"message": {"content": "not json at all"}})

    _patch_httpx(monkeypatch, handler)
    out = await llm.structured_complete(
        system="s", prompt="p", schema=_Verdict, cfg=ollama_cfg)
    assert out is None
    assert calls["n"] == 2, "exactly one retry, then stop"


@pytest.mark.asyncio
async def test_ollama_not_running_returns_none_not_an_exception(ollama_cfg, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_httpx(monkeypatch, handler)
    assert await llm.structured_complete(
        system="s", prompt="p", schema=_Verdict, cfg=ollama_cfg) is None


@pytest.mark.asyncio
async def test_a_missing_model_returns_none(ollama_cfg, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    _patch_httpx(monkeypatch, handler)
    assert await llm.structured_complete(
        system="s", prompt="p", schema=_Verdict, cfg=ollama_cfg) is None


# --------------------------------------------------------------------------- #
# Health probe
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_reports_an_installed_model(ollama_cfg, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})

    _patch_httpx(monkeypatch, handler)
    info = await llm.probe()
    assert info["provider"] == "ollama"
    assert info["ok"] is True


@pytest.mark.asyncio
async def test_probe_tells_you_the_exact_pull_command(ollama_cfg, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]})

    _patch_httpx(monkeypatch, handler)
    info = await llm.probe()
    assert info["ok"] is False
    assert "ollama pull qwen2.5:7b" in info["error"]


@pytest.mark.asyncio
async def test_probe_matches_a_bare_model_name_against_a_tagged_one(cfg, monkeypatch):
    """`OLLAMA_MODEL=qwen2.5` should match an installed `qwen2.5:7b`."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})

    _patch_httpx(monkeypatch, handler)
    assert (await llm.probe())["ok"] is True


@pytest.mark.asyncio
async def test_probe_explains_when_nothing_is_configured(cfg, monkeypatch):
    for var in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "OLLAMA_MODEL", "USE_OLLAMA"):
        monkeypatch.delenv(var, raising=False)
    info = await llm.probe()
    assert info["ok"] is False
    assert "OLLAMA_MODEL" in info["error"] or "ANTHROPIC_API_KEY" in info["error"]
