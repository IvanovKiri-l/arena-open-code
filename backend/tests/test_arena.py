"""Баг-тест ядра арены.

Гоняет run_arena на фейковом клиенте: без сети и без внешних пакетов.
Запуск из каталога backend:

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

# Изолированная SQLite на время тестов: задаём ДО импорта app-модулей.
_TMP_DIR = tempfile.mkdtemp(prefix="arena-test-")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "arena-test.db")

from app.config import settings  # noqa: E402
from app.core import store  # noqa: E402
from app.core.arena import ArenaOptions, run_arena  # noqa: E402
from app.db import init_db  # noqa: E402
from app.llm.fake import FakeLLMClient  # noqa: E402
from app.prompts.roles import build_profiles  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class CountingFakeClient(FakeLLMClient):
    """Фейк + счётчик одновременных вызовов (проверка семафора)."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.active = 0
        self.max_active = 0

    async def complete(self, *args, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            return await super().complete(*args, **kwargs)
        finally:
            self.active -= 1


class TestProfiles(unittest.TestCase):
    def test_default_mapping_10(self) -> None:
        profiles = build_profiles(10)
        self.assertEqual(len(profiles), 10)
        self.assertEqual([p.key for p in profiles[:3]], ["conservative"] * 3)
        self.assertEqual([p.key for p in profiles[3:6]], ["balanced"] * 3)
        self.assertEqual([p.key for p in profiles[6:]], ["creative"] * 4)
        self.assertEqual(profiles[0].temperature, 0.3)
        self.assertEqual(profiles[3].temperature, 0.7)
        self.assertEqual(profiles[6].temperature, 1.0)

    def test_small_n(self) -> None:
        self.assertEqual([p.key for p in build_profiles(1)], ["conservative"])
        self.assertEqual(
            [p.key for p in build_profiles(2)], ["conservative", "balanced"]
        )
        self.assertEqual(len(build_profiles(0)), 0)


class ArenaTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.assertTrue(
            str(settings.db_path).startswith(tempfile.gettempdir()),
            "тестовая БД должна быть изолированной",
        )
        init_db()

    def make_options(self, **kwargs) -> ArenaOptions:
        base = dict(n=10, timeout_s=5.0, max_concurrency=5, jitter_ms=(0, 0))
        base.update(kwargs)
        return ArenaOptions(**base)


class TestRunArena(ArenaTestBase):
    async def test_full_run_10_instances(self) -> None:
        client = FakeLLMClient(delay_range=(0.0, 0.01), seed=1)
        run = await run_arena("тестовый промпт", self.make_options(), client=client)

        self.assertEqual(run.status, "done")
        self.assertEqual(len(run.responses), 10)
        self.assertEqual([r["slot"] for r in run.responses], list(range(1, 11)))
        self.assertEqual(
            [r["profile"] for r in run.responses],
            ["conservative"] * 3 + ["balanced"] * 3 + ["creative"] * 4,
        )
        self.assertEqual(len({r["id"] for r in run.responses}), 10, "id должны быть уникальны")
        for r in run.responses:
            self.assertEqual(r["status"], "ok", r.get("error"))
            self.assertIsNone(r["error"])
            self.assertRegex(r["output"], r"^ответ \d+$")
            self.assertIsInstance(r["latency_ms"], int)
        self.assertEqual(len(run.ok_responses), 10)
        self.assertEqual(client.calls, 10)

    async def test_error_slot_does_not_kill_run(self) -> None:
        # concurrency=1 → порядок вызовов совпадает с порядком слотов
        client = FakeLLMClient(delay_range=(0.0, 0.01), fail_slots={4}, seed=2)
        run = await run_arena(
            "prompt", self.make_options(n=6, max_concurrency=1), client=client
        )

        self.assertEqual(run.status, "done")
        self.assertEqual(len(run.responses), 6)
        self.assertEqual(len(run.ok_responses), 5)
        failed = [r for r in run.responses if r["status"] == "error"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["slot"], 4)
        self.assertIn("fake client failure", failed[0]["error"])

    async def test_timeout_marks_error(self) -> None:
        client = FakeLLMClient(
            delay_range=(0.0, 0.0), delay_slots={3: 5.0}, seed=3
        )
        run = await run_arena(
            "prompt", self.make_options(n=3, timeout_s=0.25, max_concurrency=1),
            client=client,
        )

        self.assertEqual([r["status"] for r in run.responses[:2]], ["ok", "ok"])
        timed_out = run.responses[2]
        self.assertEqual(timed_out["status"], "error")
        self.assertIn("timeout", timed_out["error"])
        self.assertGreaterEqual(timed_out["latency_ms"], 150)
        self.assertLess(timed_out["latency_ms"], 1500)

    async def test_semaphore_limits_concurrency(self) -> None:
        client = CountingFakeClient(delay_range=(0.05, 0.08), seed=4)
        run = await run_arena(
            "prompt", self.make_options(n=10, max_concurrency=3), client=client
        )

        self.assertEqual(len(run.responses), 10)
        self.assertLessEqual(client.max_active, 3, "семафор пропустил лишние вызовы")
        self.assertGreaterEqual(client.max_active, 2, "параллелизм не наблюдался")

    async def test_streaming_chunks_aggregate(self) -> None:
        chunks: dict[int, list[str]] = {}

        def on_chunk(slot: int, chunk: str) -> None:
            chunks.setdefault(slot, []).append(chunk)

        client = FakeLLMClient(delay_range=(0.0, 0.01), seed=5)
        run = await run_arena(
            "prompt", self.make_options(n=4), client=client, on_chunk=on_chunk
        )

        self.assertEqual(len(chunks), 4)
        for r in run.responses:
            self.assertTrue(chunks.get(r["slot"]), "для слота нет чанков")
            self.assertEqual("".join(chunks[r["slot"]]), r["output"])

    async def test_all_failed_still_returns(self) -> None:
        client = FakeLLMClient(
            delay_range=(0.0, 0.0), fail_slots={1, 2, 3}, seed=6
        )
        run = await run_arena("prompt", self.make_options(n=3), client=client)

        self.assertEqual(run.status, "done")
        self.assertEqual(run.ok_responses, [])
        self.assertTrue(all(r["status"] == "error" for r in run.responses))

    async def test_empty_prompt_rejected(self) -> None:
        client = FakeLLMClient()
        with self.assertRaises(ValueError):
            await run_arena("   ", self.make_options(n=1), client=client)

    async def test_db_persistence(self) -> None:
        client = FakeLLMClient(delay_range=(0.0, 0.01), seed=7)
        run = await run_arena("db prompt", self.make_options(n=5), client=client)

        stored = store.fetch_run(run.id)
        self.assertIsNotNone(stored)
        assert stored is not None  # для типовой проверки
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["prompt"], "db prompt")
        self.assertEqual(len(stored["responses"]), 5)
        self.assertEqual(stored["verdicts"], [])
        self.assertEqual(stored["options"]["n"], 5)
        self.assertEqual(stored["options"]["model"], "fake-model")

        first = stored["responses"][0]
        self.assertEqual(first["slot"], 1)
        self.assertEqual(first["status"], "ok")
        self.assertIsInstance(first["temperature"], float)
        self.assertEqual(first["profile"], "conservative")

        history = store.fetch_runs(limit=10)
        self.assertIn(run.id, [item["id"] for item in history])

    async def test_concurrent_runs_share_db_safely(self) -> None:
        """Два забега параллельно — общий SQLite не должен ломаться."""
        client = FakeLLMClient(delay_range=(0.0, 0.01), seed=8)
        opts = self.make_options(n=6, max_concurrency=3)
        run_a, run_b = await asyncio.gather(
            run_arena("prompt A", opts, client=client),
            run_arena("prompt B", opts, client=client),
        )

        self.assertNotEqual(run_a.id, run_b.id)
        for run in (run_a, run_b):
            stored = store.fetch_run(run.id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["status"], "done")
            self.assertEqual(len(stored["responses"]), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
