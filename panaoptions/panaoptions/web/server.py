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
        out["session"] = {
            "entry_open": cfg.get("session.entry_open"),
            "entry_close": cfg.get("session.entry_close"),
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

    @app.get("/api/activity")
    async def activity(limit: int = 60) -> dict[str, Any]:
        """What the desk just did. A working desk and a hung one look the same
        from an empty position list; this is the difference."""
        return {"events": desk.activity.recent(limit),
                "count": len(desk.activity)}

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
        """The three strategies, their windows, and whether each is live now."""
        from panaoptions.engine.strategies import ALL

        now = clock.now(cfg.timezone).time()
        out = []
        for factory in ALL:
            strategy = factory(cfg)
            out.append({
                "name": strategy.name.value,
                "enabled": strategy.enabled,
                "from": strategy.opens.strftime("%H:%M"),
                "to": strategy.closes.strftime("%H:%M"),
                "live": strategy.enabled and strategy.opens <= now < strategy.closes,
            })
        return {"strategies": out,
                "market_time": clock.now(cfg.timezone).strftime("%H:%M %Z")}

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
        for asset in ("app.js", "styles.css"):
            html = html.replace(f"/static/{asset}",
                                f"/static/{asset}?v={_asset_version(asset)}")
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
