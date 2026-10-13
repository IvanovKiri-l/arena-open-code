"""Тесты верификации: извлечение кода и прогон через реальные команды."""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

# Изолированная SQLite: задаём ДО импорта app-модулей.
_TMP_DIR = tempfile.mkdtemp(prefix="arena-verify-test-")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "verify-test.db")

from app.config import settings  # noqa: E402
from app.core import store  # noqa: E402
from app.core.judge import judge_run  # noqa: E402
from app.core.verify import VerifyError, extract_code, verify_responses  # noqa: E402
from app.db import init_db  # noqa: E402


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


#: Реальная команда компиляции — как в .env.example.
PY_COMPILE = f'"{sys.executable}" -m py_compile {{file}}'


class TestExtractCode(unittest.TestCase):
    def test_fenced_python(self) -> None:
        code, ext = extract_code("Вот решение:\n```python\nprint(1)\n```\nГотово")
        self.assertEqual(code.strip(), "print(1)")
        self.assertEqual(ext, ".py")

    def test_largest_fence_wins(self) -> None:
        text = "```py\nx = 1\n```\n```python\nprint('hello')\nprint('world')\n```"
        code, ext = extract_code(text)
        self.assertIn("world", code)
        self.assertEqual(ext, ".py")

    def test_unknown_language_gets_txt(self) -> None:
        code, ext = extract_code("```brainfuck\n+++\n```")
        self.assertEqual(ext, ".txt")

    def test_no_fence_returns_whole_text(self) -> None:
        code, ext = extract_code("print(42)")
        self.assertEqual(code, "print(42)")
        self.assertEqual(ext, ".txt")

    def test_empty_output(self) -> None:
        code, ext = extract_code("")
        self.assertEqual(code, "")
        self.assertEqual(ext, ".txt")


class TestVerifyResponses(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        init_db()

    async def test_disabled_without_command(self) -> None:
        run_id, responses = make_run(["print(1)"])
        outcome = await verify_responses(run_id, responses, cmd="")

        self.assertFalse(outcome.enabled)
        self.assertEqual(responses[0]["status"], "ok")

    async def test_valid_passes_invalid_disqualified(self) -> None:
        good = "Вот код:\n```python\nprint('ok')\n```"
        bad = "```python\ndef broken(:\n```"
        run_id, responses = make_run([good, bad])
        outcome = await verify_responses(run_id, responses, cmd=PY_COMPILE)

        self.assertTrue(outcome.enabled)
        self.assertEqual(outcome.checked, 2)
        self.assertEqual(outcome.passed, 1)
        self.assertEqual(outcome.disqualified, 1)
        self.assertEqual(len(outcome.updated), 1)
        self.assertEqual(responses[0]["status"], "ok")
        self.assertEqual(responses[1]["status"], "disqualified")
        self.assertIn("verify", responses[1]["error"])

        stored = store.fetch_run(run_id)
        statuses = [r["status"] for r in stored["responses"]]
        self.assertEqual(statuses, ["ok", "disqualified"])

    async def test_no_code_disqualified(self) -> None:
        run_id, responses = make_run(["   "])
        outcome = await verify_responses(run_id, responses, cmd=PY_COMPILE)

        self.assertEqual(outcome.disqualified, 1)
        self.assertIn("нет кода", responses[0]["error"])

    async def test_text_answer_fails_compile(self) -> None:
        run_id, responses = make_run(["Просто текст без кода, но не пустой"])
        outcome = await verify_responses(run_id, responses, cmd=PY_COMPILE)
        self.assertEqual(outcome.disqualified, 1)

    async def test_command_not_found_raises(self) -> None:
        run_id, responses = make_run(["```python\nprint(1)\n```"])
        with self.assertRaises(VerifyError):
            await verify_responses(
                run_id, responses, cmd="definitely-not-a-command-xyz {file}"
            )

    async def test_timeout_disqualifies(self) -> None:
        sleeper = f'"{sys.executable}" -c "import time; time.sleep(5)"'
        run_id, responses = make_run(["```python\nprint(1)\n```"])
        outcome = await verify_responses(run_id, responses, cmd=sleeper, timeout_s=0.5)

        self.assertEqual(outcome.disqualified, 1)
        self.assertIn("timeout", responses[0]["error"])

    async def test_error_responses_skipped(self) -> None:
        run_id, responses = make_run(
            ["упал", "```python\nprint(1)\n```"], statuses=["error", "ok"]
        )
        outcome = await verify_responses(run_id, responses, cmd=PY_COMPILE)

        self.assertEqual(outcome.checked, 1)
        self.assertEqual(outcome.passed, 1)
        self.assertEqual(responses[0]["status"], "error")

    async def test_all_failed_leads_to_no_winner(self) -> None:
        # если провалились все — судить некого, вердикта нет
        run_id, responses = make_run(["```python\nbroken(:\n```"])
        await verify_responses(run_id, responses, cmd=PY_COMPILE)

        unconfigured = dataclasses.replace(settings, llm_base_url="", llm_model="")
        result = await judge_run(run_id, "задача", responses, client=None, cfg=unconfigured)

        self.assertIsNone(result.winner_id)
        self.assertTrue(result.errors)
        stored = store.fetch_run(run_id)
        self.assertEqual(stored["verdicts"], [])
