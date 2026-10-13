"""HTTP-API арены.

Маршруты (в Docker их вызывает nginx, срезая префикс /api):
    POST /arena                 — старт забега, отвечает мгновенно (202)
    GET  /arena/{id}/stream     — SSE: чанки, готовые ответы, судейство
    GET  /arena/{id}            — финальный результат
    GET  /arena/{id}/export     — экспорт забега в JSON
    POST /arena/{id}/judge      — переголосовать (перезапускает только судейство)
    GET  /runs                  — история забегов
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..core import store
from ..core.arena import ArenaOptions, run_arena
from ..core.hub import RunChannel, hub
from ..core.judge import judge_run
from ..core.verify import verify_responses

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# Фоновые задачи держим на ссылках, чтобы их не собрал сборщик мусора.
_tasks: set[asyncio.Task[None]] = set()


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


class ArenaRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    n: int | None = Field(default=None, ge=1, le=100)
    judges: int | None = Field(default=None, ge=1, le=9)
    criteria: list[str] | None = None
    timeout_s: float | None = Field(default=None, gt=0)
    max_concurrency: int | None = Field(default=None, ge=1, le=100)
    verify: bool | None = None


class JudgeRequest(BaseModel):
    judges: int | None = Field(default=None, ge=1, le=9)
    criteria: list[str] | None = None


# ── Публичные представления записей ─────────────────────────────────


def _public_response(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in ("run_id", "prompt")}


def _public_verdict(verdict: dict[str, Any]) -> dict[str, Any]:
    return {
        "judge_index": verdict.get("judge_index"),
        "winner_id": verdict.get("winner_id"),
        "scores": verdict.get("scores") or [],
        "rationale": verdict.get("rationale"),
        "created_at": verdict.get("created_at"),
    }


def _public_run(stored: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": stored["id"],
        "created_at": stored["created_at"],
        "prompt": stored["prompt"],
        "options": stored["options"],
        "status": stored["status"],
        "winner_id": stored["winner_id"],
        "responses": [
            _public_response(r) for r in sorted(stored["responses"], key=lambda r: r["slot"])
        ],
        "verdicts": [_public_verdict(v) for v in stored["verdicts"]],
    }


# ── Запуск забега ───────────────────────────────────────────────────


@router.post("/arena", status_code=202)
async def start_arena(request: ArenaRequest) -> dict[str, Any]:
    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Пустой промпт")
    if request.verify and not settings.verify_cmd:
        raise HTTPException(
            status_code=400,
            detail="верификация запрошена, но VERIFY_CMD не задан в .env",
        )

    run_id = str(uuid4())
    options = ArenaOptions(
        n=request.n,
        timeout_s=request.timeout_s,
        max_concurrency=request.max_concurrency,
    )
    # Канал открываем синхронно, чтобы стрим можно было слушать сразу.
    hub.open(run_id)
    _spawn(
        _execute_run(
            run_id,
            prompt,
            options,
            judges=request.judges,
            criteria=request.criteria,
            verify=request.verify,
        )
    )
    return {"run_id": run_id, "status": "running"}


async def _execute_run(
    run_id: str,
    prompt: str,
    options: ArenaOptions,
    *,
    judges: int | None,
    criteria: list[str] | None,
    verify: bool | None,
) -> None:
    """Фоновый конвейер: арена → верификация → судейство → вердикт."""
    try:
        def on_chunk(slot: int, text: str) -> None:
            hub.publish(run_id, {"type": "chunk", "slot": slot, "text": text})

        def on_response(record: dict[str, Any]) -> None:
            hub.publish(run_id, {"type": "response", "data": _public_response(record)})

        try:
            run = await run_arena(
                prompt,
                options,
                run_id=run_id,
                on_chunk=on_chunk,
                on_response=on_response,
            )
        except Exception as exc:  # сбой конфигурации и т.п.
            hub.publish(run_id, {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return

        # Верификация кода (если задан VERIFY_CMD и не отключена явно).
        if verify is not False and settings.verify_cmd:
            hub.publish(run_id, {"type": "verifying"})
            try:
                outcome = await verify_responses(run_id, run.responses, cfg=settings)
                for record in outcome.updated:
                    hub.publish(
                        run_id, {"type": "response", "data": _public_response(record)}
                    )
            except Exception as exc:
                hub.publish(
                    run_id,
                    {"type": "error", "message": f"верификация: {type(exc).__name__}: {exc}"},
                )
                return

        hub.publish(run_id, {"type": "judging"})
        try:
            result = await judge_run(
                run_id, run.prompt, run.responses, judges=judges, criteria=criteria
            )
            hub.publish(
                run_id,
                {
                    "type": "verdict",
                    "data": {
                        "winner_id": result.winner_id,
                        "verdicts": result.verdicts,
                        "errors": result.errors,
                    },
                },
            )
        except Exception as exc:
            hub.publish(
                run_id,
                {"type": "error", "message": f"судейство: {type(exc).__name__}: {exc}"},
            )
    finally:
        hub.publish(run_id, {"type": "end"})
        hub.close(run_id)


# ── SSE-стрим ───────────────────────────────────────────────────────


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/arena/{run_id}/stream")
async def stream_arena(run_id: str) -> StreamingResponse:
    return StreamingResponse(
        _event_source(run_id, hub.get(run_id)),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def _event_source(run_id: str, channel: RunChannel | None) -> AsyncIterator[str]:
    # 1) Снапшот из БД: то, что уже успело закончиться до подключения.
    stored = store.fetch_run(run_id)
    if stored is not None:
        for response in stored["responses"]:
            yield _sse({"type": "response", "data": _public_response(response)})
        if stored["winner_id"]:
            yield _sse(
                {
                    "type": "verdict",
                    "data": {
                        "winner_id": stored["winner_id"],
                        "verdicts": [_public_verdict(v) for v in stored["verdicts"]],
                        "errors": [],
                    },
                }
            )
            yield _sse({"type": "end"})
            return
        if not hub.is_active(run_id):
            if stored["status"] == "running":
                yield _sse(
                    {"type": "error", "message": "забег прерван (сервер был перезапущен)"}
                )
            yield _sse({"type": "end"})
            return

    if channel is None or not hub.is_active(run_id):
        yield _sse({"type": "error", "message": "забег не найден"})
        yield _sse({"type": "end"})
        return

    # 2) Живые события.
    queue = channel.subscribe()
    try:
        if hub.closed(run_id):
            yield _sse({"type": "end"})
            return
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                return
            yield _sse(event)
    finally:
        channel.unsubscribe(queue)


# ── Результат, история, экспорт, переголосование ────────────────────


@router.get("/arena/{run_id}")
async def get_arena(run_id: str) -> dict[str, Any]:
    stored = store.fetch_run(run_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="забег не найден")
    return _public_run(stored)


@router.get("/arena/{run_id}/export")
async def export_arena(run_id: str) -> Response:
    stored = store.fetch_run(run_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="забег не найден")
    payload = {"version": 1, "exported_at": store.utcnow_iso(), **_public_run(stored)}
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="arena-{run_id[:8]}.json"'},
    )


@router.post("/arena/{run_id}/judge")
async def rejudge_arena(run_id: str, request: JudgeRequest | None = None) -> dict[str, Any]:
    stored = store.fetch_run(run_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="забег не найден")
    if stored["status"] != "done":
        raise HTTPException(status_code=409, detail="забег ещё выполняется")

    result = await judge_run(
        run_id,
        stored["prompt"],
        stored["responses"],
        judges=request.judges if request else None,
        criteria=request.criteria if request else None,
    )
    return {
        "winner_id": result.winner_id,
        "verdicts": [_public_verdict(v) for v in result.verdicts],
        "errors": result.errors,
    }


@router.get("/runs")
async def list_runs(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    return {"runs": store.fetch_runs(limit=limit)}
