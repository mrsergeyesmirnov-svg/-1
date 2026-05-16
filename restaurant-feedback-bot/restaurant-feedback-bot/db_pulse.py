"""
Минимальная запись событий в PostgreSQL (Railway DATABASE_URL).

Если переменной нет или подключение не удалось — бот работает только с JSONL, без падения.
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None

DDL_TABLE = """
CREATE TABLE IF NOT EXISTS feedback_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    telegram_user_id BIGINT,
    organization_id TEXT,
    restaurant_chat_id BIGINT,
    event_type TEXT NOT NULL,
    rating SMALLINT,
    problem_code TEXT,
    comment_text TEXT,
    payload JSONB
)
"""
DDL_IDX1 = (
    "CREATE INDEX IF NOT EXISTS idx_feedback_events_org_time "
    "ON feedback_events (organization_id, created_at DESC)"
)
DDL_IDX2 = (
    "CREATE INDEX IF NOT EXISTS idx_feedback_events_chat_time "
    "ON feedback_events (restaurant_chat_id, created_at DESC)"
)


async def init_db(dsn: str) -> bool:
    global _pool
    dsn = dsn.strip()
    if not dsn:
        return False
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]
    try:
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=5,
            command_timeout=60,
        )
        async with _pool.acquire() as conn:
            await conn.execute(DDL_TABLE)
            await conn.execute(DDL_IDX1)
            await conn.execute(DDL_IDX2)
        print("[postgres] подключено, таблица feedback_events готова")
        return True
    except Exception as e:
        print("[postgres] не используется:", repr(e))
        _pool = None
        return False


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool | None:
    return _pool


async def insert_feedback_event(row: dict[str, Any]) -> None:
    if _pool is None:
        return
    event_type = row.get("event")
    if not event_type:
        return
    reserved = {
        "event",
        "user_id",
        "organization_id",
        "restaurant_chat_id",
        "rating",
        "problem",
        "comment",
    }
    payload = {k: v for k, v in row.items() if k not in reserved}
    payload_json = json.dumps(payload, ensure_ascii=False) if payload else "{}"
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO feedback_events (
                    telegram_user_id, organization_id, restaurant_chat_id,
                    event_type, rating, problem_code, comment_text, payload
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                """,
                row.get("user_id"),
                row.get("organization_id"),
                row.get("restaurant_chat_id"),
                event_type,
                row.get("rating"),
                row.get("problem"),
                row.get("comment"),
                payload_json,
            )
    except Exception as e:
        print("[postgres insert_feedback_event]", repr(e))
