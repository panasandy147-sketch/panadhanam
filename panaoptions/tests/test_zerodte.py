"""The zerodte profile: same-day options on the four 5-minute strategies."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest


@pytest.fixture
def zcfg(tmp_path, monkeypatch):
    from panaoptions import config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config_mod, "ENV_PATH", tmp_path / "absent.env")
    monkeypatch.delenv("PANAOPTIONS_CAPITAL", raising=False)
    return config_mod.Config(profile="zerodte")


def test_the_profile_is_same_day_with_the_asked_for_numbers(zcfg):
    g = zcfg.get
    assert (g("contracts.min_dte"), g("contracts.max_dte")) == (0, 4)
    assert g("contracts.prefer_nearest_expiry") is True
    assert (g("contracts.min_delta"), g("contracts.max_delta")) == (0.35, 0.50)
    assert g("contracts.max_spread_pct_of_mid") == 7.0
    assert (g("contracts.min_contract_price"), g("contracts.max_contract_price")) == (0.50, 3.50)
    assert g("strategies.orb_vwap.to") == "15:00"
    assert g("strategies.vwap_ema_pullback.to") == "15:00"
    assert g("strategies.liquidity_sweep.to") == "14:30"
    assert g("strategies.candlestick_at_level.timeframe") == "5m"
    assert g("risk.stop_mode") == "underlying"
    assert g("risk.disaster_stop_pct") == 45.0
    assert g("risk.daily_loss_limit_pct") == 10.0
    assert zcfg.last_entry_hhmm == "15:00"


def test_only_fast_patterns_are_traded(zcfg):
    allowed = set(zcfg.get("strategies.candlestick_at_level.allowed_patterns"))
    assert allowed == {"Hammer", "Shooting Star", "Bullish Engulfing",
                       "Bearish Engulfing", "Tweezer Bottom", "Tweezer Top"}


def _bars(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_an_engulfing_is_not_hidden_by_a_disallowed_structure():
    """detect() used to return only the strongest pattern, so an engulfing on
    the same bar as a three-line strike could never be traded by a desk that
    allows one and not the other."""
    from panaoptions.engine import patterns

    # Three falling candles, then one bullish candle engulfing all three:
    # a Bullish Three-Line Strike AND a Bullish Engulfing on the last bar.
    df = _bars([[110, 111, 105, 106, 1], [106, 107, 101, 102, 1],
                [102, 103, 97, 98, 1], [97, 112, 96.5, 111, 1]])
    strongest = patterns.detect(df)
    assert strongest is not None and strongest.name == "Bullish Three-Line Strike"
    only_fast = patterns.detect(df, allowed={"Bullish Engulfing"})
    assert only_fast is not None and only_fast.name == "Bullish Engulfing"
    assert patterns.detect(df, allowed={"Hammer"}) is None


def test_a_same_day_contract_gets_a_real_delta_from_yahoo_data():
    """Priced at 0 whole days, Black-Scholes returned delta 0 and every 0DTE
    contract failed the filter."""
    from panaoptions.data.feed import _days_to_close
    from panaoptions.data.greeks import delta

    at_noon = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)          # 12:00 ET
    days = _days_to_close(date(2026, 9, 25), now=at_noon)
    assert days == pytest.approx(4 / 24, abs=1e-6)                # 4 hours
    d = delta(100.0, 100.0, days, 0.30, is_call=True)
    assert 0.45 < d < 0.60


def test_the_nearest_expiry_is_preferred(zcfg):
    from panaoptions.engine import contracts
    from panaoptions.models import Direction, OptionContract, OptionRight

    def call(dte, mid):
        return OptionContract(symbol="SPY", right=OptionRight.CALL, strike=660,
                              expiry="2026-09-25", dte=dte, bid=mid - 0.02,
                              ask=mid + 0.02, delta=0.45)

    search = contracts.choose("SPY", [call(3, 1.10), call(0, 1.40)],
                              Direction.LONG, zcfg, budget=800)
    assert search.chosen.dte == 0


def test_start_puts_the_desk_on_zerodte_but_never_overrides_a_choice(tmp_path, monkeypatch):
    import run
    from panaoptions import config as config_mod

    env = tmp_path / ".env"
    monkeypatch.setattr(config_mod, "ENV_PATH", env)
    # Recorded, so whatever _ensure_profile writes to os.environ is undone.
    monkeypatch.setenv("PANAOPTIONS_PROFILE", "")
    monkeypatch.delenv("PANAOPTIONS_PROFILE", raising=False)
    env.write_text("PANAOPTIONS_CAPITAL=4000\n", encoding="utf-8")
    run._ensure_profile()
    assert "PANAOPTIONS_PROFILE=zerodte" in env.read_text(encoding="utf-8")

    monkeypatch.delenv("PANAOPTIONS_PROFILE", raising=False)
    env.write_text("PANAOPTIONS_PROFILE=default\n", encoding="utf-8")
    run._ensure_profile()
    assert "PANAOPTIONS_PROFILE=default" in env.read_text(encoding="utf-8")


def test_rules_describe_the_same_day_desk(zcfg):
    from panaoptions import rules

    doc = rules.build(zcfg, capital=4000)
    text = str(doc)
    assert "same-day options" in text
    assert "Hammer, Shooting Star, Bullish Engulfing" in text
    assert "Three" not in "".join(p["pattern"] for s in doc["sections"]
                                  for p in s.get("patterns", []))
