"""The symbols the desk scans, editable while it runs.

`universe.symbols` in the YAML is the default. This is the override: a list
typed on the dashboard, saved to disk so it survives a restart, and applied
without stopping the desk.

What this deliberately is NOT: a way to trade. Choosing what to LOOK at and
choosing what to BUY are different powers, and only the first one belongs to
whoever last used the dashboard. Every rule still has to agree before a
position is opened, and nothing here can open, size or close one.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from panaoptions.config import DATA_DIR
from panaoptions.logging import get_logger

log = get_logger("watchlist")

STORE = DATA_DIR / "watchlist.json"

# Tickers, including the forms that carry a suffix (BRK-B) or a dot (BF.B).
_VALID = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# Each symbol costs three feed requests per screen and one per hunt cycle.
# Past this the desk spends its cycle waiting on Yahoo instead of deciding,
# and starts drawing rate limits that make it look broken.
MAX_SYMBOLS = 20


class WatchlistError(ValueError):
    """A list that cannot be used, with a message meant for a person."""


def parse(raw: str) -> list[str]:
    """Split what somebody typed into symbols, or say why it will not do.

    Accepts commas, spaces and newlines, because people paste from all three.
    """
    chunks = [c.strip().upper() for c in re.split(r"[,\s]+", raw or "") if c.strip()]

    seen: list[str] = []
    for chunk in chunks:
        if not _VALID.match(chunk):
            raise WatchlistError(
                f"{chunk!r} is not a ticker. Letters, digits, dots and "
                f"hyphens only, up to 10 characters — for example: "
                f"QQQ, SPY, AAPL")
        if chunk not in seen:          # quietly drop a repeat, do not refuse
            seen.append(chunk)

    if not seen:
        raise WatchlistError(
            "No symbols. Type them separated by commas, for example: "
            "QQQ, SPY, AAPL")
    if len(seen) > MAX_SYMBOLS:
        raise WatchlistError(
            f"{len(seen)} symbols is too many — the limit is {MAX_SYMBOLS}. "
            f"Each one costs several data requests every cycle, and past this "
            f"the desk spends its time waiting on the feed instead of "
            f"deciding. Trim the list to the names you actually want watched.")
    return seen


def load() -> list[str]:
    """The saved override, or [] if there is none."""
    try:
        raw = json.loads(STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s (%s) — falling back to the config "
                    "universe", STORE, exc)
        return []
    symbols = raw.get("symbols") if isinstance(raw, dict) else raw
    if not isinstance(symbols, list):
        return []
    # Re-validate on the way in: the file is editable by hand, and a bad entry
    # should not reach the feed.
    return [s for s in (str(x).upper() for x in symbols) if _VALID.match(s)][:MAX_SYMBOLS]


def save(symbols: list[str]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps({"symbols": symbols}, indent=2),
                     encoding="utf-8")
    log.info("watchlist saved: %s", ", ".join(symbols))


def clear() -> None:
    """Go back to the universe in the config file."""
    Path(STORE).unlink(missing_ok=True)
    log.info("watchlist cleared — back to the config universe")
