"""Абстракция LLM-клиента.

Любой провайдер подключается наследованием от LLMClient и реализацией
complete(). Из коробки: FakeLLMClient (тесты и разработка без сети) и,
начиная со следующего шага, OpenAICompatClient (любой OpenAI-совместимый API).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Union

# Колбэк стрима: может быть синхронным или асинхронным.
ChunkHandler = Callable[[str], Union[Awaitable[None], None]]


@dataclass
class Completion:
    """Результат одного вызова модели."""

    output: str
    tokens: int | None = None
    model: str | None = None


class LLMClient(ABC):
    """Интерфейс провайдера модели."""

    #: Идентификатор модели (наследники переопределяют).
    model: str = "unknown"

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        stream: bool = False,
        on_chunk: ChunkHandler | None = None,
    ) -> Completion:
        """Выполнить один вызов модели.

        :param prompt: пользовательский промпт
        :param system: системный промпт (роль инстанса)
        :param temperature: температура генерации
        :param stream: запросить у провайдера стриминг
        :param on_chunk: колбэк, вызываемый на каждый чанк текста
        :raises Exception: любые ошибки транспорта/провайдера — арена ловит
            их по каждому инстансу отдельно и не роняет весь забег.
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        """Освободить ресурсы клиента (HTTP-соединения). По умолчанию — нечего."""
        return None
