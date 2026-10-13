"""Роли и профили инстансов арены.

Раскладка по умолчанию (N=10): экземпляры 1–3 — консервативные/минималисты
(temperature 0.3), 4–6 — сбалансированные (0.7), 7–10 — креативные (1.0).
Тексты ролей живут здесь, а не в коде арены, — их можно менять и
локализовать в одном месте.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstanceProfile:
    """Профиль одного инстанса: роль + температура."""

    key: str
    label: str
    temperature: float
    system_prompt: str


CONSERVATIVE = InstanceProfile(
    key="conservative",
    label="консервативный/минималист",
    temperature=0.3,
    system_prompt=(
        "Ты — консервативный эксперт-минималист. Отвечай максимально точно, "
        "строго и лаконично. Используй только хорошо проверенные решения, "
        "не добавляй лишнего, избегай экспериментов и украшательств. "
        "Если задача допускает минимальное решение — выбери его."
    ),
)

BALANCED = InstanceProfile(
    key="balanced",
    label="сбалансированный",
    temperature=0.7,
    system_prompt=(
        "Ты — опытный специалист со сбалансированным подходом: точность важна, "
        "но ответ должен быть полным, понятным и практичным. "
        "Объясняй ключевые решения и приводи примеры там, где это уместно."
    ),
)

CREATIVE = InstanceProfile(
    key="creative",
    label="креативный/экспериментальный",
    temperature=1.0,
    system_prompt=(
        "Ты — креативный исследователь. Предлагай нестандартные, смелые решения "
        "и альтернативные подходы, допускай эксперименты. При этом ответ должен "
        "оставаться релевантным задаче и по возможности корректным."
    ),
)

PROFILES: tuple[InstanceProfile, ...] = (CONSERVATIVE, BALANCED, CREATIVE)


def build_profiles(n: int) -> list[InstanceProfile]:
    """Разложить n инстансов по ролям: сперва консервативные, затем
    сбалансированные, затем креативные. Для n=10 это 3 / 3 / 4."""
    if n <= 0:
        return []
    if n <= len(PROFILES):
        return list(PROFILES[:n])
    base = max(1, round(n / len(PROFILES)))
    counts = [base] * (len(PROFILES) - 1) + [n - base * (len(PROFILES) - 1)]
    out: list[InstanceProfile] = []
    for profile, count in zip(PROFILES, counts):
        out.extend([profile] * count)
    return out
