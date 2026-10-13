"""Этапы работы инстансов: черновик → финал.

Даём агентам время: сначала черновик, затем доработка. Это заметно
улучшает качество и заодно позволяет рано отсеять слабые варианты,
не тратя токены на оставшиеся этапы.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    key: str
    title: str
    instruction: str


STAGES: tuple[Stage, ...] = (
    Stage(
        key="draft",
        title="Черновик",
        instruction=(
            "Не торопись. Сначала набросай черновик: план и ключевые идеи, "
            "можно коротко и по пунктам. Это промежуточный этап — качество важнее скорости."
        ),
    ),
    Stage(
        key="final",
        title="Финал",
        instruction=(
            "Теперь доведи работу до финального вида: учти заметки к черновику, "
            "улучши структуру и формулировки, убери лишнее. "
            "Ответ должен быть цельным и готовым к использованию."
        ),
    ),
)


def build_stage_prompt(task: str, stage: Stage, previous: str | None = None) -> str:
    """Промпт конкретного этапа (с черновиком прошлого этапа, если он есть)."""
    parts = [task.strip(), "", "---"]
    if previous:
        parts += [
            "Твой черновик с прошлого этапа:",
            '"""',
            previous.strip(),
            '"""',
            "",
        ]
    parts.append(f"Этап «{stage.title}». {stage.instruction}")
    return "\n".join(parts)
