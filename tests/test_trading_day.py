"""Trading Day: explicit, per-session arming of live order placement."""
from __future__ import annotations

import pytest

from app.brokers.paper import PaperBroker
from app.core.models import Instrument, Side, SignalStatus, TradeSignal
from app.scheduler import TradingEngine


@pytest.fixture
async def engine(cfg):
    cfg.switch_market("IN")
    cfg.settings["system"]["square_off_time"] = "23:59"
    cfg.settings["execution"]["auto_place_orders"] = True
    broker = PaperBroker(config={"total_capital": 100_000})
    await broker.connect()
    eng = TradingEngine(broker, cfg)
    eng.risk.set_capital(100_000)
    yield eng
    cfg.settings["execution"]["auto_place_orders"] = False


def _signal() -> TradeSignal:
    return TradeSignal(
        id="SIG-TD", instrument=Instrument(symbol="RELIANCE",
                                           tradingsymbol="RELIANCE"),
        side=Side.BUY, entry=1000.0, stop_loss=980.0, target=1040.0,
        quantity=50, risk_reward=2.0, status=SignalStatus.APPROVED)


# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_config_flag_alone_does_not_place_orders(engine):
    """auto_place_orders left on from last week must not trade today's market."""
    assert engine.cfg.get("execution.auto_place_orders") is True
    assert engine.trading_day.armed is False

    result = await engine.desk.dispatcher.dispatch(_signal())
    assert result["order"]["ok"] is False
    assert "alert-only" in result["order"]["message"]


@pytest.mark.asyncio
async def test_arming_lets_orders_through(engine):
    assert (await engine.trading_day.start())["armed"] is True
    assert engine.trading_day.armed is True

    result = await engine.desk.dispatcher.dispatch(_signal())
    assert result["order"]["ok"] is True


@pytest.mark.asyncio
async def test_stopping_disarms_immediately(engine):
    await engine.trading_day.start()
    await engine.trading_day.stop()
    assert engine.trading_day.armed is False

    result = await engine.desk.dispatcher.dispatch(_signal())
    assert result["order"]["ok"] is False


@pytest.mark.asyncio
async def test_arming_expires_with_the_session_date(engine):
    """Arming covers ONE date. It must not survive into the next day."""
    await engine.trading_day.start()
    assert engine.trading_day.armed is True

    engine.trading_day._armed_for = "2020-01-01"     # yesterday, in effect
    assert engine.trading_day.armed is False


@pytest.mark.asyncio
async def test_arming_auto_disarms_after_square_off(engine, cfg):
    await engine.trading_day.start()
    assert engine.trading_day.armed is True

    cfg.settings["system"]["square_off_time"] = "00:00"   # always past it
    assert engine.trading_day.armed is False
    assert "square-off" in engine.trading_day.status()["disarm_reason"]
    cfg.settings["system"]["square_off_time"] = "23:59"


@pytest.mark.asyncio
async def test_cannot_arm_after_square_off(engine, cfg):
    cfg.settings["system"]["square_off_time"] = "00:00"
    result = await engine.trading_day.start()
    assert result["armed"] is False
    assert "square-off" in result["reason"].lower()
    cfg.settings["system"]["square_off_time"] = "23:59"


@pytest.mark.asyncio
async def test_cannot_arm_a_halted_desk(engine):
    engine.risk.state.halted = True
    engine.risk.state.halt_reason = "Daily loss limit breached"
    result = await engine.trading_day.start()
    assert result["armed"] is False
    assert "halted" in result["reason"].lower()


@pytest.mark.asyncio
async def test_a_real_money_account_still_needs_the_live_switches(engine, monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("ENABLE_LIVE_ORDERS", raising=False)
    engine.broker.is_paper_account = False

    result = await engine.trading_day.start()
    assert result["armed"] is False
    assert "real-money" in result["reason"].lower()


@pytest.mark.asyncio
async def test_status_reports_whether_orders_will_actually_be_placed(engine, cfg):
    await engine.trading_day.start()
    assert engine.trading_day.status()["orders_will_be_placed"] is True

    # Armed, but the execution switch is off: alerts, not orders.
    cfg.settings["execution"]["auto_place_orders"] = False
    assert engine.trading_day.status()["orders_will_be_placed"] is False
    assert engine.trading_day.status()["armed"] is True


@pytest.mark.asyncio
async def test_the_day_report_has_the_shape_the_ui_needs(engine):
    from app.storage import db
    db.init_db()

    report = engine.trading_day.report()
    for key in ("date", "market", "broker", "is_paper_account", "data_source",
                "signals_generated", "trades_taken", "trades_closed",
                "win_rate", "total_r", "pnl", "rejected", "top_rejections",
                "trades", "risk"):
        assert key in report, f"missing {key}"
    assert isinstance(report["trades"], list)
    assert isinstance(report["top_rejections"], list)


@pytest.mark.asyncio
async def test_the_desk_keeps_analysing_while_disarmed(engine):
    """Disarmed means no ORDERS — signals and alerts must still flow, or you
    lose the ability to watch a day without trading it."""
    assert engine.trading_day.armed is False
    result = await engine.desk.dispatcher.dispatch(_signal())
    assert result["dispatched"] is True        # the alert still went out
    assert result["order"]["ok"] is False      # but no order


# --------------------------------------------------------------------------- #
# settings.yaml is tracked, so editing auto_place_orders there is a change the
# next `git pull` will argue with. .env has to be able to win.
# --------------------------------------------------------------------------- #
def test_env_can_turn_order_placement_on_over_the_tracked_yaml(cfg, monkeypatch):
    cfg.settings["execution"]["auto_place_orders"] = False
    monkeypatch.setenv("AUTO_PLACE_ORDERS", "true")
    cfg.reload()
    assert cfg.get("execution.auto_place_orders") is True


def test_env_can_also_force_order_placement_off(cfg, monkeypatch):
    cfg.settings["execution"]["auto_place_orders"] = True
    monkeypatch.setenv("AUTO_PLACE_ORDERS", "false")
    cfg.reload()
    assert cfg.get("execution.auto_place_orders") is False


def test_yaml_still_decides_when_env_says_nothing(cfg, monkeypatch):
    monkeypatch.delenv("AUTO_PLACE_ORDERS", raising=False)
    cfg.reload()
    assert cfg.get("execution.auto_place_orders") is False, \
        "the shipped default must stay alert-only"


# --------------------------------------------------------------------------- #
# The header badge has three states to describe, not two.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_status_distinguishes_simulated_orders_from_alert_only(engine, cfg):
    # auto_place_orders on + a paper broker means orders ARE placed, simulated.
    # Reporting that as alert-only told people nothing would be sent while the
    # desk was filling positions.
    cfg.settings["execution"]["auto_place_orders"] = True
    status = engine.status()

    assert status["auto_place_orders"] is True
    assert status["live_orders"] is False, "no real-money switches are set"
    assert status["is_paper_account"] is True, \
        "the dashboard needs this to tell simulated orders from no orders"


@pytest.mark.asyncio
async def test_status_reports_whether_today_is_armed(engine):
    assert engine.status()["armed"] is False
    await engine.trading_day.start()
    assert engine.status()["armed"] is True


# --------------------------------------------------------------------------- #
# Auto-arming: paper accounts only, and never twice a day.
# --------------------------------------------------------------------------- #
@pytest.fixture
def market_open(engine, monkeypatch):
    """Pin the session to open. Auto-arm only fires during the session, so
    without this the tests pass or fail according to the wall clock."""
    monkeypatch.setattr(engine, "session_phase", lambda: "open")
    return engine


@pytest.mark.asyncio
async def test_auto_arm_waits_for_the_session_to_open(engine, cfg, monkeypatch):
    cfg.settings.setdefault("trading_day", {})["auto_arm_on_open"] = True
    monkeypatch.setattr(engine, "session_phase", lambda: "premarket")

    assert await engine.trading_day.maybe_auto_arm() is None
    assert not engine.trading_day.armed
@pytest.mark.asyncio
async def test_the_desk_arms_itself_at_the_open_on_a_paper_account(market_open, engine, cfg):
    cfg.settings.setdefault("trading_day", {})["auto_arm_on_open"] = True
    assert not engine.trading_day.armed

    result = await engine.trading_day.maybe_auto_arm()
    assert result and result["armed"] is True
    assert engine.trading_day.armed
    assert engine.trading_day.status()["armed_by"] == "auto"


@pytest.mark.asyncio
async def test_pressing_the_button_is_still_recorded_as_yours(engine, cfg):
    await engine.trading_day.start()
    assert engine.trading_day.status()["armed_by"] == "you"


@pytest.mark.asyncio
async def test_auto_arm_is_refused_on_a_real_money_account(market_open, engine, cfg, monkeypatch):
    # Deliberately not configurable. Committing real capital is a decision a
    # person takes each morning, not one a config file takes overnight.
    cfg.settings.setdefault("trading_day", {})["auto_arm_on_open"] = True
    monkeypatch.setattr(engine.broker, "is_paper_account", False, raising=False)

    assert await engine.trading_day.maybe_auto_arm() is None
    assert not engine.trading_day.armed, \
        "no setting may arm a real-money account without a person"


@pytest.mark.asyncio
async def test_a_refused_auto_arm_is_not_retried_every_cycle(market_open, engine, cfg, monkeypatch):
    cfg.settings.setdefault("trading_day", {})["auto_arm_on_open"] = True
    monkeypatch.setattr(engine.broker, "is_paper_account", False, raising=False)

    calls = []
    original = engine.trading_day.start

    async def _counted(by="you"):
        calls.append(by)
        return await original(by=by)

    monkeypatch.setattr(engine.trading_day, "start", _counted)
    for _ in range(5):
        await engine.trading_day.maybe_auto_arm()
    assert calls == []


@pytest.mark.asyncio
async def test_auto_arm_does_nothing_when_switched_off(market_open, engine, cfg):
    cfg.settings.setdefault("trading_day", {})["auto_arm_on_open"] = False
    assert await engine.trading_day.maybe_auto_arm() is None
    assert not engine.trading_day.armed


@pytest.mark.asyncio
async def test_auto_arm_leaves_an_already_armed_day_alone(market_open, engine, cfg):
    cfg.settings.setdefault("trading_day", {})["auto_arm_on_open"] = True
    await engine.trading_day.start()
    armed_at = engine.trading_day.status()["armed_at"]

    assert await engine.trading_day.maybe_auto_arm() is None
    assert engine.trading_day.status()["armed_at"] == armed_at


@pytest.mark.asyncio
async def test_stopping_the_day_is_not_undone_by_the_next_auto_arm(market_open, engine, cfg):
    # Pressing Stop must mean stopped. Re-arming what somebody just disarmed
    # would make the button useless.
    cfg.settings.setdefault("trading_day", {})["auto_arm_on_open"] = True
    await engine.trading_day.maybe_auto_arm()
    await engine.trading_day.stop()
    assert not engine.trading_day.armed

    await engine.trading_day.maybe_auto_arm()
    assert not engine.trading_day.armed


# --------------------------------------------------------------------------- #
# The end-of-day summary publishes itself after square-off.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_day_summary_publishes_once_after_square_off(engine, cfg,
                                                               monkeypatch):
    from datetime import datetime

    from app.core import clock as clock_mod

    cfg.settings["system"]["square_off_time"] = "15:15"
    cfg.settings.setdefault("trading_day", {})["summary_after_square_off_minutes"] = 5
    monkeypatch.setattr(clock_mod, "market_now",
                        lambda tz: datetime(2026, 9, 22, 15, 25))

    published = []
    monkeypatch.setattr("app.scheduler.bus.publish",
                        lambda topic, data: published.append((topic, data))
                        or _done())

    await engine._maybe_publish_day_summary()
    assert [t for t, _ in published] == ["trading_day.summary"]

    # Once a day, not once a cycle.
    await engine._maybe_publish_day_summary()
    assert len(published) == 1


async def _done():
    return None


@pytest.mark.asyncio
async def test_nothing_is_published_before_square_off(engine, cfg, monkeypatch):
    from datetime import datetime

    from app.core import clock as clock_mod

    cfg.settings["system"]["square_off_time"] = "15:15"
    monkeypatch.setattr(clock_mod, "market_now",
                        lambda tz: datetime(2026, 9, 22, 14, 0))

    published = []
    monkeypatch.setattr("app.scheduler.bus.publish",
                        lambda topic, data: published.append(topic) or _done())

    await engine._maybe_publish_day_summary()
    assert published == []


@pytest.mark.asyncio
async def test_the_summary_reaches_the_dashboard_payload(engine, cfg, monkeypatch):
    from datetime import datetime

    from app.core import clock as clock_mod

    cfg.settings["system"]["square_off_time"] = "15:15"
    monkeypatch.setattr(clock_mod, "market_now",
                        lambda tz: datetime(2026, 9, 22, 15, 30))
    monkeypatch.setattr("app.scheduler.bus.publish",
                        lambda topic, data: _done())

    assert engine.status()["day_summary"] is None
    await engine._maybe_publish_day_summary()

    summary = engine.status()["day_summary"]
    assert summary is not None
    assert "published_at" in summary
    assert "trades" in summary and "top_rejections" in summary


@pytest.mark.asyncio
async def test_a_weekend_publishes_nothing(engine, cfg, monkeypatch):
    from datetime import datetime

    from app.core import clock as clock_mod

    monkeypatch.setattr(clock_mod, "market_now",
                        lambda tz: datetime(2026, 9, 26, 16, 0))   # Saturday
    published = []
    monkeypatch.setattr("app.scheduler.bus.publish",
                        lambda topic, data: published.append(topic) or _done())

    await engine._maybe_publish_day_summary()
    assert published == []
