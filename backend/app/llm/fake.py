"""Фейковые LLM-клиенты: не ходят в сеть, годятся для тестов и разработки UI."""
from __future__ import annotations

import asyncio
import inspect
import json
import random
import re

from .base import ChunkHandler, Completion, LLMClient


class FakeLLMClient(LLMClient):
    """Возвращает «ответ N» (N — номер вызова по порядку).

    Параметры позволяют симулировать поведение реального провайдера:
    * delay_range / delay_slots — задержки (проверка таймаутов);
    * failure_rate / fail_slots — сбои отдельных вызовов.
    """

    def __init__(
        self,
        *,
        delay_range: tuple[float, float] = (0.05, 0.2),
        delay_slots: dict[int, float] | None = None,
        failure_rate: float = 0.0,
        fail_slots: set[int] | None = None,
        failure_message: str = "fake client failure",
        seed: int | None = None,
    ) -> None:
        self.model = "fake-model"
        self._delay_range = delay_range
        self._delay_slots = dict(delay_slots or {})
        self._failure_rate = failure_rate
        self._fail_slots = set(fail_slots or set())
        self._failure_message = failure_message
        self._rng = random.Random(seed)
        self._calls = 0

    @property
    def calls(self) -> int:
        """Сколько раз вызывали complete() (для тестов и отладки)."""
        return self._calls

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        stream: bool = False,
        on_chunk: ChunkHandler | None = None,
    ) -> Completion:
        self._calls += 1
        call_no = self._calls

        delay = self._delay_slots.get(call_no)
        if delay is None:
            delay = self._rng.uniform(*self._delay_range)
        await asyncio.sleep(delay)

        if call_no in self._fail_slots or self._rng.random() < self._failure_rate:
            raise RuntimeError(f"{self._failure_message} (call {call_no})")

        text = f"ответ {call_no}"

        if stream and on_chunk is not None:
            parts = text.split(" ")
            for i, part in enumerate(parts):
                chunk = part + (" " if i < len(parts) - 1 else "")
                result = on_chunk(chunk)
                if inspect.isawaitable(result):
                    await result

        return Completion(output=text, tokens=len(text.split()), model=self.model)


class FakeJudgeClient(LLMClient):
    """Фейковый судья для офлайн-прогонов (CLI --fake) и тестов судейства.

    Выбирает самый длинный ответ и возвращает валидный JSON-вердикт.
    """

    model = "fake-judge"

    def __init__(
        self,
        *,
        failure_rate: float = 0.0,
        bad_json_rate: float = 0.0,
        seed: int | None = None,
    ) -> None:
        self._failure_rate = failure_rate
        self._bad_json_rate = bad_json_rate
        self._rng = random.Random(seed)
        self.calls = 0

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        stream: bool = False,
        on_chunk: ChunkHandler | None = None,
    ) -> Completion:
        self.calls += 1
        await asyncio.sleep(0)
        if self._rng.random() < self._failure_rate:
            raise RuntimeError("fake judge failure")
        if self._rng.random() < self._bad_json_rate:
            return Completion(output="Это рассуждения без JSON.", model=self.model)

        answers = _parse_numbered_answers(prompt)
        if not answers:
            return Completion(output="{}", model=self.model)

        winner = max(answers, key=lambda number: len(answers[number]))
        verdict = {
            "winner_id": winner,
            "scores": [
                {
                    "id": number,
                    "score": 10.0 if number == winner else 5.0,
                    "reason": "fake-судья: самый длинный ответ"
                    if number == winner
                    else "fake",
                }
                for number in sorted(answers)
            ],
            "rationale": "Fake-судья всегда выбирает самый длинный ответ.",
        }
        return Completion(
            output=json.dumps(verdict, ensure_ascii=False), model=self.model
        )


def _parse_numbered_answers(prompt: str) -> dict[int, str]:
    """Вытащить из промпта судьи блоки «### Ответ N»."""
    pattern = r"### Ответ (\d+)\n(.*?)(?=\n### Ответ |\nВерни |\Z)"
    matches = re.findall(pattern, prompt, flags=re.DOTALL)
    return {int(number): text.strip() for number, text in matches}
