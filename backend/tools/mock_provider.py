"""Локальный мок OpenAI-совместимого провайдера — демо и e2e без ключей.

Запуск (из каталога backend):

    python tools/mock_provider.py --port 8011

Настройки в .env:

    LLM_BASE_URL=http://127.0.0.1:8011/v1
    LLM_MODEL=mock-model
    LLM_API_KEY=mock

Умеет: /chat/completions (plain и SSE-стрим), а на «судейский» промпт
возвращает валидный JSON-вердикт (выбирает самый длинный ответ).
"""
from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _extract_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _numbered_answers(prompt: str) -> dict[int, str]:
    pattern = r"### Ответ (\d+)\n(.*?)(?=\n### Ответ |\nВерни |\Z)"
    return {
        int(number): text.strip()
        for number, text in re.findall(pattern, prompt, flags=re.DOTALL)
    }


def _judge_verdict(prompt: str) -> str | None:
    """Если у нас просят вердикт — вернуть валидный JSON (самый длинный ответ)."""
    if "winner_id" not in prompt:
        return None
    answers = _numbered_answers(prompt)
    if not answers:
        return None
    winner = max(answers, key=lambda number: len(answers[number]))
    return json.dumps(
        {
            "winner_id": winner,
            "scores": [
                {
                    "id": number,
                    "score": 10.0 if number == winner else 5.0,
                    "reason": "mock-судья: самый содержательный ответ"
                    if number == winner
                    else "mock",
                }
                for number in sorted(answers)
            ],
            "rationale": "Mock-судья: ответы равноценны по форме, порядок перемешан — победа честная.",
        },
        ensure_ascii=False,
    )


#: Все mock-ответы выравниваются до одной длины: у судьи не должно быть
#: «любимчиков» — ни один профиль не выигрывает только за счёт длины ответа.
_TARGET_LENGTH = 240


def _mock_answer(user_text: str, model: str, temperature: Any) -> str:
    try:
        temp = float(temperature)
    except (TypeError, ValueError):
        temp = 0.5
    if temp <= 0.4:
        style = "Минимальное решение: выбран самый простой и строгий вариант."
    elif temp <= 0.8:
        style = "Сбалансированное решение: рассмотрены варианты и выбран практичный."
    else:
        style = "Креативное решение: предложены нестандартные альтернативы и эксперимент."
    head = f"Mock-ответ модели «{model}» (temperature={temp:.1f}). Задача: {user_text.strip()}"
    body = f"{head}\n\n{style} "
    filler = "Дополнение не влияет на оценку судьи. "
    while len(body) < _TARGET_LENGTH:
        body += filler
    return body[:_TARGET_LENGTH]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # тише в консоли
        pass

    def _read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 — имя диктует BaseHTTPRequestHandler
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json(404, {"error": {"message": "only /chat/completions"}})
            return

        payload = self._read_payload()
        model = str(payload.get("model") or "mock-model")
        user_text = _extract_user_text(payload.get("messages") or [])

        verdict_json = _judge_verdict(user_text)
        if verdict_json is not None:
            self._send_json(
                200,
                {
                    "model": model,
                    "choices": [
                        {"message": {"role": "assistant", "content": verdict_json}}
                    ],
                    "usage": {"total_tokens": 42},
                },
            )
            return

        answer = _mock_answer(user_text, model, payload.get("temperature"))

        if payload.get("stream"):
            self._stream(answer, model)
            return

        self._send_json(
            200,
            {
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": answer}}],
                "usage": {"total_tokens": len(answer.split())},
            },
        )

    def _stream(self, answer: str, model: str) -> None:
        """Настоящий SSE-стрим через chunked-кодирование, с паузами."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        words = answer.split(" ")
        for index, word in enumerate(words):
            piece = word + (" " if index < len(words) - 1 else "")
            event = json.dumps(
                {"model": model, "choices": [{"delta": {"content": piece}}]},
                ensure_ascii=False,
            )
            self._write_chunk(f"data: {event}\n\n")
            time.sleep(0.02)
        final = json.dumps(
            {
                "model": model,
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"total_tokens": len(words)},
            },
            ensure_ascii=False,
        )
        self._write_chunk(f"data: {final}\n\n")
        self._write_chunk("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _write_chunk(self, text: str) -> None:
        data = text.encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock OpenAI-совместимый провайдер")
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock-provider: http://{args.host}:{args.port}/v1 (Ctrl+C — стоп)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
