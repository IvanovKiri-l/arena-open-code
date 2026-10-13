"""Тесты HTTP-API: запуск забега, SSE-стрим, история, экспорт, переголосование."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

# Изолированная SQLite: задаём ДО импорта app-модулей.
_TMP_DIR = tempfile.mkdtemp(prefix="arena-api-test-")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "api-test.db")

with warnings.catch_warnings():
    # starlette ругается на httpx в TestClient — только шум в тестах
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.llm.base import Completion, LLMClient  # noqa: E402
from app.llm.fake import FakeJudgeClient, FakeLLMClient  # noqa: E402
from app.main import app  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._client_ctx = TestClient(app)
        cls.client = cls._client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client_ctx.__exit__(None, None, None)

    def setUp(self) -> None:
        init_db()
        arena_patch = mock.patch(
            "app.core.arena.build_client",
            return_value=FakeLLMClient(delay_range=(0.0, 0.01), seed=1),
        )
        arena_patch.start()
        self.addCleanup(arena_patch.stop)
        judge_patch = mock.patch(
            "app.core.judge.build_client", return_value=FakeJudgeClient(seed=1)
        )
        judge_patch.start()
        self.addCleanup(judge_patch.stop)

    # ── helpers ──────────────────────────────────────────────────────

    def _start(self, payload: dict) -> str:
        response = self.client.post("/arena", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["run_id"]

    def _wait(self, run_id: str, timeout: float = 15.0, need_verdict: bool = True) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            last = self.client.get(f"/arena/{run_id}").json()
            if last.get("status") == "done" and (not need_verdict or last.get("verdicts")):
                return last
            time.sleep(0.05)
        raise AssertionError(f"забег не завершился за {timeout}s: {last}")

    # ── тесты ────────────────────────────────────────────────────────

    def test_start_run_and_fetch(self) -> None:
        run_id = self._start({"prompt": "тест api", "n": 4, "judges": 1})
        data = self._wait(run_id)

        self.assertEqual(data["prompt"], "тест api")
        self.assertEqual(len(data["responses"]), 4)
        self.assertEqual(len(data["verdicts"]), 1)
        self.assertIsNotNone(data["winner_id"])
        self.assertEqual([r["slot"] for r in data["responses"]], [1, 2, 3, 4])
        self.assertNotIn("run_id", data["responses"][0])
        self.assertNotIn("prompt", data["responses"][0])

        runs = self.client.get("/runs").json()["runs"]
        self.assertIn(run_id, [item["id"] for item in runs])

    def test_stream_delivers_live_events(self) -> None:
        slow = FakeLLMClient(delay_range=(0.1, 0.15), seed=2)
        with mock.patch("app.core.arena.build_client", return_value=slow):
            run_id = self._start({"prompt": "стрим", "n": 3, "judges": 1})

            types: list[str] = []
            with self.client.stream("GET", f"/arena/{run_id}/stream") as response:
                self.assertEqual(response.status_code, 200)
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    types.append(event["type"])
                    if event["type"] == "end":
                        break
                    self.assertLess(len(types), 1000, "слишком длинный поток")

        for expected in ("chunk", "response", "judging", "verdict", "end"):
            self.assertIn(expected, types, f"в стриме нет события {expected}")
        self.assertEqual(types.count("response"), 3)

    def test_stream_unknown_run(self) -> None:
        types: list[str] = []
        with self.client.stream("GET", "/arena/nonexistent/stream") as response:
            for line in response.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    types.append(event["type"])
                    if event["type"] == "end":
                        break
        self.assertEqual(types, ["error", "end"])

    def test_stream_snapshot_and_end_for_finished_run(self) -> None:
        run_id = self._start({"prompt": "снапшот", "n": 3, "judges": 1})
        self._wait(run_id)

        types: list[str] = []
        with self.client.stream("GET", f"/arena/{run_id}/stream") as response:
            for line in response.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    types.append(event["type"])
                    if event["type"] == "end":
                        break
        # поздний подписчик получает снимок из БД и сразу «end»
        self.assertEqual(types.count("response"), 3)
        self.assertIn("verdict", types)
        self.assertEqual(types[-1], "end")

    def test_export(self) -> None:
        run_id = self._start({"prompt": "экспорт", "n": 3, "judges": 1})
        self._wait(run_id)

        response = self.client.get(f"/arena/{run_id}/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["id"], run_id)
        self.assertEqual(len(payload["responses"]), 3)
        self.assertIsNotNone(payload["winner_id"])
        self.assertEqual(len(payload["verdicts"]), 1)

    def test_rejudge(self) -> None:
        run_id = self._start({"prompt": "переголосование", "n": 3, "judges": 1})
        self._wait(run_id)

        response = self.client.post(f"/arena/{run_id}/judge", json={"judges": 2})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["verdicts"]), 2)
        self.assertIsNotNone(body["winner_id"])

        stored = self.client.get(f"/arena/{run_id}").json()
        self.assertEqual(len(stored["verdicts"]), 2, "старые вердикты должны замениться")

    def test_not_found(self) -> None:
        self.assertEqual(self.client.get("/arena/bogus").status_code, 404)
        self.assertEqual(self.client.post("/arena/bogus/judge").status_code, 404)
        self.assertEqual(self.client.get("/arena/bogus/export").status_code, 404)

    def test_judge_while_running_conflicts(self) -> None:
        slow = FakeLLMClient(delay_range=(0.4, 0.5), seed=3)
        with mock.patch("app.core.arena.build_client", return_value=slow):
            run_id = self._start({"prompt": "долгий", "n": 3, "judges": 1})

            # дождаться, пока строка забега появится в БД (статус running)
            deadline = time.time() + 5
            while time.time() < deadline:
                if self.client.get(f"/arena/{run_id}").status_code == 200:
                    break
                time.sleep(0.01)

            response = self.client.post(f"/arena/{run_id}/judge")
            self.assertEqual(response.status_code, 409)

            self._wait(run_id)

    def test_verify_without_cmd_is_rejected(self) -> None:
        response = self.client.post("/arena", json={"prompt": "код", "verify": True})
        self.assertEqual(response.status_code, 400)
        self.assertIn("VERIFY_CMD", response.json().get("detail", ""))

    def test_verify_flow_over_api(self) -> None:
        class FixedClient(LLMClient):
            model = "fixed"

            def __init__(self, outputs: list[str]) -> None:
                self.outputs = list(outputs)

            async def complete(
                self, prompt, *, system=None, temperature=0.7, stream=False, on_chunk=None
            ) -> Completion:
                await asyncio.sleep(0.15)
                return Completion(output=self.outputs.pop(0))

        good = "Решение:\n```python\nprint('ok')\n```"
        bad = "```python\ndef broken(:\n```"
        cmd = f'"{sys.executable}" -m py_compile {{file}}'
        patched = dataclasses.replace(settings, verify_cmd=cmd)
        fixed = FixedClient([good, bad])

        with mock.patch("app.api.routes.settings", patched), mock.patch(
            "app.core.arena.build_client", return_value=fixed
        ):
            run_id = self._start({"prompt": "код", "n": 2, "judges": 1, "verify": True})

            types: list[str] = []
            latest_responses: dict[int, str] = {}
            with self.client.stream("GET", f"/arena/{run_id}/stream") as response:
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    types.append(event["type"])
                    if event["type"] == "response":
                        latest_responses[event["data"]["slot"]] = event["data"]["status"]
                    if event["type"] == "end":
                        break
            self.assertIn("verifying", types)

        data = self._wait(run_id)
        statuses = sorted(r["status"] for r in data["responses"])
        self.assertEqual(statuses, ["disqualified", "ok"])
        # единственный выживший ответ побеждает автоматически
        self.assertEqual(len(data["verdicts"]), 1)
        ok = next(r for r in data["responses"] if r["status"] == "ok")
        self.assertEqual(data["winner_id"], ok["id"])
        disqualified = next(r for r in data["responses"] if r["status"] == "disqualified")
        self.assertIn("verify", disqualified["error"])
        # UI обновился до финального статуса через SSE
        self.assertEqual(sorted(latest_responses.values()), ["disqualified", "ok"])
