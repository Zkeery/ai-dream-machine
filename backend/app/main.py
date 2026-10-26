# -*- coding: utf-8 -*-
"""FastAPI 入口：装配路由、异常处理、静态验收界面。"""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth as auth_api
from app.api import files, health, knowledge, models, sessions, settings, tasks, usage
from app.api import trial, trial_materials
from app.core import config
from app.core.errors import register_exception_handlers
from app.core.logging_config import setup_logging
from app.services import db, execution_store, knowledge_retrieval
from app.services import trial_controls, trial_events

setup_logging()
config.settings.ensure_dirs()
db.init_db()
trial_controls.init_trial()
trial_events.init_events()
from app.services import f1_controls
if f1_controls.enabled():
    from app.services import f1_request_audit
    f1_request_audit.install()


async def _clean_originals() -> None:
    while True:
        trial_events.cleanup_originals()
        await asyncio.sleep(3600)

@asynccontextmanager
async def lifespan(application: FastAPI):
    execution_store.recover_unfinished()
    trial_controls.recover_interrupted_reservations()
    cleaner = asyncio.create_task(_clean_originals())
    try:
        yield
    finally:
        cleaner.cancel()
        try:
            await cleaner
        except asyncio.CancelledError:
            pass
        await execution_store.shutdown()
        knowledge_retrieval.close()


app = FastAPI(title="AI造梦机", version="0.1.0", lifespan=lifespan)

# 本地开发允许前端跨域（正式前端阶段 3 起用 3030）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(auth_api.router, prefix="/api", tags=["auth"])
app.include_router(files.router, prefix="/api", tags=["files"])
app.include_router(sessions.router, prefix="/api", tags=["sessions"])
app.include_router(tasks.router, prefix="/api", tags=["tasks"])
app.include_router(settings.router, prefix="/api", tags=["settings"])
app.include_router(knowledge.router, prefix="/api", tags=["knowledge"])
app.include_router(models.router, prefix="/api", tags=["models"])
app.include_router(usage.router, prefix="/api", tags=["usage"])
app.include_router(trial.router, prefix="/api", tags=["trial"])
app.include_router(trial_materials.router, prefix="/api", tags=["trial"])

# 最小验收界面（后端托管，非正式前端）
STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
