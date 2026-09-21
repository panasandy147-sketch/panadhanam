"""LLM provider abstraction.

Two providers, one interface. Every agent asks for a validated Pydantic object
and never learns which model produced it:

    anthropic  Claude. Best judgement, costs money, needs an API key.
    ollama     A model running on YOUR PC. Free, private, no signup, no key.

Local models are weaker at obeying a schema than Claude is, so the Ollama path
validates the response and retries once with the error fed back before giving
up. When it does give up the caller falls through to its deterministic rule
engine — the desk never goes dark because a model misbehaved.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Config, get_config
from app.core.logging import get_logger

log = get_logger("llm")

T = TypeVar("T", bound=BaseModel)

_anthropic_client: Any = None
_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #
async def _get_anthropic(cfg: Config) -> Any | None:
    global _anthropic_client
    if not cfg.anthropic_key:
        return None
    if _anthropic_client is not None:
        return _anthropic_client
    async with _lock:
        if _anthropic_client is None:
            try:
                from anthropic import AsyncAnthropic
                _anthropic_client = AsyncAnthropic(api_key=cfg.anthropic_key)
                log.info("LLM provider: anthropic (%s)", cfg.llm_model)
            except ImportError:
                log.warning("anthropic package not installed")
                return None
    return _anthropic_client


async def _anthropic_structured(cfg: Config, system: str, prompt: str,
                                schema: type[T], max_tokens: int) -> T | None:
    client = await _get_anthropic(cfg)
    if client is None:
        return None
    response = await client.messages.parse(
        model=cfg.llm_model,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": cfg.llm_effort},
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    return response.parsed_output


# --------------------------------------------------------------------------- #
# Ollama — a model running locally
# --------------------------------------------------------------------------- #
class _Health:
    """Stops a dead Ollama from producing one ERROR per agent per cycle.

    `OLLAMA_MODEL` in .env is enough to select the Ollama provider, so a
    machine where Ollama is configured but not installed will try — and fail —
    on every analyst of every cycle. That is five errors a minute saying the
    same thing, which buries the one line that matters underneath the noise it
    generates.

    So the first failure is logged loudly with the fix, and after that the
    provider is treated as down: calls return None immediately, without a
    socket attempt, until the cooldown expires and one probe is allowed
    through. Recovery is logged too, because "it started working again" is
    also something you want to see.
    """

    RETRY_AFTER = 120.0      # seconds before trying a dead provider again

    def __init__(self) -> None:
        self.down_since: float = 0.0
        self.reason: str = ""

    @property
    def is_down(self) -> bool:
        return bool(self.down_since)

    def should_skip(self) -> bool:
        if not self.down_since:
            return False
        if time.monotonic() - self.down_since < self.RETRY_AFTER:
            return True
        # Cooldown is up: let exactly one call through to test the water.
        self.down_since = time.monotonic()
        return False

    def mark_down(self, reason: str) -> None:
        first = not self.down_since
        self.down_since = time.monotonic()
        self.reason = reason
        if first:
            log.error("%s — agents fall back to their rule engines until it is "
                      "fixed. This will not be repeated every cycle.", reason)

    def mark_up(self) -> None:
        if self.down_since:
            log.info("Ollama is answering again — LLM reasoning is back on.")
        self.down_since = 0.0
        self.reason = ""


ollama_health = _Health()


async def _ollama_structured(cfg: Config, system: str, prompt: str,
                             schema: type[T], max_tokens: int) -> T | None:
    host = cfg.ollama_host.rstrip("/")
    model = cfg.ollama_model
    json_schema = schema.model_json_schema()

    if ollama_health.should_skip():
        return None

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    async with httpx.AsyncClient(timeout=cfg.ollama_timeout) as client:
        for attempt in (1, 2):
            try:
                r = await client.post(f"{host}/api/chat", json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    # Ollama constrains generation to this JSON Schema, which is
                    # what makes a small local model usable for structured work.
                    "format": json_schema,
                    "options": {
                        "temperature": 0.2,      # analysis, not prose
                        "num_predict": max_tokens,
                    },
                })
            except httpx.ConnectError:
                ollama_health.mark_down(
                    f"Ollama is not reachable at {host}. Install it from "
                    f"https://ollama.com/download, then run `ollama serve` and "
                    f"`ollama pull {model}` (see docs/OLLAMA.md)")
                return None
            except Exception as exc:
                log.warning("Ollama request failed: %s", exc)
                return None

            if r.status_code == 404:
                ollama_health.mark_down(
                    f"Ollama is running but has no model called '{model}'. "
                    f"Run: ollama pull {model}")
                return None
            if r.status_code != 200:
                log.warning("Ollama returned HTTP %s: %s", r.status_code, r.text[:200])
                return None

            content = (r.json().get("message") or {}).get("content", "")
            ollama_health.mark_up()
            try:
                return schema.model_validate_json(content)
            except (ValidationError, json.JSONDecodeError) as exc:
                if attempt == 1:
                    # Hand the model its own error and let it correct itself.
                    log.debug("Ollama schema miss, retrying once: %s",
                              str(exc)[:160])
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (f"That response did not match the required "
                                    f"schema: {str(exc)[:400]}\n\nReturn ONLY "
                                    f"valid JSON matching the schema exactly."),
                    })
                    continue
                log.warning("Ollama could not produce schema-valid output after "
                            "a retry — falling back to the rule engine.")
                return None
    return None


# --------------------------------------------------------------------------- #
# Public interface
# --------------------------------------------------------------------------- #
async def structured_complete(*, system: str, prompt: str, schema: type[T],
                              max_tokens: int = 2000,
                              cfg: Config | None = None) -> T | None:
    """Ask the configured provider for a validated object, or None."""
    cfg = cfg or get_config()
    provider = cfg.llm_provider

    if provider == "ollama":
        return await _ollama_structured(cfg, system, prompt, schema, max_tokens)
    if provider == "anthropic":
        return await _anthropic_structured(cfg, system, prompt, schema, max_tokens)

    log.warning("unknown LLM provider '%s' — no reasoning applied", provider)
    return None


async def probe() -> dict[str, Any]:
    """Health check for the active provider. Drives `run.py --check-llm`."""
    cfg = get_config()
    provider = cfg.llm_provider

    if provider == "ollama":
        host = cfg.ollama_host.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{host}/api/tags")
                if r.status_code != 200:
                    return {"provider": "ollama", "ok": False, "host": host,
                            "error": f"HTTP {r.status_code}"}
                installed = [m["name"] for m in r.json().get("models", [])]
                wanted = cfg.ollama_model
                # Ollama reports "llama3.1:8b"; a bare "llama3.1" should match.
                have = any(m == wanted or m.split(":")[0] == wanted.split(":")[0]
                           for m in installed)
                return {"provider": "ollama", "ok": have, "host": host,
                        "model": wanted, "installed": installed,
                        "error": None if have else
                        f"model '{wanted}' not installed — run: ollama pull {wanted}"}
        except httpx.ConnectError:
            return {"provider": "ollama", "ok": False, "host": host,
                    "error": "Ollama is not running. Start it, then retry."}
        except Exception as exc:
            return {"provider": "ollama", "ok": False, "host": host,
                    "error": str(exc)}

    if provider == "anthropic":
        return {"provider": "anthropic", "ok": bool(cfg.anthropic_key),
                "model": cfg.llm_model,
                "error": None if cfg.anthropic_key else "ANTHROPIC_API_KEY is not set"}

    if provider == "none":
        return {"provider": "none", "ok": False,
                "error": ("No LLM configured. Set OLLAMA_MODEL in .env for a "
                          "free local model (see docs/OLLAMA.md), or "
                          "ANTHROPIC_API_KEY for Claude.")}
    return {"provider": provider, "ok": False,
            "error": f"unknown provider '{provider}' — use 'anthropic' or 'ollama'"}
