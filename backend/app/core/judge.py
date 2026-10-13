"""Судейство забега: LLM-судья выбирает лучший ответ.

Особенности:
* ответы подаются судье под случайными номерами — ни порядок, ни профиль
  инстанса не влияют на решение;
* вердикт — строго JSON, валидируется; при провале ровно один повтор;
* несколько судей (JUDGES=3) — победитель большинством голосов, при равенстве
  решают сумма баллов и (как последний рубеж) номер слота.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..config import settings as default_settings
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..prompts.judge import JUDGE_SYSTEM_PROMPT, build_judge_prompt
from . import store


class JudgeError(Exception):
    """Ответ судьи невалиден: не JSON, нет полей или значения вне допустимых."""


@dataclass
class JudgeResult:
    """Итог судейства: победитель + вердикты судей + ошибки неудавшихся судей."""

    winner_id: str | None
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def judge_run(
    run_id: str,
    prompt: str,
    responses: list[dict[str, Any]],
    *,
    client: LLMClient | None = None,
    cfg: Settings = default_settings,
    criteria: Sequence[str] | None = None,
    judges: int | None = None,
    temperature: float = 0.2,
    rng: random.Random | None = None,
) -> JudgeResult:
    """Провести судейство и сохранить вердикты в SQLite.

    responses — записи забега (как из run_arena, так и из БД). Судить можно
    только ответы со статусом "ok": ошибочные и дисквалифицированные
    верификацией исключаются автоматически.

    client можно не передавать — соберётся клиент из .env.
    """
    selected = [r for r in responses if r.get("status") == "ok"]
    if not selected:
        return JudgeResult(None, [], ["нет ответов со статусом ok — судить нечего"])

    if len(selected) == 1:
        # Судить нечего: единственный валидный ответ побеждает автоматически.
        only = selected[0]
        verdict = {
            "judge_index": 0,
            "winner_id": only["id"],
            "scores": [
                {"id": only["id"], "score": 10.0, "reason": "единственный валидный ответ"}
            ],
            "rationale": "Побеждает автоматически: других валидных ответов нет.",
        }
        _persist(run_id, [verdict], only["id"])
        return JudgeResult(only["id"], [verdict], [])

    criteria_list = [
        str(c).strip() for c in (criteria or cfg.judge_criteria) if str(c).strip()
    ] or ["корректность", "полнота", "читаемость"]
    judges_count = max(1, judges or cfg.judges)
    rnd = rng or random.Random()

    owns_client = client is None
    if client is None:
        client = build_client(cfg)

    verdicts: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        # Судьи идут последовательно: меньше давления на rate limit,
        # результат воспроизводим при фиксированном rng.
        for judge_index in range(judges_count):
            verdict, error = await _run_single_judge(
                judge_index=judge_index,
                prompt=prompt,
                criteria=criteria_list,
                selected=selected,
                client=client,
                timeout_s=cfg.llm_timeout_s,
                temperature=temperature,
                rnd=rnd,
            )
            if verdict is None:
                errors.append(f"судья {judge_index + 1}: {error}")
            else:
                verdicts.append(verdict)
    finally:
        if owns_client:
            await client.aclose()

    if not verdicts:
        # Ни один судья не справился. Старое решение (если было) не трогаем:
        # при фоновом сбое провайдера лучше сохранить прежний вердикт.
        return JudgeResult(None, [], errors)

    winner = pick_winner(verdicts, rnd)
    _persist(run_id, verdicts, winner)
    return JudgeResult(winner, verdicts, errors)


async def _run_single_judge(
    *,
    judge_index: int,
    prompt: str,
    criteria: Sequence[str],
    selected: list[dict[str, Any]],
    client: LLMClient,
    timeout_s: float,
    temperature: float,
    rnd: random.Random,
) -> tuple[dict[str, Any] | None, str | None]:
    """Один судья: свой случайный порядок + один ретрай на невалидный JSON."""
    order = list(selected)
    rnd.shuffle(order)
    display = {number: response for number, response in enumerate(order, start=1)}
    prompt_text = build_judge_prompt(
        prompt,
        criteria,
        [
            (number, (response.get("output") or "").strip())
            for number, response in display.items()
        ],
    )

    last_error: str | None = None
    for _attempt in (1, 2):  # первый заход + ровно один ретрай
        try:
            completion = await asyncio.wait_for(
                client.complete(
                    prompt_text, system=JUDGE_SYSTEM_PROMPT, temperature=temperature
                ),
                timeout=timeout_s,
            )
        except Exception as exc:  # сетевые/провайдерские сбои: клиент уже ретраил
            return None, f"{type(exc).__name__}: {exc}"
        try:
            parsed = parse_verdict(completion.output, set(display))
        except JudgeError as exc:
            last_error = str(exc)
            continue
        return (
            {
                "judge_index": judge_index,
                "winner_id": display[parsed["winner"]]["id"],
                "scores": [
                    {
                        "id": display[item["id"]]["id"],
                        "score": item["score"],
                        "reason": item["reason"],
                    }
                    for item in parsed["scores"]
                ],
                "rationale": parsed["rationale"],
            },
            None,
        )
    return None, last_error or "невалидный вердикт"


def pick_winner(
    verdicts: list[dict[str, Any]],
    rng: random.Random | None = None,
) -> str | None:
    """Победитель: большинство голосов → сумма баллов → честная жеребьёвка.

    Важно: при полном равенстве выбираем случайно, а не по номеру слота, —
    иначе у низких слотов появляется системное преимущество («любимчики»).
    """
    if not verdicts:
        return None

    votes = Counter(v["winner_id"] for v in verdicts if v.get("winner_id"))
    if not votes:
        return None
    best = max(votes.values())
    tied = [response_id for response_id, count in votes.items() if count == best]
    if len(tied) == 1:
        return tied[0]

    totals: dict[str, float] = defaultdict(float)
    for verdict in verdicts:
        for item in verdict.get("scores") or []:
            totals[item["id"]] += float(item["score"])
    best_total = max(totals.get(item, 0.0) for item in tied)
    finalists = sorted(item for item in tied if totals.get(item, 0.0) == best_total)
    if len(finalists) == 1:
        return finalists[0]

    lottery = rng or random.Random()
    return lottery.choice(finalists)


def _persist(run_id: str, verdicts: list[dict[str, Any]], winner_id: str | None) -> None:
    store.clear_verdicts(run_id)
    for verdict in verdicts:
        store.save_verdict(
            run_id,
            verdict["judge_index"],
            verdict["winner_id"],
            verdict["scores"],
            verdict["rationale"],
        )
    store.finish_run(run_id, status="done", winner_id=winner_id)


# ── Парсинг и валидация вердикта ─────────────────────────────────────


def parse_verdict(raw: str, numbers: set[int]) -> dict[str, Any]:
    """Разобрать ответ судьи в нормализованный вердикт (номера — выставочные).

    :raises JudgeError: если JSON не найден/невалиден или поля не сходятся
        с множеством номеров ответов.
    """
    data = _extract_json(raw)
    if not isinstance(data, dict):
        raise JudgeError("вердикт судьи — не JSON-объект")

    winner = _as_int(data.get("winner_id"))
    if winner is None or winner not in numbers:
        raise JudgeError(f"winner_id={data.get('winner_id')!r} не из списка ответов")

    scores_raw = data.get("scores")
    if not isinstance(scores_raw, list) or not scores_raw:
        raise JudgeError("scores отсутствует или пуст")

    scores: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in scores_raw:
        if not isinstance(item, dict):
            raise JudgeError("элемент scores — не объект")
        item_id = _as_int(item.get("id"))
        if item_id is None or item_id not in numbers:
            raise JudgeError(f"scores: неверный id {item.get('id')!r}")
        if item_id in seen:
            raise JudgeError(f"scores: id {item_id} продублирован")
        seen.add(item_id)
        score = _as_score(item.get("score"))
        if score is None:
            raise JudgeError(f"scores: неверный балл {item.get('score')!r}")
        reason = item.get("reason")
        scores.append(
            {"id": item_id, "score": score, "reason": str(reason).strip() if reason else ""}
        )

    missing = numbers - seen
    if missing:
        raise JudgeError(f"scores: пропущены ответы {sorted(missing)}")

    rationale = data.get("rationale")
    return {
        "winner": winner,
        "scores": scores,
        "rationale": str(rationale).strip() if rationale else "",
    }


def _extract_json(raw: str) -> Any:
    """Вытащить первый JSON-объект из ответа модели (фенсы, проза вокруг)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text[3:]
        if text[:4].lower() == "json":
            text = text[4:]
        if text.endswith("```"):
            text = text[:-3]
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            value, _ = decoder.raw_decode(text, idx)
            return value
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
    raise JudgeError("в ответе судьи не найден JSON-объект")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().replace(",", "."))
        except ValueError:
            return None
    else:
        return None
    return number if 0.0 <= number <= 10.0 else None
