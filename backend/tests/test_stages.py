"""Тесты этапной работы (черновик → финал) и раннего отбора черновиков."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

# Изолированная SQLite: задаём ДО импорта app-модулей.
_TMP_DIR = tempfile.mkdtemp(prefix="arena-stages-test-")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "stages-test.db")

from app.core.arena import ArenaOptions, run_arena  # noqa: E402
from app.db import init_db  # noqa: E402
from app.llm.base import ChunkHandler, Completion, LLMClient  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class StageAwareClient(LLMClient):
    """Имитация этапного провайдера: черновик → отбор → финал."""

    model = "stage-aware"

    def __init__(self) -> None:
        self.stage1_calls = 0
        self.stage2_calls = 0
        self.screen_calls = 0

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        stream: bool = False,
        on_chunk: ChunkHandler | None = None,
    ) -> Completion:
        if "БЫСТРЫЙ ОТБОР" in prompt:
            self.screen_calls += 1
            answers = re.findall(
                r"### Ответ (\d+)\n(.*?)(?=\n### Ответ |\nВерни |\Z)",
                prompt,
                flags=re.DOTALL,
            )
            scores = [
                {
                    "id": int(number),
                    "score": 1.0 if "ПЛОХО" in text else 8.0,
                    "reason": "слабый черновик" if "ПЛОХО" in text else "хороший задел",
                }
                for number, text in answers
            ]
            return Completion(output=json.dumps({"scores": scores}, ensure_ascii=False))

        if "Твой черновик" in prompt:
            self.stage2_calls += 1
            return Completion(output="ФИНАЛ: доработанный и улучшенный ответ")

        self.stage1_calls += 1
        quality = (
            "ЧЕРНОВИК: ПЛОХО, обрывки мыслей без ответа"
            if temperature <= 0.4
            else "ЧЕРНОВИК: нормальный план работы"
        )
        return Completion(output=quality)


class BrokenScreenClient(StageAwareClient):
    """Судья-фильтр отвечает мусором: отбор не должен никого выкидывать."""

    async def complete(self, prompt: str, **kwargs: Any) -> Completion:
        if "БЫСТРЫЙ ОТБОР" in prompt:
            self.screen_calls += 1
            return Completion(output="никакого JSON тут нет")
        return await super().complete(prompt, **kwargs)


class TestStagedArena(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 10.0
        init_db()

    def _options(self, n: int = 6) -> ArenaOptions:
        return ArenaOptions(
            n=n,
            timeout_s=5.0,
            max_concurrency=3,
            jitter_ms=(0, 0),
            stages=2,
            screening=True,
        )

    async def test_stages_and_early_screening(self) -> None:
        client = StageAwareClient()
        response_events: list[dict[str, Any]] = []
        stage_events: list[dict[str, Any]] = []
        screening_seen: list[bool] = []

        run = await run_arena(
            "задача",
            self._options(),
            client=client,
            on_response=response_events.append,
            on_stage=stage_events.append,
            on_screening=lambda: screening_seen.append(True),
        )

        self.assertEqual([event["index"] for event in stage_events], [1, 2])
        self.assertEqual([event["title"] for event in stage_events], ["Черновик", "Финал"])
        self.assertEqual(len(screening_seen), 1)

        # 6 черновиков → отбор → 4 финала (двое слабых выбыли раньше времени)
        self.assertEqual(client.stage1_calls, 6)
        self.assertEqual(client.screen_calls, 1)
        self.assertEqual(client.stage2_calls, 4)

        statuses = [response["status"] for response in run.responses]
        self.assertEqual(statuses.count("disqualified"), 2)
        self.assertEqual(statuses.count("ok"), 4)

        eliminated = [r for r in run.responses if r["status"] == "disqualified"]
        for record in eliminated:
            self.assertIn("отбор", record["error"])
            self.assertEqual(len(record["stages"]), 1)  # только черновик
            self.assertEqual(record["stages"][0]["title"], "Черновик")

        survivors = [r for r in run.responses if r["status"] == "ok"]
        for record in survivors:
            self.assertEqual(len(record["stages"]), 2)
            self.assertEqual(record["output"], "ФИНАЛ: доработанный и улучшенный ответ")

        # выбывшие отдали запись раньше финала: событий не меньше шести
        self.assertGreaterEqual(len(response_events), 6)

    async def test_screening_failure_eliminates_nobody(self) -> None:
        client = BrokenScreenClient()
        run = await run_arena("задача", self._options(), client=client)

        self.assertEqual(client.screen_calls, 2)  # оригинал + один ретрай
        self.assertEqual(client.stage2_calls, 6)  # никто не выбыл
        self.assertTrue(all(response["status"] == "ok" for response in run.responses))

    async def test_single_stage_mode_skips_screening(self) -> None:
        client = StageAwareClient()
        options = ArenaOptions(
            n=6, timeout_s=5.0, max_concurrency=3, jitter_ms=(0, 0), stages=1
        )
        run = await run_arena("задача", options, client=client)

        self.assertEqual(client.stage1_calls, 6)
        self.assertEqual(client.screen_calls, 0)
        self.assertEqual(client.stage2_calls, 0)
        self.assertTrue(all(len(response["stages"]) == 1 for response in run.responses))
