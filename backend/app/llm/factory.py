"""Сборка LLM-клиента из настроек окружения (.env)."""
from __future__ import annotations

from ..config import Settings
from .base import LLMClient
from .openai_compat import OpenAICompatClient


def build_client(cfg: Settings) -> LLMClient:
    """Клиент по умолчанию: OpenAI-совместимый провайдер из .env.

    Друг может заменить это на свою реализацию LLMClient и передать её
    в run_arena(client=...) — ничего больше менять не придётся.
    """
    if not cfg.llm_configured:
        raise RuntimeError(
            "LLM не настроен: заполните LLM_BASE_URL и LLM_MODEL "
            "(и LLM_API_KEY для облачных провайдеров) в .env"
        )
    return OpenAICompatClient(
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        model=cfg.llm_model,
        timeout_s=cfg.llm_timeout_s,
        max_retries=cfg.llm_max_retries,
    )
