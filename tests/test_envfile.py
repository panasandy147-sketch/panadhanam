""".env editing — the file every per-machine decision lives in.

It is untracked by design, so a commit cannot change it and it has to be
edited on the machine itself. That makes a careless rewrite unrecoverable:
these tests exist so an edit never loses a key it was not asked to touch.
"""
from __future__ import annotations

import pytest

from app.core.envfile import is_secret, mask, parse_assignment, read_values, set_values


def _env(tmp_path, body: str):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
def test_an_existing_key_is_rewritten_where_it_already_sits(tmp_path):
    path = _env(tmp_path, "# capital\nTOTAL_CAPITAL=100000\nHOST=127.0.0.1\n")
    assert set_values(path, {"TOTAL_CAPITAL": "100"}) == {"TOTAL_CAPITAL": "updated"}

    lines = path.read_text().splitlines()
    assert lines == ["# capital", "TOTAL_CAPITAL=100", "HOST=127.0.0.1"], \
        "the key must keep its place so its comment stays attached to it"


def test_a_new_key_is_appended_and_nothing_else_moves(tmp_path):
    path = _env(tmp_path, "HOST=127.0.0.1\nPORT=8000\n")
    assert set_values(path, {"OLLAMA_MODEL": "qwen2.5:7b"}) == \
        {"OLLAMA_MODEL": "added"}

    values = read_values(path)
    assert values == {"HOST": "127.0.0.1", "PORT": "8000",
                      "OLLAMA_MODEL": "qwen2.5:7b"}


def test_every_duplicate_is_rewritten_not_just_the_first(tmp_path):
    # dotenv lets the LAST assignment win, so leaving a stale duplicate behind
    # would silently undo the change that was just made.
    path = _env(tmp_path, "TOTAL_CAPITAL=100000\nHOST=x\nTOTAL_CAPITAL=50000\n")
    set_values(path, {"TOTAL_CAPITAL": "100"})

    assert path.read_text().count("TOTAL_CAPITAL=100\n") == 2
    assert read_values(path)["TOTAL_CAPITAL"] == "100"


def test_a_trailing_comment_survives_the_edit(tmp_path):
    path = _env(tmp_path, "ALPACA_PAPER=true   # true = simulator\n")
    set_values(path, {"ALPACA_PAPER": "false"})
    assert path.read_text().strip() == "ALPACA_PAPER=false  # true = simulator"


def test_a_commented_out_key_is_left_alone(tmp_path):
    path = _env(tmp_path, "# TOTAL_CAPITAL=999999\nHOST=x\n")
    assert set_values(path, {"TOTAL_CAPITAL": "100"}) == {"TOTAL_CAPITAL": "added"}
    assert "# TOTAL_CAPITAL=999999" in path.read_text()


def test_a_missing_file_is_seeded_from_the_template(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text("HOST=127.0.0.1\nTOTAL_CAPITAL=100000\n", encoding="utf-8")
    path = tmp_path / ".env"

    set_values(path, {"TOTAL_CAPITAL": "100"}, template=template)
    assert read_values(path) == {"HOST": "127.0.0.1", "TOTAL_CAPITAL": "100"}


def test_a_missing_file_with_no_template_is_still_created(tmp_path):
    path = tmp_path / ".env"
    set_values(path, {"LLM_PROVIDER": "ollama"}, template=tmp_path / "nope")
    assert read_values(path) == {"LLM_PROVIDER": "ollama"}


def test_reading_ignores_comments_blanks_and_junk(tmp_path):
    path = _env(tmp_path, "\n# a note\nHOST=127.0.0.1\nnot an assignment\n"
                          "  PORT = 8000  \nBAD KEY=1\n")
    assert read_values(path) == {"HOST": "127.0.0.1", "PORT": "8000"}


def test_reading_a_file_that_is_not_there_is_not_an_error(tmp_path):
    assert read_values(tmp_path / "nothing") == {}


# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,expected", [
    ("TOTAL_CAPITAL=100", ("TOTAL_CAPITAL", "100")),
    ("LLM_PROVIDER=ollama", ("LLM_PROVIDER", "ollama")),
    ('MODEL="qwen2.5:7b"', ("MODEL", "qwen2.5:7b")),
    ("MODEL='qwen2.5:7b'", ("MODEL", "qwen2.5:7b")),
    ("  HOST = 127.0.0.1  ", ("HOST", "127.0.0.1")),
    ("CLEARED=", ("CLEARED", "")),
])
def test_assignments_are_parsed_the_way_a_shell_would(text, expected):
    assert parse_assignment(text) == expected


@pytest.mark.parametrize("text", [
    "TOTAL_CAPITAL", "=100", "BAD KEY=1", "KEY=line\nother=1",
])
def test_a_malformed_assignment_is_refused_rather_than_guessed(text):
    with pytest.raises(ValueError):
        parse_assignment(text)


# --------------------------------------------------------------------------- #
def test_secrets_are_confirmed_but_never_echoed():
    assert is_secret("ALPACA_API_KEY") and is_secret("KITE_API_SECRET")
    assert not is_secret("TOTAL_CAPITAL")

    printed = mask("ALPACA_API_SECRET", "abcd1234")
    assert "abcd1234" not in printed and "8 chars" in printed
    assert mask("ALPACA_API_KEY", "") == "cleared"
    assert mask("TOTAL_CAPITAL", "100") == "100"
