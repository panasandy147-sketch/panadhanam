"""The dashboard: a small FastAPI app over the desk that is already running.

The desk runs whether or not anyone is looking at it — the web layer only
reads. There are no endpoints that open or close a position, because a trading
decision belongs to the rules engine, not to whoever last clicked a button.

The one thing this page does that the terminal cannot: keep the configuration
check permanently on screen. A desk that is scanning and taking nothing looks
identical to a quiet market, and that is the single most expensive confusion
in this whole system.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from panaoptions import clock, preflight
from panaoptions.config import get_config
from panaoptions.ledger import store
from panaoptions.logging import get_logger

log = get_logger("web")

STATIC = Path(__file__).resolve().parent / "static"


def create_app(desk: Any, cycle_seconds: int = 60) -> FastAPI:
    app = FastAPI(title="panaoptions", docs_url="/api/docs")
    cfg = get_config()

    @app.on_event("startup")
    async def _start() -> None:
        store.init()
        app.state.desk_task = asyncio.create_task(desk.start(cycle_seconds))
        log.info("dashboard ready — http://127.0.0.1:%s",
                 app.state.port if hasattr(app.state, "port") else 8100)

    @app.on_event("shutdown")
    async def _stop() -> None:
        await desk.stop()
        task = getattr(app.state, "desk_task", None)
        if task:
            task.cancel()

    # ---------------------------------------------------------------- #
    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        out = desk.status()
        out["config"] = _config_payload(cfg)
        out["universe"] = cfg.symbols
        out["profile"] = cfg.profile_label
        # Charts and option chains are different Yahoo hosts and fail
        # independently. A desk whose charts work can still be unable to
        # price a single contract, and from the outside that looks exactly
        # like a quiet market.
        from panaoptions.data.provider import describe as describe_provider

        who = describe_provider(cfg)
        out["feed"] = {
            "provider": who["provider"],
            "note": who["note"],
            "ready": who["ready"],
            "options_available": getattr(desk.feed, "options_available", None),
            "options_error": getattr(desk.feed, "options_error", ""),
        }
        out["session"] = {
            "timeframe": cfg.get("technical.timeframe"),
            "dte": f"{cfg.get('contracts.min_dte')}-{cfg.get('contracts.max_dte')}",
            "entry_open": cfg.get("session.entry_open"),
            # What the desk actually honours, which is the last strategy's
            # close — showing the configured 10:30 would be a lie on screen.
            "entry_close": cfg.last_entry_hhmm,
            "tighten_stops_at": cfg.get("session.tighten_stops_at"),
            "force_exit_at": cfg.get("session.force_exit_at"),
            "timezone": cfg.timezone,
        }
        return out

    @app.get("/api/config-check")
    async def config_check() -> dict[str, Any]:
        return _config_payload(cfg)

    @app.get("/api/screen")
    async def screen() -> dict[str, Any]:
        """The pre-market read. Cached from the desk's own daily run."""
        return {
            "screened_on": desk._screened_on,
            "reads": [r.model_dump() for r in desk.screened],
            "thresholds": {
                "min_rvol": cfg.get("premarket.min_rvol"),
                "min_gap_pct": cfg.get("premarket.min_gap_pct"),
            },
        }

    @app.get("/api/trades")
    async def trades(days: int = 30) -> dict[str, Any]:
        from datetime import date, timedelta

        since = (date.today() - timedelta(days=days)).isoformat()
        rows = store.trades(limit=500, since=since)
        return {
            "days": days,
            "trades": rows,
            "sessions": store.sessions(limit=days),
            "stats": desk.ledger.stats(),
            # What is on RIGHT NOW. The panel used to count only closed
            # trades, so a paper trade opened a minute ago left "Trades 0" on
            # screen and read as though nothing had been bought at all.
            "open": [t.model_dump(mode="json")
                     for t in desk.ledger.open_trades.values()],
            # Why setups did not become trades. On a desk that is taking
            # nothing, this is the panel that explains it.
            "rejections": store.rejection_tally(since),
        }

    @app.get("/api/contracts")
    async def contracts() -> dict[str, Any]:
        """What the current budget buys, estimated without a live chain."""
        from panaoptions.data.greeks import atm_premium_estimate

        multiplier = cfg.multiplier
        budget = cfg.capital * float(cfg.get("risk.max_capital_deployed_pct", 20)) / 100
        dte = int(cfg.get("contracts.min_dte", 7))

        rows = []
        for symbol in cfg.symbols:
            typical = preflight._TYPICAL.get(symbol.upper())
            if not typical:
                continue
            spot, iv = typical
            cost = atm_premium_estimate(spot, iv, dte) * multiplier
            rows.append({"symbol": symbol, "spot": spot,
                         "atm_cost": round(cost, 0),
                         "affordable": cost <= budget})
        return {"budget": round(budget, 2), "rows": rows,
                "delta_band": [cfg.get("contracts.min_delta"),
                               cfg.get("contracts.max_delta")]}

    @app.get("/api/journal")
    async def journal(days: int = 30) -> dict[str, Any]:
        """Graded trades: which strategy pays, and what indiscipline cost."""
        from datetime import date, timedelta

        from panaoptions.journal.analytics import analyse
        from panaoptions.journal.store import cards, entries

        since = (date.today() - timedelta(days=days)).isoformat()
        rows = entries(limit=500, since=since)
        from panaoptions.ml import llm

        # "Configured" and "working" look identical from a dashboard unless
        # something says which you are getting.
        coach = {"configured": bool(cfg.get("journal.use_llm", False)),
                 "model": llm.model(cfg), "down_reason": llm.health.reason}
        coach["writing_cards"] = coach["configured"] and not llm.health.reason

        return {"days": days, "entries": rows, "stats": analyse(rows),
                "cards": cards(limit=10), "coach": coach}

    @app.get("/api/candles/{symbol}")
    async def candles(symbol: str, timeframe: str = "5m", count: int = 180,
                      session: bool = True) -> dict[str, Any]:
        """OHLCV for the chart. Lightweight-charts wants seconds, not ISO.

        `session=true` (the default) returns only the LATEST trading day.
        Three days of 5m bars on one axis compresses today into the right-hand
        third, which is the part anyone is actually reading — the pattern
        fired on today's tape, so today's tape is what the panel should show.

        "Today" is taken from the last bar's exchange date rather than from
        the wall clock, so the chart still shows a complete last session
        before the open, after the close, and at a weekend.
        """
        from zoneinfo import ZoneInfo

        bars = await desk.feed.candles(symbol.upper(), timeframe)
        exchange = ZoneInfo(cfg.timezone)

        if session and bars:
            day = bars[-1].ts.astimezone(exchange).date()
            bars = [b for b in bars if b.ts.astimezone(exchange).date() == day]

        return {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            # The chart library renders epochs in UTC, so it needs telling
            # which clock the session is on; New York is the only one that
            # makes sense here and the axis must not silently show UTC.
            "timezone": cfg.timezone,
            "session_only": bool(session),
            "candles": [
                {"time": int(c.ts.timestamp()), "open": c.open, "high": c.high,
                 "low": c.low, "close": c.close, "volume": c.volume}
                for c in bars[-count:]
            ],
        }

    @app.get("/api/candidate")
    async def candidate() -> dict[str, Any]:
        """What the desk is looking at, and the case for the newest setup."""
        return {
            "scanning": desk.scanning,
            "candidate": desk.candidate,
            "watchlist": [r.symbol for r in desk.screened if r.passed],
            "phase": clock.session_phase(cfg, clock.now(cfg.timezone)),
        }

    # ---------------------------------------------------------------- #
    # The watchlist. This is the ONE thing on the dashboard that changes what
    # the desk does, and the line it does not cross matters: it chooses what
    # to LOOK at. It cannot open, size or close a position — every rule still
    # has to agree before anything is bought, and there is a test that the
    # dashboard has no endpoint which can trade.
    @app.get("/api/watchlist")
    async def get_watchlist() -> dict[str, Any]:
        from panaoptions import watchlist as wl

        saved = wl.load()
        return {
            "symbols": cfg.symbols,
            "source": "custom" if saved else "config",
            "config_symbols": list(
                (cfg.get("universe", {}) or {}).get("symbols", [])),
            "max": wl.MAX_SYMBOLS,
        }

    @app.post("/api/watchlist")
    async def set_watchlist(body: dict[str, Any]) -> dict[str, Any]:
        from panaoptions import watchlist as wl

        try:
            symbols = wl.parse(str(body.get("symbols", "")))
        except wl.WatchlistError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        desk.set_universe(symbols)
        return {"symbols": symbols, "source": "custom",
                "note": "Scanning starts on the next cycle. Open positions "
                        "keep their own exit rules."}

    @app.post("/api/watchlist/reset")
    async def reset_watchlist() -> dict[str, Any]:
        symbols = desk.reset_universe()
        return {"symbols": symbols, "source": "config"}

    @app.get("/api/activity")
    async def activity(limit: int = 60, decisions: bool = False
                       ) -> dict[str, Any]:
        """What the desk just did. A working desk and a hung one look the same
        from an empty position list; this is the difference.

        `decisions=true` drops the scanning chatter and leaves what was taken,
        refused, and why — which on a 1-minute desk is the only way to see a
        trade from four hours ago.
        """
        return {"events": desk.activity.recent(limit, notable_only=decisions),
                "count": len(desk.activity),
                "decisions": desk.activity.notable_count}

    @app.get("/api/daily")
    async def daily_review(day: str | None = None,
                           coach: bool = True) -> dict[str, Any]:
        """The session's review. A diary entry, not evidence about a strategy."""
        from datetime import date

        from panaoptions.journal import weekly

        try:
            when = date.fromisoformat(day) if day else weekly.today(cfg)
        except ValueError as exc:
            raise HTTPException(400, f"'{day}' is not a date") from exc
        review = await weekly.build_daily(cfg, when, with_coach=coach)
        return review.model_dump(mode="json")

    @app.get("/api/daily/download")
    async def download_daily(day: str | None = None,
                             format: str = "md") -> Response:
        from datetime import date

        from panaoptions.journal import weekly

        if format not in {"md", "json"}:
            raise HTTPException(400, "format must be 'md' or 'json'")
        when = date.fromisoformat(day) if day else weekly.today(cfg)
        review = await weekly.build_daily(cfg, when)

        if format == "json":
            body, media = review.model_dump_json(indent=2), "application/json"
        else:
            body, media = weekly.to_markdown(review, cfg), "text/markdown; charset=utf-8"
        name = f"panaoptions-day-{review.label}.{format}"
        return Response(content=body, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/weekly")
    async def weekly_review(week: str | None = None,
                            coach: bool = True) -> dict[str, Any]:
        from datetime import date

        from panaoptions.journal import weekly

        if week:
            try:
                start, end = weekly.week_bounds(date.fromisoformat(week))
            except ValueError as exc:
                raise HTTPException(400, f"'{week}' is not a date") from exc
        else:
            start, end = weekly.current_week(cfg)
        review = await weekly.build(cfg, start, end, with_coach=coach)
        return review.model_dump(mode="json")

    @app.get("/api/weekly/download")
    async def download_weekly(week: str | None = None,
                              format: str = "md") -> Response:
        from datetime import date

        from panaoptions.journal import weekly

        if format not in {"md", "json"}:
            raise HTTPException(400, "format must be 'md' or 'json'")
        if week:
            start, end = weekly.week_bounds(date.fromisoformat(week))
        else:
            start, end = weekly.current_week(cfg)

        review = await weekly.build(cfg, start, end)
        if format == "json":
            body, media = review.model_dump_json(indent=2), "application/json"
        else:
            body, media = weekly.to_markdown(review, cfg), "text/markdown; charset=utf-8"
        name = f"panaoptions-week-{review.label}.{format}"
        return Response(content=body, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/strategies")
    async def strategies() -> dict[str, Any]:
        """Every strategy, its window, and whether it is live now."""
        from panaoptions.engine.strategies import ALL

        moment = clock.now(cfg.timezone)
        now = moment.time()
        minutes = now.hour * 60 + now.minute

        out = []
        for factory in ALL:
            strategy = factory(cfg)
            opens_at = strategy.opens.hour * 60 + strategy.opens.minute
            live = strategy.enabled and strategy.opens <= now < strategy.closes

            # "Outside window" is true at 09:41 and at 16:30 and means
            # something completely different. Four minutes before the open
            # reads as a broken desk unless the panel says so.
            if not strategy.enabled:
                state, opens_in = "off", None
            elif live:
                state, opens_in = "live", None
            elif minutes < opens_at:
                state, opens_in = "waiting", opens_at - minutes
            else:
                state, opens_in = "done", None

            out.append({
                "name": strategy.name.value,
                "enabled": strategy.enabled,
                "from": strategy.opens.strftime("%H:%M"),
                "to": strategy.closes.strftime("%H:%M"),
                "live": live,
                "state": state,
                "opens_in_minutes": opens_in,
            })

        live_count = sum(1 for row in out if row["live"])
        waiting = [row for row in out if row["state"] == "waiting"]
        next_open = min((row["opens_in_minutes"] for row in waiting),
                        default=None)
        return {"strategies": out,
                "live": live_count,
                "next_open_minutes": next_open,
                "market_time": moment.strftime("%H:%M %Z")}

    # ---------------------------------------------------------------- #
    app.mount("/static", _VersionedStatic(directory=str(STATIC)), name="static")

    @app.get("/")
    async def index() -> Response:
        """The page itself is never cached; its assets are stamped instead.

        Without this a `git pull` leaves the browser serving the previous
        app.js from cache — the code is updated, the page is not, and the
        update looks like it never happened.
        """
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        # Every script and stylesheet the page references, not a hand-kept
        # list — a vendored library upgraded in place would otherwise stay
        # cached for a year behind a URL that never changed.
        html = re.sub(
            r'/static/([\w.-]+\.(?:js|css))',
            lambda m: f"/static/{m.group(1)}?v={_asset_version(m.group(1))}",
            html)
        return HTMLResponse(html, headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        })

    return app


class _VersionedStatic(StaticFiles):
    """Cacheable for a year, because a changed file gets a different URL."""

    def file_response(self, *args, **kwargs):          # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _asset_version(name: str) -> str:
    try:
        return str(int((STATIC / name).stat().st_mtime))
    except OSError:
        return "0"


def _config_payload(cfg) -> dict[str, Any]:
    findings = preflight.check(cfg)
    deployed_pct = float(cfg.get("risk.max_capital_deployed_pct", 20))
    stop_pct = float(cfg.get("risk.stop_loss_pct", 20))
    budget = cfg.capital * deployed_pct / 100

    return {
        "capital": cfg.capital,
        "currency": cfg.get("account.currency", "$"),
        "deployed_per_trade": round(budget, 2),
        "deployed_pct": deployed_pct,
        "risk_per_trade": round(budget * stop_pct / 100, 2),
        "risk_per_trade_pct": round(deployed_pct * stop_pct / 100, 2),
        "contract_price_cap": round(
            float(cfg.get("contracts.max_contract_price", 0)) * cfg.multiplier),
        "delta_band": [cfg.get("contracts.min_delta"),
                       cfg.get("contracts.max_delta")],
        "blockers": [_finding(f) for f in findings if f.level == "blocker"],
        "warnings": [_finding(f) for f in findings if f.level == "warning"],
        "market_time": clock.now(cfg.timezone).strftime("%H:%M %Z"),
    }


def _finding(f) -> dict[str, str]:
    return {"setting": f.setting, "problem": f.problem, "fix": f.fix,
            "command": f.command}
