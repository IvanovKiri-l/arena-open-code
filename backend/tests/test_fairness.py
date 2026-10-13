"""Честность арены: нет «любимчиков» — победа не залипает на слот и профиль.

Проверяем на равноценных ответах: в серии забегов победителями должны
становиться разные слоты и разные роли, а судья — видеть ответы анонимными,
в перемешанном порядке (это уже покрыто test_judge, здесь — распределение).
"""
from __future__ import annotations

import asyncio
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path

# Изолированная SQLite: задаём ДО импорта app-модулей.
_TMP_DIR = tempfile.mkdtemp(prefix="arena-fairness-test-")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "fairness-test.db")

from app.core.arena import ArenaOptions, run_arena  # noqa: E402
from app.core.judge import judge_run  # noqa: E402
from app.db import init_db  # noqa: E402
from app.llm.fake import FakeJudgeClient, FakeLLMClient  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class TestFairness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Windows-планировщик иногда даёт колбэки >100 мс — не шумим в логе.
        asyncio.get_running_loop().slow_callback_duration = 10.0
        init_db()

    async def _winner_of(self, seed: int, judges: int = 1) -> tuple[int, str]:
        """Один забег на равноценных ответах: кто выиграл и под каким профилем."""
        client = FakeLLMClient(delay_range=(0.0, 0.0), seed=seed)
        options = ArenaOptions(n=6, timeout_s=5.0, max_concurrency=3, jitter_ms=(0, 0))
        run = await run_arena("одинаковая задача", options, client=client)
        result = await judge_run(
            run.id,
            run.prompt,
            run.responses,
            client=FakeJudgeClient(seed=seed),
            judges=judges,
            rng=random.Random(seed),
        )
        winner = next(r for r in run.responses if r["id"] == result.winner_id)
        return winner["slot"], winner["profile"]

    async def test_no_slot_or_profile_favorites(self) -> None:
        outcomes = [await self._winner_of(seed) for seed in range(12)]
        slots = {slot for slot, _ in outcomes}
        profiles = {profile for _, profile in outcomes}

        self.assertGreaterEqual(
            len(slots), 3, f"победитель залипает на одних и тех же слотах: {outcomes}"
        )
        self.assertGreaterEqual(
            len(profiles), 2, f"победитель залипает на одном профиле: {outcomes}"
        )

    async def test_first_slot_has_no_advantage(self) -> None:
        # слот №1 не должен побеждать заметно чаще остальных
        outcomes = [await self._winner_of(seed) for seed in range(100, 112)]
        first_slot_wins = sum(1 for slot, _ in outcomes if slot == 1)
        self.assertLessEqual(
            first_slot_wins, 5, f"первый слот побеждает слишком часто: {outcomes}"
        )

    async def test_panel_of_three_judges_has_no_favorites(self) -> None:
        # именно здесь раньше был баг: при расколе голосов тай-брейк уводил
        # победу в минимальный слот — проверим, что этого больше нет
        outcomes = [await self._winner_of(seed, judges=3) for seed in range(12)]
        slots = {slot for slot, _ in outcomes}
        profiles = {profile for _, profile in outcomes}
        self.assertGreaterEqual(len(slots), 3, f"жюри залипает на слотах: {outcomes}")
        self.assertGreaterEqual(len(profiles), 2, f"жюри залипает на профиле: {outcomes}")
