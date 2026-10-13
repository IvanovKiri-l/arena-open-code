"""Дым-тест FastAPI-приложения: поднятие, lifespan (init_db) и /health."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import warnings
from pathlib import Path

# Изолированная SQLite: задаём ДО импорта app-модулей.
_TMP_DIR = tempfile.mkdtemp(prefix="arena-app-test-")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "app-test.db")

with warnings.catch_warnings():
    # starlette ругается DeprecationWarning'ом про httpx в TestClient —
    # для тестов это неважно, но засоряет вывод.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class TestAppSmoke(unittest.TestCase):
    def test_lifespan_and_health(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with TestClient(app) as client:
                resp = client.get("/health")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "arena-backend")
        self.assertIn("llm_configured", body)
        self.assertTrue(
            Path(settings.db_path).exists(), "init_db не создал SQLite-файл"
        )
