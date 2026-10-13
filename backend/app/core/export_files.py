"""Выгрузка ответов агентов на диск.

После судейства забег раскладывается по файлам:

    data/answers/<run_id>/run.md
    data/answers/<run_id>/verdict.md
    data/answers/<run_id>/slot-01-conservative.md
    data/answers/<run_id>/slot-07-creative-winner.md

Так результат можно посмотреть глазами — без UI и без API.
"""
from __future__ import annotations

from pathlib import Path

from ..db import db_path
from . import store


def answers_root() -> Path:
    """Каталог с файлами ответов — рядом с базой (data/answers)."""
    return db_path().parent / "answers"


def answers_dir_for(run_id: str) -> Path:
    return answers_root() / run_id


def relative_answers_dir(run_id: str) -> str | None:
    """Путь к папке забега для показа в UI (None, если папки ещё нет)."""
    path = answers_dir_for(run_id)
    if not path.exists():
        return None
    try:
        return path.relative_to(db_path().parent.parent).as_posix()
    except ValueError:
        return path.as_posix()


def save_run_answers(run_id: str) -> Path:
    """Разложить забег по файлам. Возвращает каталог, куда всё сохранено."""
    stored = store.fetch_run(run_id)
    if stored is None:
        raise ValueError(f"забег {run_id} не найден")

    root = answers_dir_for(run_id)
    root.mkdir(parents=True, exist_ok=True)
    winner_id = stored.get("winner_id")

    listing: list[str] = []
    for response in stored["responses"]:
        is_winner = response["id"] == winner_id
        suffix = "-winner" if is_winner else ""
        name = f"slot-{response['slot']:02d}-{response['profile']}{suffix}.md"
        mark = " ★ победитель" if is_winner else ""
        body = "\n".join(
            [
                f"# Слот #{response['slot']} — {response['profile']}{mark}",
                "",
                f"- temperature: {response['temperature']}",
                f"- status: {response['status']}",
                f"- latency_ms: {response.get('latency_ms')}",
                f"- tokens: {response.get('tokens')}",
                f"- error: {response.get('error')}",
                "",
                "---",
                "",
                "## Итоговый ответ",
                "",
                response.get("output") or "(пусто)",
                "",
                *[
                    line
                    for stage in response.get("stages") or []
                    for line in (
                        f"## Этап: {stage.get('title') or stage.get('key')}",
                        "",
                        stage.get("output") or "(пусто)",
                        "",
                    )
                ],
            ]
        )
        (root / name).write_text(body, encoding="utf-8")
        listing.append(f"- {name}{' ★' if is_winner else ''}")

    run_md = "\n".join(
        [
            f"# Забег {run_id}",
            "",
            f"**Промпт:** {stored['prompt']}",
            "",
            f"- создан: {stored['created_at']}",
            f"- статус: {stored['status']}",
            f"- модель: {stored['options'].get('model', '—')}",
            "",
            "## Ответы",
            "",
            *listing,
            "",
        ]
    )
    (root / "run.md").write_text(run_md, encoding="utf-8")

    verdict_lines = ["# Вердикты судей", ""]
    for verdict in stored["verdicts"]:
        verdict_lines.extend(
            [
                f"## Судья {verdict['judge_index'] + 1}",
                "",
                f"- победитель (id): {verdict.get('winner_id')}",
                f"- обоснование: {verdict.get('rationale') or '—'}",
                "",
            ]
        )
        for score in verdict.get("scores") or []:
            verdict_lines.append(
                f"  - `{score['id']}` — {score['score']} ({score.get('reason') or '—'})"
            )
        verdict_lines.append("")
    (root / "verdict.md").write_text("\n".join(verdict_lines), encoding="utf-8")

    return root
