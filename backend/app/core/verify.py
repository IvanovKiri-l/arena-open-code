"""Верификация ответов: прогон кода через команду пользователя.

Если в .env задан VERIFY_CMD (например, «python -m py_compile {file}»),
каждый успешный ответ арены прогоняется через эту команду во временном
файле. Не прошедшие помечаются «disqualified» и исключаются из судейства.
Если провалились все — судья просто не находит кандидатов и вердикта нет.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..config import settings as default_settings
from . import store


class VerifyError(RuntimeError):
    """Команду верификации не удалось запустить (ошибка конфигурации)."""


@dataclass
class VerifyOutcome:
    """Итог верификации: сколько проверено, прошло и дисквалифицировано."""

    enabled: bool
    checked: int = 0
    passed: int = 0
    disqualified: int = 0
    #: Записи, у которых изменился статус (для обновления UI).
    updated: list[dict[str, Any]] = field(default_factory=list)


#: Расширение файла по языку из ```-фенса.
EXT_BY_LANG = {
    "python": ".py", "py": ".py",
    "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts",
    "bash": ".sh", "sh": ".sh", "shell": ".sh",
    "go": ".go", "rust": ".rs", "rs": ".rs", "java": ".java",
    "c": ".c", "cpp": ".cpp", "c++": ".cpp",
    "json": ".json", "yaml": ".yaml", "yml": ".yml",
    "html": ".html", "css": ".css", "sql": ".sql",
    "ruby": ".rb", "rb": ".rb", "php": ".php",
}

_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def extract_code(output: str) -> tuple[str, str]:
    """Вернуть (код, расширение).

    Берём самый длинный ```-блок (обычно это и есть решение); если фенсов
    нет — используем весь ответ как есть.
    """
    best_code = ""
    best_ext = ".txt"
    for language, body in _FENCE.findall(output or ""):
        if len(body) > len(best_code):
            best_code = body
            best_ext = EXT_BY_LANG.get(language.strip().lower(), ".txt")
    if best_code:
        return best_code, best_ext
    return output or "", ".txt"


async def verify_responses(
    run_id: str,
    responses: list[dict[str, Any]],
    *,
    cmd: str | None = None,
    cfg: Settings | None = None,
    timeout_s: float | None = None,
) -> VerifyOutcome:
    """Прогнать успешные ответы через команду верификации.

    Записи обновляются на месте (status/error) и сохраняются в SQLite.
    """
    cfg = cfg or default_settings
    command = (cmd if cmd is not None else cfg.verify_cmd).strip()
    if not command:
        return VerifyOutcome(enabled=False)

    candidates = [r for r in responses if r.get("status") == "ok"]
    timeout = timeout_s or cfg.verify_timeout_s
    semaphore = asyncio.Semaphore(max(1, cfg.max_concurrency))
    outcome = VerifyOutcome(enabled=True, checked=len(candidates))

    with tempfile.TemporaryDirectory(prefix="arena-verify-") as tmp_dir:

        async def check(record: dict[str, Any]) -> None:
            code, ext = extract_code(record.get("output") or "")
            if not code.strip():
                _disqualify(record, "verify: в ответе нет кода для проверки")
                outcome.updated.append(record)
                outcome.disqualified += 1
                store.update_response_status(record["id"], record["status"], record["error"])
                return

            file_path = Path(tmp_dir) / f"slot-{record['slot']}{ext}"
            file_path.write_text(code, encoding="utf-8")
            async with semaphore:
                returncode, output_text = await _run_command(command, file_path, timeout)

            if returncode == 0:
                outcome.passed += 1
                return

            tail = output_text.strip()[-400:] or "без вывода"
            _disqualify(record, f"verify: код {returncode}\n{tail}")
            outcome.updated.append(record)
            outcome.disqualified += 1
            store.update_response_status(record["id"], record["status"], record["error"])

        if candidates:
            await asyncio.gather(*(check(record) for record in candidates))

    return outcome


async def _run_command(command: str, file_path: Path, timeout_s: float) -> tuple[int, str]:
    """Запустить команду верификации; {file} подставляется как аргумент."""
    argv = [part.replace("{file}", str(file_path)) for part in shlex.split(command)]
    if not argv:
        raise VerifyError("пустая команда верификации")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(file_path.parent),
        )
    except OSError as exc:
        raise VerifyError(f"команда верификации не найдена: {argv[0]!r} ({exc})") from exc

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return 124, f"timeout: верификация не уложилась в {timeout_s:g} с"

    return process.returncode or 0, (stdout or b"").decode("utf-8", "replace")


def _disqualify(record: dict[str, Any], reason: str) -> None:
    record["status"] = "disqualified"
    record["error"] = reason
