"""Ядро арены: параллельный забег N инстансов модели на один промпт.

Работа идёт этапами (по умолчанию черновик → финал, см. STAGES).
После первого этапа судья-фильтр делает ранний отбор: откровенно слабые
черновики вылетают и не тратят токены на оставшиеся этапы.
"""
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
from ..prompts.stages import STAGES, Stage, build_stage_prompt
from . import store
from .judge import screen_drafts

#: Колбэк стрима: (slot инстанса, чанк текста).
ChunkCallback = Callable[[int, str], None]

#: Колбэк готового ответа инстанса: (запись ответа).
ResponseCallback = Callable[[dict[str, Any]], None]

#: Колбэк начала этапа: {"index": 1, "total": 2, "key": ..., "title": ...}.
StageCallback = Callable[[dict[str, Any]], None]

#: Колбэк начала раннего отбора (без аргументов).
PlainCallback = Callable[[], None]


@dataclass(frozen=True)
class ArenaOptions:
    """Параметры забега. None означает «взять из .env» (Settings)."""

    n: int | None = None
    timeout_s: float | None = None
    max_concurrency: int | None = None
    jitter_ms: tuple[int, int] = (50, 100)
    stream: bool = False
    stages: int | None = None
    screening: bool | None = None


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
    on_stage: StageCallback | None = None,
    on_screening: PlainCallback | None = None,
    run_id: str | None = None,
) -> ArenaRun:
    """Запустить забег N инстансов и вернуть собранные результаты.

    Каждый инстанс проходит этапы последовательно, все инстансы —
    параллельно (семафор MAX_CONCURRENCY, джиттер между запусками).
    Ошибка отдельного вызова не роняет забег: она попадает в поле error.

    client можно не передавать — тогда соберётся OpenAI-совместимый клиент
    из настроек (.env); такой «свой» клиент арена закроет сама.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Промпт пустой — арене нечего запускать")

    opts = options or ArenaOptions()
    n = opts.n or cfg.arena_n
    if n <= 0:
        raise ValueError("Количество инстансов n должно быть >= 1")
    timeout_s = float(opts.timeout_s or cfg.llm_timeout_s)
    max_concurrency = max(1, opts.max_concurrency or cfg.max_concurrency)
    jitter = (min(opts.jitter_ms), max(opts.jitter_ms))
    stages_total = max(1, opts.stages or cfg.stages)
    stage_list = [STAGES[index % len(STAGES)] for index in range(stages_total)]
    screening_enabled = cfg.screening if opts.screening is None else opts.screening
    profiles = build_profiles(n)

    owns_client = client is None
    if client is None:
        client = build_client(cfg)

    run_id = run_id or str(uuid.uuid4())
    created_at = store.utcnow_iso()
    options_snapshot: dict[str, Any] = {
        "n": n,
        "timeout_s": timeout_s,
        "max_concurrency": max_concurrency,
        "jitter_ms": jitter,
        "stream": opts.stream,
        "model": client.model,
        "stages": stages_total,
        "screening": screening_enabled,
    }
    store.create_run(run_id, prompt, options_snapshot)

    semaphore = asyncio.Semaphore(max_concurrency)
    rnd = random.Random()

    states: dict[int, dict[str, Any]] = {}
    for slot, profile in enumerate(profiles, start=1):
        states[slot] = {
            "id": str(uuid.uuid4()),
            "slot": slot,
            "profile": profile,
            "temperature": profile.temperature,
            "stages": [],
            "latency_ms": 0,
            "tokens": 0,
            "error": None,
            "record": None,
        }
    remaining: list[int] = sorted(states)

    try:
        for stage_index, stage in enumerate(stage_list):
            _notify_stage(
                on_stage,
                {
                    "index": stage_index + 1,
                    "total": len(stage_list),
                    "key": stage.key,
                    "title": stage.title,
                },
            )

            outcomes = await asyncio.gather(
                *(
                    _run_stage(
                        slot=slot,
                        stage=stage,
                        state=states[slot],
                        prompt=prompt,
                        wrap_stage=len(stage_list) > 1,
                        client=client,
                        timeout_s=timeout_s,
                        semaphore=semaphore,
                        jitter=jitter,
                        stream=opts.stream,
                        on_chunk=on_chunk,
                    )
                    for slot in remaining
                ),
                return_exceptions=True,
            )

            failed: list[int] = []
            for slot, outcome in zip(list(remaining), outcomes):
                state = states[slot]
                if isinstance(outcome, BaseException):
                    state["error"] = f"internal error: {outcome!r}"
                    failed.append(slot)
                    continue
                state["latency_ms"] += int(outcome["latency_ms"])
                state["tokens"] += int(outcome["tokens"] or 0)
                if outcome["error"] is not None:
                    state["error"] = outcome["error"]
                    failed.append(slot)
                    continue
                state["stages"].append(
                    {
                        "key": stage.key,
                        "title": stage.title,
                        "output": outcome["output"] or "",
                    }
                )

            for slot in failed:
                record = _final_record(run_id, prompt, states[slot], status="error")
                store.save_response(record)
                _notify(on_response, record)
                states[slot]["record"] = record
            remaining = [slot for slot in remaining if slot not in failed]

            # Ранний отбор после первого этапа: слабые не идут дальше.
            if (
                stage_index == 0
                and len(stage_list) > 1
                and screening_enabled
                and len(remaining) > 2
            ):
                _notify_plain(on_screening)
                drafts = [
                    {"slot": slot, "output": states[slot]["stages"][0]["output"]}
                    for slot in remaining
                ]
                screening = await screen_drafts(
                    prompt,
                    drafts,
                    client=client,
                    min_score=cfg.screen_min_score,
                    rng=rnd,
                    timeout_s=timeout_s,
                )
                if screening.ran and screening.eliminated_slots:
                    eliminated = set(screening.eliminated_slots)
                    for slot in sorted(eliminated):
                        state = states[slot]
                        score = screening.scores.get(slot, 0.0)
                        reason = screening.reasons.get(slot) or "слабый черновик"
                        state["error"] = f"отбор: {score:.1f}/10 — {reason}"
                        record = _final_record(run_id, prompt, state, status="disqualified")
                        store.save_response(record)
                        _notify(on_response, record)
                        states[slot]["record"] = record
                    remaining = [slot for slot in remaining if slot not in eliminated]

        for slot in remaining:
            record = _final_record(run_id, prompt, states[slot], status="ok")
            store.save_response(record)
            _notify(on_response, record)
            states[slot]["record"] = record
    finally:
        # Закрываем HTTP-соединения, если клиент собирали мы сами.
        if owns_client:
            await client.aclose()

    responses = [
        states[slot]["record"] or _final_record(run_id, prompt, states[slot], status="error")
        for slot in sorted(states)
    ]
    store.finish_run(run_id, status="done")

    return ArenaRun(
        id=run_id,
        prompt=prompt,
        created_at=created_at,
        options=options_snapshot,
        responses=responses,
        status="done",
    )


async def _run_stage(
    *,
    slot: int,
    stage: Stage,
    state: dict[str, Any],
    prompt: str,
    wrap_stage: bool,
    client: LLMClient,
    timeout_s: float,
    semaphore: asyncio.Semaphore,
    jitter: tuple[int, int],
    stream: bool,
    on_chunk: ChunkCallback | None,
) -> dict[str, Any]:
    """Один этап одного инстанса: джиттер → семафор → вызов с таймаутом."""
    await asyncio.sleep(random.uniform(*jitter) / 1000.0)

    partial: list[str] = []

    def handle_chunk(chunk: str) -> None:
        partial.append(chunk)
        if on_chunk is not None:
            try:
                on_chunk(slot, chunk)
            except Exception:
                pass  # сбой UI-колбэка не должен ронять забег

    if wrap_stage:
        previous = state["stages"][-1]["output"] if state["stages"] else None
        prompt_text = build_stage_prompt(prompt, stage, previous)
    else:
        # одноэтапный режим: шлём промпт как есть (совместимость)
        prompt_text = prompt

    async with semaphore:
        started = time.perf_counter()
        try:
            completion = await asyncio.wait_for(
                client.complete(
                    prompt_text,
                    system=state["profile"].system_prompt,
                    temperature=state["temperature"],
                    stream=stream or on_chunk is not None,
                    on_chunk=handle_chunk,
                ),
                timeout=timeout_s,
            )
            return {
                "output": completion.output,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "tokens": completion.tokens,
                "error": None,
            }
        except asyncio.TimeoutError:
            return {
                "output": "".join(partial) or None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "tokens": None,
                "error": f"timeout: нет ответа за {timeout_s:g} с",
            }
        except Exception as exc:  # noqa: BLE001 — сохраняем любой сбой провайдера
            return {
                "output": "".join(partial) or None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "tokens": None,
                "error": f"{type(exc).__name__}: {exc}",
            }


def _final_record(
    run_id: str, prompt: str, state: dict[str, Any], *, status: str
) -> dict[str, Any]:
    profile: InstanceProfile = state["profile"]
    last_output = state["stages"][-1]["output"] if state["stages"] else None
    return {
        "id": state["id"],
        "run_id": run_id,
        "slot": state["slot"],
        "profile": profile.key,
        "profile_label": profile.label,
        "temperature": state["temperature"],
        "prompt": prompt,
        "output": last_output,
        "latency_ms": state["latency_ms"],
        "tokens": state["tokens"] or None,
        "error": state["error"],
        "status": status,
        "stages": list(state["stages"]),
    }


def _notify(callback: ResponseCallback | None, record: dict[str, Any]) -> None:
    """Позвать подписчика, не роняя забег из-за его ошибок."""
    if callback is None:
        return
    try:
        callback(record)
    except Exception:
        pass


def _notify_stage(callback: StageCallback | None, info: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(info)
    except Exception:
        pass


def _notify_plain(callback: PlainCallback | None) -> None:
    if callback is None:
        return
    try:
        callback()
    except Exception:
        pass
