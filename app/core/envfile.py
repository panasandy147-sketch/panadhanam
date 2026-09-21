"""Read and rewrite `.env` in place, preserving comments and ordering.

`.env` is where every per-machine decision lives — capital, the LLM provider,
whether the desk may place orders. It is deliberately untracked, so it cannot
be changed by a commit and has to be edited on the machine itself. Hand-editing
it on Windows is where this setup keeps going wrong: Notepad appends `.txt`,
smart quotes creep in, a key gets duplicated and the second one silently wins.

So the app edits it: `python run.py --set KEY=VALUE`. The rules are boring on
purpose — an existing key is rewritten where it already sits (its surrounding
comments stay attached to it), a new key is appended, and nothing else in the
file is touched.
"""
from __future__ import annotations

from pathlib import Path

# Keys whose values must never be echoed back to a terminal or a log.
SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASS", "WEBHOOK")


def is_secret(key: str) -> bool:
    return any(hint in key.upper() for hint in SECRET_HINTS)


def mask(key: str, value: str) -> str:
    """What may safely be printed. Secrets are confirmed, never shown."""
    if not is_secret(key):
        return value
    return f"set ({len(value)} chars)" if value else "cleared"


def parse_assignment(text: str) -> tuple[str, str]:
    """'KEY=value' -> ('KEY', 'value'), with the shell's quoting stripped."""
    if "=" not in text:
        raise ValueError(f"'{text}' is not KEY=VALUE")
    key, _, value = text.partition("=")
    key = key.strip()
    if not key:
        raise ValueError(f"'{text}' has no key before the '='")
    if not all(c.isalnum() or c == "_" for c in key):
        raise ValueError(f"'{key}' is not a valid environment variable name")

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if "\n" in value or "\r" in value:
        raise ValueError(f"the value for {key} spans more than one line")
    return key, value


def read_values(path: Path) -> dict[str, str]:
    """Every assignment in the file. A later line wins, as dotenv does."""
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


def set_values(path: Path, updates: dict[str, str],
               template: Path | None = None) -> dict[str, str]:
    """Apply `updates` to `path`. Returns {key: "updated" | "added"}.

    A key that appears more than once is rewritten at EVERY occurrence, not
    just the first. dotenv lets the last one win, so leaving a stale duplicate
    behind would quietly undo the change that was just made.
    """
    if not path.exists() and template and template.exists():
        path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    elif not path.exists():
        path.write_text("# panadhanam local settings\n", encoding="utf-8")

    lines = path.read_text(encoding="utf-8").splitlines()
    outcome: dict[str, str] = {}

    for key, value in updates.items():
        replaced = False
        for i, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            existing = stripped.partition("=")[0].strip()
            if existing != key:
                continue
            # Keep any trailing comment: it explains the setting, and the
            # explanation is still true whatever the value is.
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

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return outcome
