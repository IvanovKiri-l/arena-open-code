"""Конфигурация приложения.

Единственный источник настроек — переменные окружения. В Docker они приходят
через env_file (.env), при локальном запуске файл .env подхватывается сам.
Никаких захардкоженных ключей, путей и портов — только безопасные значения
по умолчанию, которые переопределяются через .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Подхватить корневой .env при локальном запуске без Docker."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:  # dotenv не установлен — живём на переменных окружения
        return
    path = find_dotenv(filename=".env", raise_error_if_not_found=False)
    if path:
        load_dotenv(path, override=False)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout_s: float
    llm_max_retries: int
    arena_n: int
    max_concurrency: int
    judges: int
    judge_criteria: tuple[str, ...]
    verify_cmd: str
    verify_timeout_s: float
    stages: int
    screening: bool
    screen_min_score: float
    cors_origins: tuple[str, ...]
    port: int
    db_path: Path

    @property
    def llm_configured(self) -> bool:
        # Ключ может быть пустым: локальные эндпоинты (Ollama и т.п.) его не требуют.
        return bool(self.llm_base_url and self.llm_model)


def load_settings() -> Settings:
    criteria = _env("JUDGE_CRITERIA", "correctness,completeness,readability")
    return Settings(
        llm_api_key=_env("LLM_API_KEY"),
        llm_base_url=_env("LLM_BASE_URL").rstrip("/"),
        llm_model=_env("LLM_MODEL"),
        llm_timeout_s=_env_float("LLM_TIMEOUT_S", 120.0),
        llm_max_retries=_env_int("LLM_MAX_RETRIES", 3),
        arena_n=_env_int("ARENA_N", 10),
        max_concurrency=_env_int("MAX_CONCURRENCY", 5),
        judges=_env_int("JUDGES", 1),
        judge_criteria=tuple(c.strip() for c in criteria.split(",") if c.strip()),
        verify_cmd=_env("VERIFY_CMD"),
        verify_timeout_s=_env_float("VERIFY_TIMEOUT_S", 60.0),
        stages=_env_int("STAGES", 2),
        screening=_env_bool("SCREENING", True),
        screen_min_score=_env_float("SCREEN_MIN_SCORE", 3.0),
        cors_origins=tuple(o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip()),
        port=_env_int("PORT", 3000),
        db_path=Path(_env("DB_PATH", "./data/arena.db")),
    )


settings = load_settings()
