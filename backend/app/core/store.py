"""Тонкий слой доступа к SQLite: забеги, ответы, вердикты.

Каждая операция — отдельное короткое соединение; этого достаточно
для SQLite в WAL-режиме и не требует внешних СУБД.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..db import connect


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def create_run(run_id: str, prompt: str, options: dict[str, Any]) -> None:
    _execute(
        "INSERT INTO runs (id, created_at, prompt, options, status) "
        "VALUES (?, ?, ?, ?, 'running')",
        (run_id, utcnow_iso(), prompt, json.dumps(options, ensure_ascii=False)),
    )


def save_response(record: dict[str, Any]) -> None:
    _execute(
        """
        INSERT INTO responses
            (id, run_id, slot, profile, temperature, output, latency_ms, tokens, error, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["id"],
            record["run_id"],
            record["slot"],
            record["profile"],
            record["temperature"],
            record.get("output"),
            record.get("latency_ms"),
            record.get("tokens"),
            record.get("error"),
            record.get("status", "ok"),
        ),
    )


def update_response_status(response_id: str, status: str, error: str | None = None) -> None:
    """Обновить статус ответа (верификация помечает ответы disqualified)."""
    _execute(
        "UPDATE responses SET status = ?, error = ? WHERE id = ?",
        (status, error, response_id),
    )


def finish_run(run_id: str, *, status: str = "done", winner_id: str | None = None) -> None:
    _execute(
        "UPDATE runs SET status = ?, winner_id = ? WHERE id = ?",
        (status, winner_id, run_id),
    )


def clear_verdicts(run_id: str) -> None:
    """Удалить вердикты забега (перед повторным судейством)."""
    _execute("DELETE FROM verdicts WHERE run_id = ?", (run_id,))


def save_verdict(
    run_id: str,
    judge_index: int,
    winner_id: str | None,
    scores: list[dict[str, Any]],
    rationale: str | None,
) -> str:
    verdict_id = str(uuid.uuid4())
    _execute(
        """
        INSERT INTO verdicts (id, run_id, judge_index, winner_id, scores, rationale, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verdict_id,
            run_id,
            judge_index,
            winner_id,
            json.dumps(scores, ensure_ascii=False),
            rationale,
            utcnow_iso(),
        ),
    )
    return verdict_id


def fetch_run(run_id: str) -> dict[str, Any] | None:
    """Забег целиком: метаданные + ответы + вердикты."""
    conn = connect()
    try:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            return None
        responses = conn.execute(
            "SELECT * FROM responses WHERE run_id = ? ORDER BY slot ASC", (run_id,)
        ).fetchall()
        verdicts = conn.execute(
            "SELECT * FROM verdicts WHERE run_id = ? ORDER BY judge_index ASC", (run_id,)
        ).fetchall()
    finally:
        conn.close()

    result = dict(run)
    result["options"] = json.loads(result.get("options") or "{}")
    result["responses"] = [dict(row) for row in responses]
    result["verdicts"] = []
    for row in verdicts:
        verdict = dict(row)
        verdict["scores"] = json.loads(verdict.get("scores") or "[]")
        result["verdicts"].append(verdict)
    return result


def fetch_runs(limit: int = 50) -> list[dict[str, Any]]:
    """История забегов (свежие сверху) — для GET /runs."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.created_at, r.prompt, r.status, r.winner_id,
                   (SELECT COUNT(*) FROM responses x WHERE x.run_id = r.id) AS responses_count
            FROM runs r
            ORDER BY r.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
