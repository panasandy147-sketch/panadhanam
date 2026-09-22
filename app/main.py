"""FastAPI application entrypoint.

    python run.py                 # or: uvicorn app.main:app --reload
    → dashboard at http://127.0.0.1:8000
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.journal_routes import router as journal_router
from app.api.live_routes import router as live_router
from app.api.routes import router as api_router
from app.api.ws import router as ws_router
from app.brokers.factory import build_broker
from app.core.config import get_config
from app.core.logging import get_logger, setup_logging
from app.journal import store as journal_store
from app.scheduler import TradingEngine
from app.storage import db

setup_logging()
log = get_logger("main")

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "static"

BANNER = r"""
  ___  ____  _  _  ____  ____  _   _  ____  _  _  ____  __  __
 |  _\|  _ \| \| ||  _ \|  _ \| | | ||  _ \| \| ||  _ \|  \/  |
 | |_ | |_| |  \ || |_| | |_| | |_| || |_| |  \ || |_| | |\/| |
 |  _||  _  | |\  |  _  |  _  |  _  ||  _  | |\  |  _  | |  | |
 |_|  |_| |_|_| \_|_| |_|_| |_|_| |_||_| |_|_| \_|_| |_|_|  |_|

 Multi-Agent Stock & F&O Trading Intelligence
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    print(BANNER)
    db.init_db()
    journal_store.init_journal()

    broker = await build_broker(cfg)
    engine = TradingEngine(broker, cfg)
    app.state.engine = engine
    app.state.broker = broker

    log.info("mode=%s | broker=%s | live_orders=%s | capital=%s%s | reasoning=%s",
             cfg.trading_mode, broker.name, cfg.live_orders_enabled,
             cfg.market.currency_symbol,
             f"{float(cfg.get('risk.total_capital', 0)):,.0f}",
             cfg.llm_label)
    if not cfg.llm_enabled:
        log.info("No LLM configured — agents run on their deterministic rule "
                 "engines. For a FREE local model: set LLM_PROVIDER=ollama and "
                 "OLLAMA_MODEL in .env (see docs/OLLAMA.md). For Claude: set "
                 "ANTHROPIC_API_KEY.")
    if cfg.live_orders_enabled and cfg.get("execution.auto_place_orders"):
        log.warning("!!! LIVE ORDER PLACEMENT IS ENABLED — real money is at risk !!!")

    if os.getenv("AUTOSTART_ENGINE", "true").lower() in {"1", "true", "yes"}:
        await engine.start()

    host = os.getenv("HOST", "127.0.0.1")
    port = os.getenv("PORT", "8000")
    log.info("dashboard → http://%s:%s", host, port)

    try:
        yield
    finally:
        await engine.stop()
        await broker.disconnect()


app = FastAPI(
    title="panadhanam — Multi-Agent Trading Intelligence",
    description="Hedge-fund style multi-agent pipeline for Indian equity and F&O "
                "with strict, deterministic risk controls.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # local-only tool; tighten if you expose it
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(journal_router)
app.include_router(live_router)
app.include_router(ws_router)

if DASHBOARD_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(str(DASHBOARD_DIR / "index.html"))
