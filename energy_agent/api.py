import hashlib
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent import ask
from .config import ROOT, Settings
from .data import create_demo, dataset_context
from .models import AskRequest
from .planner import DemoPlanner, PlanningError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def training_evidence():
    try:
        train_bytes = (ROOT / "reports/sft-training.json").read_bytes()
        evaluation_bytes = (ROOT / "reports/sft-evaluation.json").read_bytes()
        chart = json.loads((ROOT / "docs/figures/sft-chart-values.json").read_text())
        if (
            hashlib.sha256(train_bytes).hexdigest() != chart["training_report_sha256"]
            or hashlib.sha256(evaluation_bytes).hexdigest() != chart["evaluation_report_sha256"]
        ):
            return {"available": False}
        trained, evaluated = json.loads(train_bytes), json.loads(evaluation_bytes)
        return {
            "available": True,
            "base": evaluated["summary"]["base"],
            "qlora": evaluated["summary"]["qlora"],
            "paired": evaluated["paired"],
            "splits": evaluated["dataset"]["splits"],
            "steps": trained["config"]["iters"],
            "rank": trained["config"]["lora_parameters"]["rank"],
            "best_step": trained["best_checkpoint"]["iteration"],
            "parameters": trained["trainable_parameters"],
            "peak_gb": trained["peak_memory_bytes"] / 1e9,
            "fixed_experiment": True,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return {"available": False}


def create_app(settings: Settings | None = None):
    config = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        create_demo(config.db_path)  # Existing imported data is never replaced on startup.
        yield

    app = FastAPI(title="Energy Data Analysis Agent", version="0.3.0", lifespan=lifespan)

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
            "model": config.model if config.planner != "demo" else None,
            "model_configured": config.planner in ("ollama-tools", "mlx-sql")
            or bool(os.getenv("OPENAI_API_KEY")),
            "provider": "mlx"
            if config.planner == "mlx-sql"
            else None
            if config.planner == "demo"
            else "ollama"
            if config.planner == "ollama-tools"
            else "openai",
            "max_model_calls": config.max_model_calls,
        }

    @app.get("/api/dataset")
    def dataset():
        return dataset_context(config.db_path)

    @app.get("/api/training-evidence")
    def training_report():
        return training_evidence()

    @app.get("/api/training-figure")
    def training_figure():
        if not training_evidence()["available"]:
            return JSONResponse(status_code=404, content={"detail": "Training evidence is unavailable"})
        return FileResponse(ROOT / "docs/figures/sft-evaluation.png", headers={"Cache-Control": "no-store"})

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
