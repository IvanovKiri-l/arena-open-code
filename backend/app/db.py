"""SQLite-хранилище. Только SQLite — никаких внешних СУБД.

Файл базы лежит рядом с проектом (./data/arena.db); путь настраивается
через DB_PATH. Схема создаётся автоматически на старте приложения.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    options     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'running',
    winner_id   TEXT
);

CREATE TABLE IF NOT EXISTS responses (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    slot        INTEGER NOT NULL,
    profile     TEXT NOT NULL,
    temperature REAL NOT NULL,
    output      TEXT,
    latency_ms  INTEGER,
    tokens      INTEGER,
    error       TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    stages      TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    judge_index INTEGER NOT NULL,
    winner_id   TEXT,
    scores      TEXT NOT NULL DEFAULT '[]',
    rationale   TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_responses_run ON responses(run_id);
CREATE INDEX IF NOT EXISTS idx_verdicts_run  ON verdicts(run_id);
"""


def db_path() -> Path:
    path = settings.db_path
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout — запас на конкурентные записи из параллельных забегов
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        # WAL включается один раз; режим сохраняется в самом файле БД.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Мягкие миграции для баз, созданных предыдущими версиями.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(responses)")}
        if "stages" not in columns:
            conn.execute("ALTER TABLE responses ADD COLUMN stages TEXT")
        conn.commit()
    finally:
        conn.close()
