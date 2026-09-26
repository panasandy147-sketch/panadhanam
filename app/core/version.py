"""Which code is running — stamped on every trade so results can be compared
before and after a change, rather than blended into one 30-day number."""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def code_version() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, cwd=ROOT, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"
