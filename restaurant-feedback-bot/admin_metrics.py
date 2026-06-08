"""Метрики для глобального админа: уникальные пользователи по точкам."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import db_pulse


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _unique_users_from_jsonl(
    chat_ids: set[str],
    jsonl_path: Path,
    *,
    days: int | None = None,
) -> dict[str, set[int]]:
    if not jsonl_path.exists():
        return {}
    cutoff: datetime | None = None
    if days is not None and days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    users: dict[str, set[int]] = {cid: set() for cid in chat_ids}
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid = d.get("restaurant_chat_id")
        if cid is None:
            continue
        cid_s = str(cid)
        if cid_s not in chat_ids:
            continue
        uid = d.get("user_id")
        if uid is None:
            continue
        try:
            uid_i = int(uid)
        except (TypeError, ValueError):
            continue
        if cutoff is not None:
            ts = _parse_ts(d.get("ts"))
            if ts is not None:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
        users[cid_s].add(uid_i)
    return users


async def _unique_users_from_postgres(
    chat_ids: list[int],
    *,
    days: int | None = None,
) -> dict[str, set[int]]:
    pool = db_pulse.pool()
    if pool is None or not chat_ids:
        return {}
    q = """
        SELECT restaurant_chat_id, telegram_user_id
        FROM feedback_events
        WHERE restaurant_chat_id = ANY($1::bigint[])
          AND telegram_user_id IS NOT NULL
    """
    args: list[Any] = [chat_ids]
    if days is not None and days > 0:
        q += " AND created_at >= now() - make_interval(days => $2)"
        args.append(int(days))
    out: dict[str, set[int]] = {}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(q, *args)
        for r in rows:
            cid_s = str(int(r["restaurant_chat_id"]))
            out.setdefault(cid_s, set()).add(int(r["telegram_user_id"]))
    except Exception as e:
        print("[admin_metrics postgres]", repr(e))
    return out


async def count_unique_users_by_chat(
    chat_ids: list[int | str],
    *,
    jsonl_path: Path | None = None,
    days: int | None = None,
) -> dict[str, int]:
    """
    Уникальные сотрудники (telegram user_id), ответившие хотя бы раз на точке.
    days=None — за всё время; days=30 — активные за 30 дней.
    """
    id_strs = [str(c) for c in chat_ids]
    id_ints: list[int] = []
    for c in chat_ids:
        try:
            id_ints.append(int(c))
        except (TypeError, ValueError):
            continue

    merged: dict[str, set[int]] = {cid: set() for cid in id_strs}

    pg = await _unique_users_from_postgres(id_ints, days=days)
    for cid, uids in pg.items():
        merged.setdefault(cid, set()).update(uids)

    if jsonl_path:
        jl = _unique_users_from_jsonl(set(id_strs), jsonl_path, days=days)
        for cid, uids in jl.items():
            merged.setdefault(cid, set()).update(uids)

    return {cid: len(merged.get(cid, set())) for cid in id_strs}


async def count_raters_in_period(
    chat_ids: list[int | str],
    start: datetime,
    end: datetime,
    *,
    jsonl_path: Path | None = None,
) -> dict[str, int]:
    """Уникальные user_id с событием rating за период (по точкам)."""
    id_strs = [str(c) for c in chat_ids]
    id_ints: list[int] = []
    for c in chat_ids:
        try:
            id_ints.append(int(c))
        except (TypeError, ValueError):
            continue
    merged: dict[str, set[int]] = {cid: set() for cid in id_strs}

    pool = db_pulse.pool()
    if pool and id_ints:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT restaurant_chat_id, telegram_user_id
                    FROM feedback_events
                    WHERE restaurant_chat_id = ANY($1::bigint[])
                      AND event_type = 'rating'
                      AND telegram_user_id IS NOT NULL
                      AND created_at >= $2 AND created_at <= $3
                    """,
                    id_ints,
                    start,
                    end,
                )
            for r in rows:
                merged.setdefault(str(int(r["restaurant_chat_id"])), set()).add(
                    int(r["telegram_user_id"])
                )
        except Exception as e:
            print("[admin_metrics raters pg]", repr(e))

    if jsonl_path and jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(d.get("event") or d.get("event_type")) != "rating":
                continue
            cid = d.get("restaurant_chat_id")
            if cid is None:
                continue
            cid_s = str(cid)
            if cid_s not in merged:
                continue
            uid = d.get("user_id")
            if uid is None:
                continue
            try:
                uid_i = int(uid)
            except (TypeError, ValueError):
                continue
            ts = _parse_ts(d.get("ts"))
            if ts is not None:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                st = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
                en = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
                if ts < st or ts > en:
                    continue
            merged[cid_s].add(uid_i)

    return {cid: len(merged.get(cid, set())) for cid in id_strs}


def format_engagement_line(
    raters: int,
    baseline: int,
    *,
    period_label: str = "за период",
) -> str:
    if baseline <= 0:
        if raters > 0:
            return f"<b>Вовлечённость:</b> ответили <b>{raters}</b> {period_label}"
        return "<b>Вовлечённость:</b> <i>пока нет ответов</i>"
    pct = min(100, round(100 * raters / baseline))
    quiet = " · ⚠️ низкий отклик" if pct < 35 and baseline >= 3 else ""
    return (
        f"<b>Вовлечённость:</b> <b>{pct}%</b> "
        f"({raters} из ~{baseline} активных за 30 дн.){quiet}"
    )
