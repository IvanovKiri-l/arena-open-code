"""OpenAI-совместимый LLM-клиент.

Работает с любым провайдером, у которого есть POST /chat/completions:
OpenAI, DeepSeek, OpenRouter, Groq, Together, vLLM, локальная Ollama и т.д.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import random
from typing import Any

import httpx

from .base import ChunkHandler, Completion, LLMClient


class ProviderError(RuntimeError):
    """Ошибка провайдера: HTTP-статус, признак повторяемости."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class OpenAICompatClient(LLMClient):
    """Клиент /chat/completions с повторами и стримингом.

    :param base_url: базовый URL API, например https://api.openai.com/v1
    :param api_key: ключ провайдера; можно пустым для keyless-эндпоинтов (Ollama)
    :param model: id модели
    :param timeout_s: таймаут одного HTTP-запроса
    :param max_retries: повторы при 429/5xx/сетевых сбоях (экспоненциальный backoff)
    :param backoff_base_s: база backoff (в тестах задаётся меньше единицы)
    :param transport: альтернативный httpx-транспорт (используется в тестах)
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout_s: float = 120.0,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_retries = max(0, int(max_retries))
        self._backoff_base_s = max(0.0, backoff_base_s)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # ── HTTP-соединение ──────────────────────────────────────────────
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=httpx.Timeout(self._timeout_s),
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Публичный интерфейс ──────────────────────────────────────────
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        stream: bool = False,
        on_chunk: ChunkHandler | None = None,
    ) -> Completion:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": bool(stream and on_chunk is not None),
        }

        if payload["stream"] and on_chunk is not None:
            return await self._stream(payload, on_chunk)

        data = await self._post_json(payload)
        return self._parse_json_completion(data)

    # ── Непотоковый путь ─────────────────────────────────────────────
    async def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._request(payload, stream=False)
        try:
            return json.loads(await response.aread())
        finally:
            await response.aclose()

    def _parse_json_completion(self, data: dict[str, Any]) -> Completion:
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        usage = data.get("usage") or {}
        return Completion(
            output=message.get("content") or "",
            tokens=usage.get("total_tokens"),
            model=data.get("model") or self.model,
        )

    # ── Потоковый путь (SSE) ─────────────────────────────────────────
    async def _stream(self, payload: dict[str, Any], on_chunk: ChunkHandler) -> Completion:
        response = await self._request(payload, stream=True)
        parts: list[str] = []
        tokens: int | None = None
        model = self.model
        try:
            async for line in response.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue  # нештатную строку просто пропускаем
                model = event.get("model") or model
                chunk = _delta_text(event)
                if chunk:
                    parts.append(chunk)
                    result = on_chunk(chunk)
                    if inspect.isawaitable(result):
                        await result
                usage = event.get("usage")
                if isinstance(usage, dict) and usage.get("total_tokens") is not None:
                    tokens = usage["total_tokens"]
        finally:
            await response.aclose()
        return Completion(output="".join(parts), tokens=tokens, model=model)

    # ── Транспорт с повторами ────────────────────────────────────────
    async def _request(self, payload: dict[str, Any], *, stream: bool) -> httpx.Response:
        last: ProviderError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                client = await self._http()
                request = client.build_request("POST", "/chat/completions", json=payload)
                response = await client.send(request, stream=stream)
            except httpx.HTTPError as exc:
                last = ProviderError(f"сетевая ошибка: {exc}", retryable=True)
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_delay(attempt))
                    continue
                raise last from exc

            if response.status_code == 429 or response.status_code >= 500:
                body = (await response.aread())[:300].decode("utf-8", "replace")
                retry_after = _retry_after_seconds(response)
                await response.aclose()
                last = ProviderError(
                    f"HTTP {response.status_code}: {body!r}",
                    status_code=response.status_code,
                    retryable=True,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff_delay(attempt, retry_after))
                    continue
                raise last

            if response.status_code >= 400:
                body = (await response.aread())[:300].decode("utf-8", "replace")
                await response.aclose()
                raise ProviderError(
                    f"HTTP {response.status_code}: {body!r}",
                    status_code=response.status_code,
                )

            return response

        raise last or ProviderError("запрос не выполнен")  # pragma: no cover

    def _backoff_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Экспоненциальный backoff: 1s, 2s, 4s… (+ джиттер); Retry-After уважаем."""
        if retry_after is not None:
            return max(0.0, min(retry_after, 60.0))
        base = self._backoff_base_s * (2 ** attempt)
        if base <= 0:
            return 0.0
        return base + random.uniform(0, base * 0.25)


def _delta_text(event: dict[str, Any]) -> str:
    choices = event.get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if content is None:
        # некоторые провайдеры в стриме шлют message.content вместо delta
        content = (choice.get("message") or {}).get("content")
    return content or ""


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date — редкий формат, игнорируем
