"""Тесты судейства: валидация JSON, ретраи, majority vote, анти-bias порядок."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import random
import re
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

# Изолированная SQLite: задаём ДО импорта app-модулей.
_TMP_DIR = tempfile.mkdtemp(prefix="arena-judge-test-")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "judge-test.db")

from app.config import settings  # noqa: E402
from app.core import store  # noqa: E402
from app.core.judge import JudgeError, judge_run, parse_verdict, pick_winner  # noqa: E402
from app.db import init_db  # noqa: E402
from app.llm.base import Completion, LLMClient  # noqa: E402
from app.llm.fake import FakeJudgeClient  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def make_run(
    outputs: list[str], statuses: list[str] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Создать забег с готовыми ответами — без арены и без сети."""
    run_id = str(uuid.uuid4())
    store.create_run(run_id, "тестовая задача", {})
    responses: list[dict[str, Any]] = []
    for index, output in enumerate(outputs):
        record = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "slot": index + 1,
            "profile": "balanced",
            "temperature": 0.7,
            "prompt": "тестовая задача",
            "output": output,
            "latency_ms": 10,
            "tokens": 3,
            "error": None,
            "status": (statuses or ["ok"] * len(outputs))[index],
        }
        store.save_response(record)
        responses.append(record)
    return run_id, responses


def valid_verdict(n: int, winner: int = 1) -> str:
    return json.dumps(
        {
            "winner_id": winner,
            "scores": [
                {"id": i, "score": 9.0 if i == winner else 5.0, "reason": f"ответ {i}"}
                for i in range(1, n + 1)
            ],
            "rationale": "тестовое обоснование",
        },
        ensure_ascii=False,
    )


class ScriptedClient(LLMClient):
    """Отдаёт заранее заданные ответы по порядку (можно класть исключения)."""

    model = "scripted"

    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.calls = 0

    async def complete(
        self, prompt, *, system=None, temperature=0.7, stream=False, on_chunk=None
    ) -> Completion:
        self.calls += 1
        if not self._replies:
            raise AssertionError("scripted client: неожиданный лишний вызов")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Completion(output=str(reply), model=self.model)


class RecordingJudge(FakeJudgeClient):
    """Fake-судья с записью промптов (для проверки перемешивания)."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.prompts: list[str] = []

    async def complete(self, prompt, **kwargs) -> Completion:
        self.prompts.append(prompt)
        return await super().complete(prompt, **kwargs)


def displayed_number_of(prompt: str, needle: str) -> int:
    """Номер, под которым ответ с needle предъявлен судье в промпте."""
    blocks = re.split(r"### Ответ (\d+)\n", prompt)
    for i in range(1, len(blocks) - 1, 2):
        number, text = int(blocks[i]), blocks[i + 1]
        if needle in text:
            return number
    raise AssertionError(f"в промпте нет ответа с маркером {needle!r}")


class TestParseVerdict(unittest.TestCase):
    NUMBERS = {1, 2, 3}

    def test_clean_json(self) -> None:
        parsed = parse_verdict(valid_verdict(3), self.NUMBERS)
        self.assertEqual(parsed["winner"], 1)
        self.assertEqual(len(parsed["scores"]), 3)
        self.assertEqual(parsed["scores"][0]["score"], 9.0)

    def test_markdown_fence(self) -> None:
        raw = f"```json\n{valid_verdict(3, winner=2)}\n```"
        self.assertEqual(parse_verdict(raw, self.NUMBERS)["winner"], 2)

    def test_prose_around_json(self) -> None:
        raw = f"Вот мой вердикт:\n{valid_verdict(3, winner=3)}\nНадеюсь, помог!"
        self.assertEqual(parse_verdict(raw, self.NUMBERS)["winner"], 3)

    def test_garbage(self) -> None:
        with self.assertRaises(JudgeError):
            parse_verdict("никакого JSON тут нет", self.NUMBERS)

    def test_winner_out_of_range(self) -> None:
        with self.assertRaises(JudgeError):
            parse_verdict(valid_verdict(3, winner=99), self.NUMBERS)

    def test_missing_entry(self) -> None:
        data = json.loads(valid_verdict(3))
        data["scores"] = data["scores"][:2]
        with self.assertRaises(JudgeError):
            parse_verdict(json.dumps(data), self.NUMBERS)

    def test_duplicate_ids(self) -> None:
        data = json.loads(valid_verdict(3))
        data["scores"][1]["id"] = data["scores"][0]["id"]
        with self.assertRaises(JudgeError):
            parse_verdict(json.dumps(data), self.NUMBERS)

    def test_score_out_of_range(self) -> None:
        data = json.loads(valid_verdict(3))
        data["scores"][0]["score"] = 11
        with self.assertRaises(JudgeError):
            parse_verdict(json.dumps(data), self.NUMBERS)

    def test_lenient_score_string(self) -> None:
        data = json.loads(valid_verdict(3))
        data["scores"][0]["score"] = "8,5"
        parsed = parse_verdict(json.dumps(data), self.NUMBERS)
        self.assertEqual(parsed["scores"][0]["score"], 8.5)


class TestPickWinner(unittest.TestCase):
    SELECTED = [
        {"id": "a", "slot": 1},
        {"id": "b", "slot": 2},
        {"id": "c", "slot": 3},
    ]

    @staticmethod
    def verdict(winner: str, scores: dict[str, float]) -> dict:
        return {
            "winner_id": winner,
            "scores": [{"id": key, "score": value} for key, value in scores.items()],
            "rationale": "",
        }

    def test_clear_majority(self) -> None:
        verdicts = [
            self.verdict("a", {"a": 9, "b": 4, "c": 5}),
            self.verdict("a", {"a": 8, "b": 7, "c": 6}),
            self.verdict("b", {"a": 3, "b": 9, "c": 4}),
        ]
        self.assertEqual(pick_winner(verdicts, self.SELECTED), "a")

    def test_tie_broken_by_total_score(self) -> None:
        verdicts = [
            self.verdict("a", {"a": 5, "b": 9}),
            self.verdict("b", {"a": 5, "b": 9}),
        ]
        self.assertEqual(pick_winner(verdicts, self.SELECTED), "b")

    def test_full_tie_broken_by_slot(self) -> None:
        verdicts = [
            self.verdict("b", {"a": 5, "b": 5}),
            self.verdict("a", {"a": 5, "b": 5}),
        ]
        self.assertEqual(pick_winner(verdicts, self.SELECTED), "a")

    def test_no_verdicts(self) -> None:
        self.assertIsNone(pick_winner([], self.SELECTED))


class JudgeRunTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Windows-планировщик иногда даёт колбэки >100 мс — не засоряем лог.
        asyncio.get_running_loop().slow_callback_duration = 10.0
        init_db()


class TestJudgeRun(JudgeRunTestBase):
    async def test_happy_path_longest_answer_wins(self) -> None:
        run_id, responses = make_run(
            ["коротко", "самый длинный и подробный ответ здесь", "средний"]
        )
        client = FakeJudgeClient(seed=1)
        result = await judge_run(run_id, "задача", responses, client=client)

        self.assertEqual(result.errors, [])
        self.assertIsNotNone(result.winner_id)
        winner = next(r for r in responses if r["id"] == result.winner_id)
        self.assertEqual(winner["output"], "самый длинный и подробный ответ здесь")
        self.assertEqual(len(result.verdicts), 1)

        stored = store.fetch_run(run_id)
        self.assertEqual(stored["winner_id"], result.winner_id)
        self.assertEqual(len(stored["verdicts"]), 1)
        scores = stored["verdicts"][0]["scores"]
        self.assertEqual({s["id"] for s in scores}, {r["id"] for r in responses})

    async def test_prompt_contains_task_and_criteria(self) -> None:
        run_id, responses = make_run(["длиннющий ответ", "короткий"])
        client = RecordingJudge(seed=1)
        await judge_run(
            run_id,
            "Напиши fizzbuzz",
            responses,
            client=client,
            criteria=["точность", "полнота"],
        )

        prompt = client.prompts[0]
        self.assertIn("Напиши fizzbuzz", prompt)
        self.assertIn("точность", prompt)
        self.assertIn("полнота", prompt)
        self.assertIn("### Ответ 1", prompt)

    async def test_three_judges_majority_and_random_order(self) -> None:
        outputs = [f"ответ {i}: бегемот" for i in range(1, 7)]
        outputs[2] = "ответ 3: бегемот бегемот бегемот — самый длинный"
        run_id, responses = make_run(outputs)

        client = RecordingJudge(seed=2)
        result = await judge_run(
            run_id, "задача", responses, client=client, judges=3, rng=random.Random(42)
        )

        self.assertEqual(len(client.prompts), 3)
        self.assertEqual(len(result.verdicts), 3)
        self.assertEqual(result.errors, [])
        long_id = responses[2]["id"]
        self.assertEqual(result.winner_id, long_id)
        self.assertTrue(all(v["winner_id"] == long_id for v in result.verdicts))

        # Воспроизводим shuffle тем же seed и проверяем, что судьи
        # действительно видели разные случайные порядки.
        expected_orders = []
        check = random.Random(42)
        for _ in range(3):
            order = list(range(len(responses)))
            check.shuffle(order)
            expected_orders.append(order)
        self.assertEqual(
            len({tuple(o) for o in expected_orders}), 3, "порядки должны различаться"
        )
        for judge_index, prompt in enumerate(client.prompts):
            displayed = displayed_number_of(prompt, "бегемот бегемот")
            self.assertEqual(displayed, expected_orders[judge_index].index(2) + 1)

    async def test_retry_once_on_bad_json(self) -> None:
        run_id, responses = make_run(["а", "б"])
        client = ScriptedClient(["это не JSON", valid_verdict(2)])
        result = await judge_run(run_id, "задача", responses, client=client)

        self.assertEqual(client.calls, 2)
        self.assertEqual(result.errors, [])
        self.assertIsNotNone(result.winner_id)

    async def test_retry_exhausted_gives_no_winner(self) -> None:
        run_id, responses = make_run(["а", "б"])
        client = ScriptedClient(["мусор", "снова не JSON"])
        result = await judge_run(run_id, "задача", responses, client=client)

        self.assertEqual(client.calls, 2, "должно быть ровно две попытки")
        self.assertIsNone(result.winner_id)
        self.assertEqual(len(result.errors), 1)
        stored = store.fetch_run(run_id)
        self.assertEqual(stored["verdicts"], [])
        self.assertIsNone(stored["winner_id"])

    async def test_provider_error_is_not_double_retried(self) -> None:
        run_id, responses = make_run(["а", "б"])
        client = ScriptedClient([RuntimeError("сеть умерла")])
        result = await judge_run(run_id, "задача", responses, client=client)

        self.assertEqual(client.calls, 1, "провайдерские сбои не ретраятся сверх клиента")
        self.assertIsNone(result.winner_id)
        self.assertIn("сеть умерла", result.errors[0])

    async def test_disqualified_responses_excluded(self) -> None:
        run_id, responses = make_run(
            ["очень длинный ответ дисквалифицированного", "короткий", "средний"],
            statuses=["disqualified", "ok", "ok"],
        )
        client = RecordingJudge(seed=3)
        result = await judge_run(run_id, "задача", responses, client=client)

        self.assertEqual(len(client.prompts), 1)
        self.assertNotIn("дисквалифицированного", client.prompts[0])
        self.assertNotIn("### Ответ 3", client.prompts[0])
        winner = next(r for r in responses if r["id"] == result.winner_id)
        self.assertNotEqual(winner["status"], "disqualified")

    async def test_all_failed_returns_without_winner(self) -> None:
        run_id, responses = make_run(["а", "б"], statuses=["error", "error"])
        unconfigured = dataclasses.replace(settings, llm_base_url="", llm_model="")
        result = await judge_run(run_id, "задача", responses, client=None, cfg=unconfigured)

        self.assertIsNone(result.winner_id)
        self.assertTrue(result.errors)

    async def test_single_candidate_wins_automatically(self) -> None:
        run_id, responses = make_run(["единственный", "упал"], statuses=["ok", "error"])
        unconfigured = dataclasses.replace(settings, llm_base_url="", llm_model="")
        result = await judge_run(run_id, "задача", responses, client=None, cfg=unconfigured)

        self.assertEqual(result.winner_id, responses[0]["id"])
        stored = store.fetch_run(run_id)
        self.assertEqual(stored["winner_id"], responses[0]["id"])
        self.assertEqual(len(stored["verdicts"]), 1)

    async def test_rejudge_replaces_previous_verdicts(self) -> None:
        run_id, responses = make_run(["долгий-долгий ответ", "короткий"])
        client = FakeJudgeClient(seed=4)
        first = await judge_run(run_id, "задача", responses, client=client)
        second = await judge_run(run_id, "задача", responses, client=client)

        self.assertEqual(first.winner_id, second.winner_id)
        stored = store.fetch_run(run_id)
        self.assertEqual(len(stored["verdicts"]), 1, "старые вердикты должны заменяться")

    async def test_failed_rejudge_keeps_previous_verdicts(self) -> None:
        run_id, responses = make_run(["длинный ответ здесь", "короткий"])
        await judge_run(run_id, "задача", responses, client=FakeJudgeClient(seed=5))

        failing = ScriptedClient(["мусор", "мусор"])
        result = await judge_run(run_id, "задача", responses, client=failing)

        self.assertIsNone(result.winner_id)
        stored = store.fetch_run(run_id)
        self.assertEqual(
            len(stored["verdicts"]), 1, "при провале re-judge старое решение сохраняется"
        )
