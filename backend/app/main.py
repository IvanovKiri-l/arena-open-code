"""Arena API — точка входа FastAPI.

Поднимает приложение, инициализирует SQLite, отдаёт /health и маршруты
забегов (см. api/routes.py).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .config import settings
from .db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Arena API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
def health() -> dict:
    """Проверка живости: UI показывает по ней статус бэкенда."""
    return {
        "status": "ok",
        "service": "arena-backend",
        "llm_configured": settings.llm_configured,
        "model": settings.llm_model or None,
        "arena_n": settings.arena_n,
        "judges": settings.judges,
        "verify_cmd_set": bool(settings.verify_cmd),
    }
