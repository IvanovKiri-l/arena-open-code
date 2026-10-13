"""Ядро арены: параллельный забег N инстансов модели на один промпт."""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..config import settings as default_settings
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..prompts.roles import InstanceProfile, build_profiles
from . import store

#: Колбэк стрима: (slot инстанса, чанк текста).
ChunkCallback = Callable[[int, str], None]

#: Колбэк готового ответа инстанса: (запись ответа).
ResponseCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class ArenaOptions:
    """Параметры забега. None означает «взять из .env» (Settings)."""

    n: int | None = None
    timeout_s: float | None = None
    max_concurrency: int | None = None
    jitter_ms: tuple[int, int] = (50, 100)
    stream: bool = False


@dataclass
class ArenaRun:
    """Итог забега: метаданные + по одному ответу на каждый инстанс."""

    id: str
    prompt: str
    created_at: str
    options: dict[str, Any]
    responses: list[dict[str, Any]] = field(default_factory=list)
    status: str = "done"

    @property
    def ok_responses(self) -> list[dict[str, Any]]:
        return [r for r in self.responses if r.get("status") == "ok"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "created_at": self.created_at,
            "options": self.options,
            "responses": self.responses,
            "status": self.status,
        }


async def run_arena(
    prompt: str,
    options: ArenaOptions | None = None,
    *,
    client: LLMClient | None = None,
    cfg: Settings = default_settings,
    on_chunk: ChunkCallback | None = None,
    on_response: ResponseCallback | None = None,
    run_id: str | None = None,
) -> ArenaRun:
    """Запустить забег N инстансов и вернуть собранные результаты.

    Ошибка любого отдельного вызова (таймаут, сбой провайдера) не роняет
    забег: она попадает в поле error соответствующего ответа.
    Забег и все ответы пишутся в SQLite (см. core/store).

    client можно не передавать — тогда соберётся OpenAI-совместимый клиент
    из настроек (.env); такой «свой» клиент арена закроет сама.

    on_chunk и on_response зовутся из event loop по мере готовности данных
    (для SSE); их сбои забег не роняют. run_id можно задать заранее —
    так API отдаёт идентификатор до старта фонового прогона.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Промпт пустой — арене нечего запускать")

    opts = options or ArenaOptions()
    n = opts.n or cfg.arena_n
    if n <= 0:
        raise ValueError("Количество инстансов n должно быть >= 1")
    owns_client = client is None
    if client is None:
        client = build_client(cfg)
    timeout_s = float(opts.timeout_s or cfg.llm_timeout_s)
    max_concurrency = max(1, opts.max_concurrency or cfg.max_concurrency)
    jitter = (min(opts.jitter_ms), max(opts.jitter_ms))
    profiles = build_profiles(n)

    run_id = run_id or str(uuid.uuid4())
    created_at = store.utcnow_iso()
    options_snapshot: dict[str, Any] = {
        "n": n,
        "timeout_s": timeout_s,
        "max_concurrency": max_concurrency,
        "jitter_ms": jitter,
        "stream": opts.stream,
        "model": client.model,
    }
    store.create_run(run_id, prompt, options_snapshot)

    semaphore = asyncio.Semaphore(max_concurrency)

    try:
        settled = await asyncio.gather(
            *(
                _run_slot(
                    run_id=run_id,
                    slot=slot,
                    profile=profile,
                    prompt=prompt,
                    client=client,
                    timeout_s=timeout_s,
                    semaphore=semaphore,
                    jitter=jitter,
                    stream=opts.stream,
                    on_chunk=on_chunk,
                    on_response=on_response,
                )
                for slot, profile in enumerate(profiles, start=1)
            ),
            return_exceptions=True,
        )
    finally:
        # Закрываем HTTP-соединения, если клиент собирали мы сами.
        if owns_client:
            await client.aclose()

    responses: list[dict[str, Any]] = []
    for slot, outcome in enumerate(settled, start=1):
        if isinstance(outcome, BaseException):
            # Страховка: слот сам ловит свои ошибки, но забег не должен
            # падать даже при внутреннем сбое обработчика.
            fallback = _empty_record(run_id, slot, profiles[slot - 1], prompt)
            fallback["error"] = f"internal error: {outcome!r}"
            store.save_response(fallback)
            _notify(on_response, fallback)
            responses.append(fallback)
        else:
            responses.append(outcome)

    store.finish_run(run_id, status="done")

    return ArenaRun(
        id=run_id,
        prompt=prompt,
        created_at=created_at,
        options=options_snapshot,
        responses=responses,
        status="done",
    )


async def _run_slot(
    *,
    run_id: str,
    slot: int,
    profile: InstanceProfile,
    prompt: str,
    client: LLMClient,
    timeout_s: float,
    semaphore: asyncio.Semaphore,
    jitter: tuple[int, int],
    stream: bool,
    on_chunk: ChunkCallback | None,
    on_response: ResponseCallback | None,
) -> dict[str, Any]:
    """Один инстанс: джиттер → семафор → вызов с таймаутом → запись в БД."""
    response_id = str(uuid.uuid4())

    # Джиттер между запусками: разводим одновременные запросы во времени.
    await asyncio.sleep(random.uniform(*jitter) / 1000.0)

    partial: list[str] = []

    def handle_chunk(chunk: str) -> None:
        partial.append(chunk)
        if on_chunk is not None:
            try:
                on_chunk(slot, chunk)
            except Exception:
                pass  # сбой UI-колбэка не должен ронять забег

    output: str | None = None
    tokens: int | None = None
    error: str | None = None
    status = "ok"

    async with semaphore:
        started = time.perf_counter()
        try:
            completion = await asyncio.wait_for(
                client.complete(
                    prompt,
                    system=profile.system_prompt,
                    temperature=profile.temperature,
                    stream=stream or on_chunk is not None,
                    on_chunk=handle_chunk,
                ),
                timeout=timeout_s,
            )
            output = completion.output
            tokens = completion.tokens
        except asyncio.TimeoutError:
            status = "error"
            error = f"timeout: нет ответа за {timeout_s:g} с"
            output = "".join(partial) or None
        except Exception as exc:  # noqa: BLE001 — сохраняем любой сбой провайдера
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            output = "".join(partial) or None

    latency_ms = int((time.perf_counter() - started) * 1000)

    record: dict[str, Any] = {
        "id": response_id,
        "run_id": run_id,
        "slot": slot,
        "profile": profile.key,
        "profile_label": profile.label,
        "temperature": profile.temperature,
        "prompt": prompt,
        "output": output,
        "latency_ms": latency_ms,
        "tokens": tokens,
        "error": error,
        "status": status,
    }
    store.save_response(record)
    _notify(on_response, record)
    return record


def _notify(callback: ResponseCallback | None, record: dict[str, Any]) -> None:
    """Позвать подписчика, не роняя забег из-за его ошибок."""
    if callback is None:
        return
    try:
        callback(record)
    except Exception:
        pass


def _empty_record(
    run_id: str, slot: int, profile: InstanceProfile, prompt: str
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "run_id": run_id,
        "slot": slot,
        "profile": profile.key,
        "profile_label": profile.label,
        "temperature": profile.temperature,
        "prompt": prompt,
        "output": None,
        "latency_ms": None,
        "tokens": None,
        "error": None,
        "status": "error",
    }
