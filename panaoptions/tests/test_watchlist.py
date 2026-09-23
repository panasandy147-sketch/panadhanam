"""The watchlist: what the desk scans, typed on the dashboard.

The line this feature must not cross: choosing what to LOOK at and choosing
what to BUY are different powers, and only the first one belongs to whoever
last used the dashboard.
"""
from __future__ import annotations

import pytest

from panaoptions import watchlist as wl


@pytest.fixture(autouse=True)
def _store_elsewhere(tmp_path, monkeypatch):
    """Never write the developer's real watchlist from a test."""
    monkeypatch.setattr(wl, "STORE", tmp_path / "watchlist.json")


# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("QQQ,SPY", ["QQQ", "SPY"]),
    ("qqq, spy , aapl", ["QQQ", "SPY", "AAPL"]),      # case and spaces
    ("QQQ\nSPY\nAAPL", ["QQQ", "SPY", "AAPL"]),        # pasted a column
    ("QQQ SPY", ["QQQ", "SPY"]),                       # just spaces
    ("BRK-B, BF.B", ["BRK-B", "BF.B"]),                # real ticker shapes
    ("QQQ, QQQ, SPY", ["QQQ", "SPY"]),                 # a repeat is not an error
])
def test_people_paste_lists_in_every_shape(raw, expected):
    assert wl.parse(raw) == expected


def test_an_empty_list_is_refused_with_an_example():
    with pytest.raises(wl.WatchlistError) as exc:
        wl.parse("   ")
    assert "QQQ" in str(exc.value)          # says what a good answer looks like


def test_something_that_is_not_a_ticker_is_refused_by_name():
    """The message names the offending word, so it can be found and fixed.

    Note that NOT and A are perfectly good ticker shapes — the refusal here
    is about the punctuation, and saying which chunk failed is the difference
    between a fixable message and "invalid input".
    """
    with pytest.raises(wl.WatchlistError) as exc:
        wl.parse("QQQ, ticker!")
    assert "TICKER!" in str(exc.value)


def test_a_url_pasted_by_accident_is_refused_rather_than_sent_to_the_feed():
    with pytest.raises(wl.WatchlistError):
        wl.parse("https://finance.yahoo.com/chart/QQQ")


def test_too_many_symbols_is_refused_with_the_reason():
    """Each symbol costs feed requests every cycle.

    Past the cap the desk spends its time waiting on Yahoo instead of
    deciding, which looks exactly like being broken.
    """
    with pytest.raises(wl.WatchlistError) as exc:
        wl.parse(", ".join(f"AA{i:02d}" for i in range(wl.MAX_SYMBOLS + 1)))
    assert str(wl.MAX_SYMBOLS) in str(exc.value)


def test_a_saved_list_survives_a_restart():
    wl.save(["QQQ", "SPY"])
    assert wl.load() == ["QQQ", "SPY"]
    wl.clear()
    assert wl.load() == []


def test_a_hand_edited_file_with_rubbish_in_it_does_not_reach_the_feed():
    wl.STORE.parent.mkdir(parents=True, exist_ok=True)
    wl.STORE.write_text('{"symbols": ["QQQ", "oh dear", 42, "SPY"]}')
    assert wl.load() == ["QQQ", "SPY"]


def test_an_unreadable_file_falls_back_rather_than_crashing_the_desk():
    wl.STORE.parent.mkdir(parents=True, exist_ok=True)
    wl.STORE.write_text("{not json")
    assert wl.load() == []


# --------------------------------------------------------------------------- #
# On the desk
# --------------------------------------------------------------------------- #
def _desk(cfg, tmp_path, monkeypatch):
    from panaoptions.app import OptionsDesk
    from panaoptions.ledger import store

    monkeypatch.setattr(store, "_conn", None)
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "w.db")
    return OptionsDesk(cfg=cfg, feed=object())


def test_setting_the_universe_takes_effect_without_a_restart(cfg, tmp_path,
                                                             monkeypatch):
    desk = _desk(cfg, tmp_path, monkeypatch)
    desk._screened_on = "2026-09-23"
    desk.screened = ["stale"]

    desk.set_universe(["QQQ", "SPY"])

    assert desk.cfg.symbols == ["QQQ", "SPY"]
    # The old screen is dropped, so the new names are looked at on the next
    # cycle rather than tomorrow — the whole point of typing them.
    assert desk._screened_on == ""
    assert desk.screened == []
    assert wl.load() == ["QQQ", "SPY"]


def test_the_change_is_written_into_the_activity_log(cfg, tmp_path,
                                                     monkeypatch):
    desk = _desk(cfg, tmp_path, monkeypatch)
    desk.set_universe(["QQQ", "SPY"])
    kinds = [e["kind"] for e in desk.activity.recent(20)]
    assert "watchlist" in kinds


def test_resetting_goes_back_to_the_config_universe(cfg, tmp_path, monkeypatch):
    desk = _desk(cfg, tmp_path, monkeypatch)
    desk.set_universe(["QQQ"])
    symbols = desk.reset_universe()
    assert "QQQ" not in symbols or len(symbols) > 1
    assert wl.load() == []
