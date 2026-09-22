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

from fastapi import FastAPI
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
