"""Read and write `.env`, with no third-party dependency.

panaoptions keeps its own copy rather than importing panadhanam's: the two
apps are deliberately decoupled, and a shared helper is exactly the kind of
coupling that lets a change to one quietly alter the other.

Why a file at all, when os.getenv already works: an environment variable set
in a shell lasts until that shell closes. On Windows that is a trap — the
value works once, then reverts on the next launch, and the desk goes back to
refusing every trade with no visible change in configuration.
"""
from __future__ import annotations

import os
from pathlib import Path

SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "WEBHOOK")


def is_secret(key: str) -> bool:
    return any(hint in key.upper() for hint in SECRET_HINTS)


def mask(key: str, value: str) -> str:
    if not is_secret(key):
        return value
    return f"set ({len(value)} chars)" if value else "cleared"


def parse_assignment(text: str) -> tuple[str, str]:
    """'KEY=value' -> ('KEY', 'value'), with the shell's quoting stripped."""
    if "=" not in text:
        raise ValueError(f"'{text}' is not KEY=VALUE")
    key, _, value = text.partition("=")
    key = key.strip()
    if not key or not all(c.isalnum() or c == "_" for c in key):
        raise ValueError(f"'{key}' is not a valid environment variable name")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if "\n" in value or "\r" in value:
        raise ValueError(f"the value for {key} spans more than one line")
    return key, value


def read(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        try:
            key, value = parse_assignment(line)
        except ValueError:
            continue
        out[key] = value
    return out


def load(path: Path, override: bool = False) -> dict[str, str]:
    """Put the file's values into the process environment.

    A real environment variable wins by default: `PANAOPTIONS_CAPITAL=5000
    python run.py` must be able to override the file for one run without
    editing it.
    """
    values = read(path)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def write(path: Path, updates: dict[str, str]) -> dict[str, str]:
    """Apply `updates`, rewriting each key where it already sits.

    Every occurrence is rewritten, not just the first: a later duplicate wins
    when the file is read back, so leaving a stale one behind would silently
    undo the change just made.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    outcome: dict[str, str] = {}

    for key, value in updates.items():
        replaced = False
        for i, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.partition("=")[0].strip() != key:
                continue
            _, _, rest = raw.partition("=")
            comment = ""
            if "#" in rest and not rest.strip().startswith("#"):
                comment = "  " + rest[rest.index("#"):].strip()
            lines[i] = f"{key}={value}{comment}"
            replaced = True
        outcome[key] = "updated" if replaced else "added"
        if not replaced:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return outcome
