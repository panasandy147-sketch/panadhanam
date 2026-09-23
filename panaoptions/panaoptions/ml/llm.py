"""A small Ollama client for the journal's prose. Optional, and never fatal.

Ollama only: it is free, local, needs no key, and is what is already installed
on this machine. The model writes explanations; it never decides a verdict, a
score, a stop or a size. Those stay deterministic, because a model that has
read a profitable trade is very good at finding reasons it was fine.

It returns None on any failure and the caller keeps its rules-written version.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from panaoptions.logging import get_logger

log = get_logger("llm")

T = TypeVar("T", bound=BaseModel)


class _Health:
    """Stops a dead Ollama producing one error per trade, for ever."""

    RETRY_AFTER = 300.0

    def __init__(self) -> None:
        self.down_since = 0.0
        self.reason = ""

    def should_skip(self) -> bool:
        if not self.down_since:
            return False
        if time.monotonic() - self.down_since < self.RETRY_AFTER:
            return True
        self.down_since = time.monotonic()
        return False

    def mark_down(self, reason: str) -> None:
        first = not self.down_since
        self.down_since = time.monotonic()
        self.reason = reason
        if first:
            log.warning("%s — journal cards fall back to the rules version. "
                        "This will not be repeated for every trade.", reason)

    def mark_up(self) -> None:
        if self.down_since:
            log.info("Ollama is answering again.")
        self.down_since = 0.0
        self.reason = ""


health = _Health()


def host(cfg) -> str:
    return str(os.getenv("OLLAMA_HOST")
               or cfg.get("journal.ollama_host", "http://127.0.0.1:11434")).rstrip("/")


def model(cfg) -> str:
    return str(os.getenv("OLLAMA_MODEL")
               or cfg.get("journal.ollama_model", "qwen2.5:7b"))


async def structured_complete(*, system: str, prompt: str, schema: type[T],
                              cfg: Any, max_tokens: int = 700) -> T | None:
    """Ask for a validated object, or None."""
    if health.should_skip():
        return None

    url, name = host(cfg), model(cfg)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt}]

    try:
        timeout = float(cfg.get("journal.ollama_timeout", 120))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{url}/api/chat", json={
                "model": name,
                "messages": messages,
                "stream": False,
                # Ollama constrains generation to the schema, which is what
                # makes a small local model usable for structured work.
                "format": schema.model_json_schema(),
                "options": {"temperature": 0.2, "num_predict": max_tokens},
            })
    except httpx.ConnectError:
        health.mark_down(f"Ollama is not reachable at {url}")
        return None
    except Exception as exc:                     # noqa: BLE001 - never fatal
        log.debug("Ollama request failed: %s", exc)
        return None

    if response.status_code == 404:
        health.mark_down(f"Ollama has no model called '{name}' "
                         f"(run: ollama pull {name})")
        return None
    if response.status_code != 200:
        log.debug("Ollama returned HTTP %s", response.status_code)
        return None

    health.mark_up()
    content = (response.json().get("message") or {}).get("content", "")
    try:
        return schema.model_validate_json(content)
    except (ValidationError, json.JSONDecodeError) as exc:
        log.debug("Ollama output did not match the schema: %s", str(exc)[:160])
        return None


async def probe(cfg) -> dict[str, Any]:
    """Health check for `run.py --check-llm`.

    Every return path carries the host AND the model, so the caller can always
    print the command that fixes it — a failure that cannot name the model is
    a failure you cannot act on.
    """
    url, name = host(cfg), model(cfg)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{url}/api/tags")
            if response.status_code != 200:
                return {"ok": False, "host": url, "model": name,
                        "error": f"HTTP {response.status_code}"}
            installed = [m["name"] for m in response.json().get("models", [])]
            have = any(m == name or m.split(":")[0] == name.split(":")[0]
                       for m in installed)
            return {"ok": have, "host": url, "model": name,
                    "installed": installed,
                    "error": None if have else
                    f"model '{name}' not installed — run: ollama pull {name}"}
    except httpx.ConnectError:
        return {"ok": False, "host": url, "model": name,
                "error": "Ollama is not reachable. Install it, or start it "
                         "with `ollama serve`."}
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "host": url, "model": name, "error": str(exc)}
