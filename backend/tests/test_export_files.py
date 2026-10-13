"""Тесты выгрузки ответов агентов на диск (data/answers/<run_id>/)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

# Изолированная SQLite: задаём ДО импорта app-модулей.
_TMP_DIR = tempfile.mkdtemp(prefix="arena-files-test-")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "files-test.db")
os.environ["STAGES"] = "1"

from app.core import store  # noqa: E402
from app.core.export_files import answers_dir_for, relative_answers_dir, save_run_answers  # noqa: E402
from app.db import init_db  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def make_run(outputs: list[str]) -> tuple[str, list[dict[str, Any]]]:
    run_id = str(uuid.uuid4())
    store.create_run(run_id, "тестовая задача", {"model": "fake-model"})
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
            "status": "ok",
        }
        store.save_response(record)
        responses.append(record)
    return run_id, responses


class TestSaveRunAnswers(unittest.TestCase):
    def setUp(self) -> None:
        init_db()

    def test_writes_all_files(self) -> None:
        run_id, responses = make_run(["первый ответ", "второй ответ — он победил"])
        store.finish_run(run_id, winner_id=responses[1]["id"])
        store.save_verdict(
            run_id,
            0,
            responses[1]["id"],
            [{"id": responses[0]["id"], "score": 5.0, "reason": "слабее"}],
            "второй ответ полнее",
        )

        root = save_run_answers(run_id)
        self.assertTrue(root.exists())

        names = sorted(path.name for path in root.iterdir())
        self.assertIn("run.md", names)
        self.assertIn("verdict.md", names)
        self.assertIn("slot-01-balanced.md", names)
        self.assertIn("slot-02-balanced-winner.md", names)

        winner_text = (root / "slot-02-balanced-winner.md").read_text(encoding="utf-8")
        self.assertIn("второй ответ — он победил", winner_text)
        self.assertIn("★", winner_text)

        run_text = (root / "run.md").read_text(encoding="utf-8")
        self.assertIn("slot-02-balanced-winner.md", run_text)
        self.assertIn("тестовая задача", run_text)

        verdict_text = (root / "verdict.md").read_text(encoding="utf-8")
        self.assertIn("Судья 1", verdict_text)
        self.assertIn("второй ответ полнее", verdict_text)

        self.assertIsNotNone(relative_answers_dir(run_id))
        self.assertTrue(str(answers_dir_for(run_id)).endswith(run_id))

    def test_missing_run_raises(self) -> None:
        with self.assertRaises(ValueError):
            save_run_answers("no-such-run")
