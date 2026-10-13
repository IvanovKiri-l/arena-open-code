"""Тесты OpenAI-совместимого клиента.

Часть тестов гоняет клиент по-настоящему по HTTP: поднимается крошечный
мок-провайдер на 127.0.0.1 (stdlib http.server) и проверяются plain-ответ,
SSE-стриминг, retry на 429 и сквозной забег через run_arena.
Сеть наружу не нужна.
"""
from __future__ import annotations

import asyncio
import dataclasses
import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

# Изолированная SQLite: задаём ДО импорта app-модулей.
_TMP_DIR = tempfile.mkdtemp(prefix="arena-http-test-")
os.environ["DB_PATH"] = str(Path(_TMP_DIR) / "http-test.db")
os.environ["STAGES"] = "1"

from app.config import settings  # noqa: E402
from app.core.arena import ArenaOptions, run_arena  # noqa: E402
from app.db import init_db  # noqa: E402
from app.llm import build_client  # noqa: E402
from app.llm.fake import FakeLLMClient  # noqa: E402
from app.llm.openai_compat import OpenAICompatClient, ProviderError  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class _MockOpenAIHandler(http.server.BaseHTTPRequestHandler):
    """Мини-провайдер: POST /v1/chat/completions (plain, SSE, 429, 401)."""

    protocol_version = "HTTP/1.1"
    requests_log: list[dict] = []
    flaky_hits = 0

    def log_message(self, *args) -> None:  # silence в выводе тестов
        pass

    def _send_json(self, code: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 — имя диктует BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        type(self).requests_log.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "payload": payload,
            }
        )

        model = payload.get("model", "")
        if model == "always-429":
            self._send_json(
                429, {"error": {"message": "rate limited"}}, {"Retry-After": "0"}
            )
            return
        if model == "unauthorized":
            self._send_json(401, {"error": {"message": "invalid key"}})
            return
        if model == "flaky-429":
            type(self).flaky_hits += 1
            if type(self).flaky_hits < 3:
                self._send_json(
                    429, {"error": {"message": "slow down"}}, {"Retry-After": "0"}
                )
                return

        user_text = ""
        for message in payload.get("messages") or []:
            if message.get("role") == "user":
                user_text = message.get("content") or ""
        answer = f"эхо: {user_text}"

        if payload.get("stream"):
            events = [
                {"choices": [{"delta": {"content": "эхо: "}}]},
                {"choices": [{"delta": {"content": user_text}}]},
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 7},
                },
            ]
            body = b"".join(
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
                for event in events
            ) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self._send_json(
            200,
            {
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": answer}}],
                "usage": {"total_tokens": 5},
            },
        )


class TestOpenAiCompatOverRealHttp(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockOpenAIHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}/v1"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        super().tearDownClass()

    async def asyncSetUp(self) -> None:
        # Локальный HTTP + планировщик Windows иногда дают колбэки >100 мс —
        # это нормально, просто не засоряем лог asyncio debug-предупреждениями.
        asyncio.get_running_loop().slow_callback_duration = 10.0
        init_db()
        _MockOpenAIHandler.requests_log.clear()
        _MockOpenAIHandler.flaky_hits = 0
        self.client = OpenAICompatClient(
            base_url=self.base_url,
            api_key="test-key",
            model="test-model",
            timeout_s=10.0,
            max_retries=2,
            backoff_base_s=0.01,
        )
        self.addAsyncCleanup(self.client.aclose)

    def _log(self) -> list[dict]:
        return _MockOpenAIHandler.requests_log

    async def test_plain_completion(self) -> None:
        result = await self.client.complete("привет", system="будь краток", temperature=0.3)

        self.assertEqual(result.output, "эхо: привет")
        self.assertEqual(result.tokens, 5)

        entry = self._log()[-1]
        self.assertEqual(entry["path"], "/v1/chat/completions")
        self.assertEqual(entry["auth"], "Bearer test-key")
        payload = entry["payload"]
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["temperature"], 0.3)
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "будь краток"})
        self.assertEqual(payload["messages"][-1]["role"], "user")

    async def test_streaming_completion(self) -> None:
        chunks: list[str] = []
        result = await self.client.complete("стрим", stream=True, on_chunk=chunks.append)

        self.assertEqual(result.output, "эхо: стрим")
        self.assertEqual("".join(chunks), "эхо: стрим")
        self.assertEqual(result.tokens, 7)
        self.assertTrue(self._log()[-1]["payload"]["stream"])

    async def test_429_backoff_then_success(self) -> None:
        self.client.model = "flaky-429"
        result = await self.client.complete("ок")

        self.assertEqual(result.output, "эхо: ок")
        attempts = [e for e in self._log() if e["payload"]["model"] == "flaky-429"]
        self.assertEqual(len(attempts), 3, "ожидались 429, 429 и успех")

    async def test_429_exhausts_retries(self) -> None:
        self.client.model = "always-429"
        with self.assertRaises(ProviderError) as ctx:
            await self.client.complete("нет")

        self.assertEqual(ctx.exception.status_code, 429)
        attempts = [e for e in self._log() if e["payload"]["model"] == "always-429"]
        self.assertEqual(len(attempts), 3, "max_retries=2 → 3 попытки")

    async def test_401_is_not_retried(self) -> None:
        self.client.model = "unauthorized"
        with self.assertRaises(ProviderError) as ctx:
            await self.client.complete("нет")

        self.assertEqual(ctx.exception.status_code, 401)
        attempts = [e for e in self._log() if e["payload"]["model"] == "unauthorized"]
        self.assertEqual(len(attempts), 1, "401 повторять бессмысленно")

    async def test_run_arena_end_to_end_over_http(self) -> None:
        run = await run_arena(
            "мир",
            ArenaOptions(n=3, timeout_s=10.0, max_concurrency=2, jitter_ms=(0, 0)),
            client=self.client,
        )

        self.assertEqual(len(run.responses), 3)
        for response in run.responses:
            self.assertEqual(response["status"], "ok", response.get("error"))
            self.assertEqual(response["output"], "эхо: мир")

        temperatures = {e["payload"]["temperature"] for e in self._log()}
        self.assertEqual(temperatures, {0.3, 0.7, 1.0})
        self.assertTrue(all(e["path"] == "/v1/chat/completions" for e in self._log()))


class _CloseTrackingClient(FakeLLMClient):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class TestClientWiring(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        init_db()

    def _options(self) -> ArenaOptions:
        return ArenaOptions(n=2, timeout_s=5.0, max_concurrency=2, jitter_ms=(0, 0))

    def test_build_client_requires_config(self) -> None:
        unconfigured = dataclasses.replace(settings, llm_base_url="", llm_model="")
        with self.assertRaises(RuntimeError):
            build_client(unconfigured)

    async def test_build_client_from_settings(self) -> None:
        configured = dataclasses.replace(
            settings,
            llm_base_url="http://127.0.0.1:9/v1",
            llm_model="unit-model",
            llm_api_key="k",
        )
        client = build_client(configured)
        try:
            self.assertIsInstance(client, OpenAICompatClient)
            self.assertEqual(client.model, "unit-model")
        finally:
            await client.aclose()

    async def test_default_client_created_and_closed(self) -> None:
        made = _CloseTrackingClient(delay_range=(0.0, 0.0), seed=11)
        with mock.patch("app.core.arena.build_client", return_value=made) as builder:
            run = await run_arena("prompt", self._options(), cfg=settings)

        builder.assert_called_once_with(settings)
        self.assertTrue(made.closed, "клиент, собранный ареной, должен закрываться")
        self.assertEqual(len(run.responses), 2)

    async def test_provided_client_is_not_closed(self) -> None:
        client = _CloseTrackingClient(delay_range=(0.0, 0.0), seed=12)
        await run_arena("prompt", self._options(), client=client)
        self.assertFalse(client.closed, "чужой клиент арена закрывать не должна")
