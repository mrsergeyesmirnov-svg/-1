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
DDL_IDX3 = (
    "CREATE INDEX IF NOT EXISTS idx_feedback_events_type_time "
    "ON feedback_events (event_type, created_at DESC)"
)

DDL_PROBLEMS = """
CREATE TABLE IF NOT EXISTS problems (
    id BIGSERIAL PRIMARY KEY,
    organization_id TEXT,
    restaurant_chat_id BIGINT NOT NULL,
    problem_key TEXT NOT NULL,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'button',
    mentions_count INT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    manager_comment TEXT,
    first_detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (restaurant_chat_id, problem_key)
)
"""
DDL_PROBLEMS_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_problems_chat_status "
    "ON problems (restaurant_chat_id, status)"
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
            await conn.execute(DDL_IDX3)
            await conn.execute(DDL_PROBLEMS)
            await conn.execute(DDL_PROBLEMS_STATUS)
            # Если таблицу создавали вручную без DEFAULT для created_at — починим для будущих INSERT.
            await conn.execute(
                "ALTER TABLE feedback_events "
                "ALTER COLUMN created_at SET DEFAULT now()"
            )
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
                    created_at,
                    telegram_user_id, organization_id, restaurant_chat_id,
                    event_type, rating, problem_code, comment_text, payload
                )
                VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8::jsonb)
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


def _problem_row_to_dict(r) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "organization_id": r["organization_id"],
        "restaurant_chat_id": int(r["restaurant_chat_id"]),
        "problem_key": r["problem_key"],
        "title": r["title"],
        "source_type": r["source_type"],
        "mentions_count": int(r["mentions_count"]),
        "status": r["status"],
        "manager_comment": r["manager_comment"],
        "first_detected_at": r["first_detected_at"],
        "last_detected_at": r["last_detected_at"],
        "resolved_at": r["resolved_at"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


async def fetch_problems_for_chat(
    chat_id: int,
    *,
    include_ignored: bool = True,
) -> list[dict[str, Any]]:
    if _pool is None:
        return []
    q = """
        SELECT * FROM problems
        WHERE restaurant_chat_id = $1
    """
    if not include_ignored:
        q += " AND status != 'ignored'"
    q += (
        " ORDER BY CASE status "
        "WHEN 'new' THEN 0 WHEN 'in_progress' THEN 1 "
        "WHEN 'resolved' THEN 2 ELSE 3 END, first_detected_at ASC NULLS LAST"
    )
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(q, chat_id)
        return [_problem_row_to_dict(r) for r in rows]
    except Exception as e:
        print("[postgres fetch_problems_for_chat]", repr(e))
        return []


async def fetch_problem_by_id(problem_id: str) -> dict[str, Any] | None:
    if _pool is None:
        return None
    try:
        pid = int(problem_id)
    except ValueError:
        return None
    try:
        async with _pool.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM problems WHERE id = $1", pid)
        return _problem_row_to_dict(r) if r else None
    except Exception as e:
        print("[postgres fetch_problem_by_id]", repr(e))
        return None


async def upsert_problem(
    *,
    restaurant_chat_id: int,
    organization_id: str | None,
    problem_key: str,
    title: str,
    mentions_count: int,
    now,
    threshold: int = 3,
) -> tuple[dict[str, Any], bool]:
    if _pool is None:
        raise RuntimeError("no pool")
    created = False
    try:
        async with _pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, status, mentions_count FROM problems
                WHERE restaurant_chat_id = $1 AND problem_key = $2
                """,
                restaurant_chat_id,
                problem_key,
            )
            if existing:
                prev_status = existing["status"]
                if prev_status == "resolved" and mentions_count >= threshold:
                    new_status = "new"
                    clear_resolved = True
                elif prev_status == "ignored" and mentions_count >= threshold:
                    new_status = "new"
                    clear_resolved = True
                else:
                    new_status = prev_status
                    clear_resolved = False
                row = await conn.fetchrow(
                    """
                    UPDATE problems SET
                        title = $2,
                        mentions_count = $3,
                        last_detected_at = $4,
                        updated_at = $4,
                        organization_id = COALESCE($5, organization_id),
                        status = $6,
                        resolved_at = CASE WHEN $7 THEN NULL ELSE resolved_at END
                    WHERE id = $1
                    RETURNING *
                    """,
                    existing["id"],
                    title,
                    mentions_count,
                    now,
                    organization_id,
                    new_status,
                    clear_resolved,
                )
            else:
                created = True
                row = await conn.fetchrow(
                    """
                    INSERT INTO problems (
                        organization_id, restaurant_chat_id, problem_key, title,
                        source_type, mentions_count, status,
                        first_detected_at, last_detected_at
                    ) VALUES ($1, $2, $3, $4, 'button', $5, 'new', $6, $6)
                    RETURNING *
                    """,
                    organization_id,
                    restaurant_chat_id,
                    problem_key,
                    title,
                    mentions_count,
                    now,
                )
        return _problem_row_to_dict(row), created
    except Exception as e:
        print("[postgres upsert_problem]", repr(e))
        raise


async def update_problem_status(
    problem_id: str,
    status: str,
    manager_comment: str | None,
    *,
    now,
) -> dict[str, Any] | None:
    if _pool is None:
        return None
    try:
        pid = int(problem_id)
    except ValueError:
        return None
    resolved_at = now if status == "resolved" else None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE problems SET
                    status = $2,
                    manager_comment = $3,
                    updated_at = $4,
                    resolved_at = COALESCE($5, resolved_at)
                WHERE id = $1
                RETURNING *
                """,
                pid,
                status,
                manager_comment,
                now,
                resolved_at,
            )
        return _problem_row_to_dict(row) if row else None
    except Exception as e:
        print("[postgres update_problem_status]", repr(e))
        return None


async def insert_manual_problem(
    *,
    restaurant_chat_id: int,
    organization_id: str | None,
    problem_key: str,
    title: str,
    now,
) -> dict[str, Any]:
    if _pool is None:
        raise RuntimeError("no pool")
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO problems (
                organization_id, restaurant_chat_id, problem_key, title,
                source_type, mentions_count, status,
                first_detected_at, last_detected_at
            ) VALUES ($1, $2, $3, $4, 'manual', 0, 'new', $5, $5)
            RETURNING *
            """,
            organization_id,
            restaurant_chat_id,
            problem_key,
            title,
            now,
        )
    return _problem_row_to_dict(row)
