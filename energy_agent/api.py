import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent import ask
from .config import Settings
from .data import create_demo, dataset_context
from .models import AskRequest
from .planner import DemoPlanner, PlanningError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None):
    config = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        create_demo(config.db_path)  # Existing imported data is never replaced on startup.
        yield

    app = FastAPI(title="Energy Data Analysis Agent", version="0.2.0", lifespan=lifespan)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        )
        return response

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "planner": config.planner,
            "model": config.model if config.planner == "openai" else None,
        }

    @app.get("/api/dataset")
    def dataset():
        return dataset_context(config.db_path)

    @app.post("/api/ask")
    def query(request: AskRequest):
        try:
            return ask(config, request)
        except PlanningError as exc:
            code = 503 if exc.code in ("provider_error", "data_error") else 400
            return JSONResponse(status_code=code, content={"detail": str(exc), "error_code": exc.code})
        except (sqlite3.Error, OSError):
            log.exception("Dataset access failed")
            return JSONResponse(
                status_code=503, content={"detail": "数据库不可用，请检查数据文件和服务日志。"}
            )

    @app.get("/api/preview")
    def preview(region: str | None = None):
        # A deterministic preview never invokes a paid model, even in OpenAI mode.
        return ask(config, AskRequest(question="负荷趋势", region=region), planner=DemoPlanner())

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
