"""
Управление проблемами: пороги, статусы, дайджесты, уведомления.
Хранение: PostgreSQL (если есть) или bot_data.json → ключ problems_store.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import db_pulse
import report_pulse

# Пороги за 7 дней (кнопка problem_*)
THRESHOLDS: dict[str, int] = {
    "kitchen": 3,
    "staff": 3,
    "management": 2,
    "conflict": 3,
    "stress": 3,
    "comment": 4,
}

PROBLEM_TITLES: dict[str, str] = {
    "kitchen": "Медленная кухня",
    "staff": "Нехватка персонала",
    "management": "Плохая организация",
    "conflict": "Конфликт / напряжение",
    "stress": "Сильная нагрузка",
    "comment": "Свои комментарии (общая тема)",
}

STATUS_NEW = "new"
STATUS_IN_PROGRESS = "in_progress"
STATUS_RESOLVED = "resolved"
STATUS_IGNORED = "ignored"

STATUS_RU = {
    STATUS_NEW: "Новая",
    STATUS_IN_PROGRESS: "В работе",
    STATUS_RESOLVED: "Решена",
    STATUS_IGNORED: "Игнорируется",
}

STATUS_EMOJI = {
    STATUS_NEW: "🔴",
    STATUS_IN_PROGRESS: "🟡",
    STATUS_RESOLVED: "🟢",
    STATUS_IGNORED: "⚪",
}


@dataclass
class ProblemRow:
    id: str
    organization_id: str | None
    restaurant_chat_id: int
    problem_key: str
    title: str
    source_type: str
    mentions_count: int
    status: str
    manager_comment: str | None
    first_detected_at: datetime | None
    last_detected_at: datetime | None
    resolved_at: datetime | None


def _store_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.setdefault("problems_store", [])
    if not isinstance(raw, list):
        data["problems_store"] = []
        return data["problems_store"]
    return raw


def _new_local_id() -> str:
    import secrets

    return "p_" + secrets.token_hex(6)


def _parse_dt(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_from_dict(d: dict[str, Any]) -> ProblemRow:
    return ProblemRow(
        id=str(d["id"]),
        organization_id=d.get("organization_id"),
        restaurant_chat_id=int(d["restaurant_chat_id"]),
        problem_key=str(d.get("problem_key") or ""),
        title=str(d.get("title") or ""),
        source_type=str(d.get("source_type") or "button"),
        mentions_count=int(d.get("mentions_count") or 0),
        status=str(d.get("status") or STATUS_NEW),
        manager_comment=d.get("manager_comment"),
        first_detected_at=_parse_dt(d.get("first_detected_at")),
        last_detected_at=_parse_dt(d.get("last_detected_at")),
        resolved_at=_parse_dt(d.get("resolved_at")),
    )


async def list_problems_for_chat(
    data: dict[str, Any],
    chat_id: int,
    *,
    include_ignored: bool = True,
) -> list[ProblemRow]:
    pool = db_pulse.pool()
    if pool:
        rows = await db_pulse.fetch_problems_for_chat(chat_id, include_ignored=include_ignored)
        return [_row_from_dict(r) for r in rows]
    out: list[ProblemRow] = []
    for d in _store_list(data):
        if int(d.get("restaurant_chat_id", 0)) != int(chat_id):
            continue
        if not include_ignored and d.get("status") == STATUS_IGNORED:
            continue
        out.append(_row_from_dict(d))
    out.sort(
        key=lambda p: (
            p.status != STATUS_NEW,
            p.status != STATUS_IN_PROGRESS,
            -p.mentions_count,
        ),
    )
    return out


async def get_problem(data: dict[str, Any], problem_id: str) -> ProblemRow | None:
    pool = db_pulse.pool()
    if pool:
        d = await db_pulse.fetch_problem_by_id(problem_id)
        return _row_from_dict(d) if d else None
    for d in _store_list(data):
        if str(d.get("id")) == str(problem_id):
            return _row_from_dict(d)
    return None


async def _upsert_local(
    data: dict[str, Any],
    *,
    chat_id: int,
    org_id: str | None,
    problem_key: str,
    title: str,
    count: int,
    now: datetime,
) -> tuple[ProblemRow, bool]:
    """Возвращает (row, created)."""
    store = _store_list(data)
    for d in store:
        if (
            int(d.get("restaurant_chat_id", 0)) == int(chat_id)
            and d.get("problem_key") == problem_key
        ):
            prev_status = d.get("status", STATUS_NEW)
            d["mentions_count"] = count
            d["last_detected_at"] = now.isoformat()
            if prev_status == STATUS_RESOLVED and count >= THRESHOLDS.get(problem_key, 3):
                d["status"] = STATUS_NEW
                d["resolved_at"] = None
            return _row_from_dict(d), False
    nid = _new_local_id()
    rec = {
        "id": nid,
        "organization_id": org_id,
        "restaurant_chat_id": chat_id,
        "problem_key": problem_key,
        "title": title,
        "source_type": "button",
        "mentions_count": count,
        "status": STATUS_NEW,
        "manager_comment": None,
        "first_detected_at": now.isoformat(),
        "last_detected_at": now.isoformat(),
        "resolved_at": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    store.append(rec)
    return _row_from_dict(rec), True


async def sync_problems_from_period(
    data: dict[str, Any],
    chat_id: int,
    org_id: str | None,
    *,
    jsonl_path,
    tz_name: str = "Europe/Moscow",
    days: int = 7,
) -> list[tuple[ProblemRow, bool]]:
    """Создаёт/обновляет проблемы по порогам. (row, is_new)."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    end = datetime.now(tz)
    start = end - timedelta(days=days)
    events = await report_pulse.load_events(
        [chat_id], start, end, jsonl_path=jsonl_path
    )
    counts: dict[str, int] = {}
    for e in events:
        if e.event_type == report_pulse.EVENT_PROBLEM and e.problem_code:
            counts[e.problem_code] = counts.get(e.problem_code, 0) + 1

    now = datetime.now(tz)
    changes: list[tuple[ProblemRow, bool]] = []
    pool = db_pulse.pool()

    try:
        import survey_buttons

        thresholds = survey_buttons.thresholds_map(data, chat_id)
        titles = survey_buttons.titles_map(data, chat_id)
    except Exception:
        thresholds = THRESHOLDS
        titles = PROBLEM_TITLES

    for key, threshold in thresholds.items():
        cnt = counts.get(key, 0)
        if cnt < threshold:
            continue
        title = titles.get(key, PROBLEM_TITLES.get(key, key))
        if pool:
            row_d, created = await db_pulse.upsert_problem(
                restaurant_chat_id=chat_id,
                organization_id=org_id,
                problem_key=key,
                title=title,
                mentions_count=cnt,
                now=now,
            )
            changes.append((_row_from_dict(row_d), created))
        else:
            row, created = await _upsert_local(
                data,
                chat_id=chat_id,
                org_id=org_id,
                problem_key=key,
                title=title,
                count=cnt,
                now=now,
            )
            changes.append((row, created))
    return changes


async def update_problem_status(
    data: dict[str, Any],
    problem_id: str,
    status: str,
    manager_comment: str | None,
    *,
    now: datetime | None = None,
) -> ProblemRow | None:
    if status not in (STATUS_NEW, STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_IGNORED):
        return None
    now = now or datetime.now().astimezone()
    pool = db_pulse.pool()
    if pool:
        d = await db_pulse.update_problem_status(
            problem_id, status, manager_comment, now=now
        )
        return _row_from_dict(d) if d else None
    for d in _store_list(data):
        if str(d.get("id")) != str(problem_id):
            continue
        d["status"] = status
        d["manager_comment"] = manager_comment
        d["updated_at"] = now.isoformat()
        if status == STATUS_RESOLVED:
            d["resolved_at"] = now.isoformat()
        elif status in (STATUS_NEW, STATUS_IN_PROGRESS):
            d["resolved_at"] = None
        return _row_from_dict(d)
    return None


def format_problem_list(rows: list[ProblemRow], *, title: str) -> str:
    if not rows:
        return (
            f"<b>{escape(title)}</b>\n\n"
            "Пока нет отслеживаемых проблем по порогам за неделю.\n"
            "Они появятся, когда тема часто отмечается в check-in."
        )
    lines = [f"<b>{escape(title)}</b>\n"]
    for p in rows[:15]:
        em = STATUS_EMOJI.get(p.status, "🔴")
        st = STATUS_RU.get(p.status, p.status)
        lines.append(
            f"{em} <b>{escape(p.title)}</b> ({p.mentions_count})\n"
            f"   Статус: {escape(st)}\n"
        )
    return "\n".join(lines)


def format_problem_card(p: ProblemRow) -> str:
    fd = p.first_detected_at.strftime("%d.%m.%Y") if p.first_detected_at else "—"
    ld = p.last_detected_at.strftime("%d.%m.%Y") if p.last_detected_at else "—"
    st = STATUS_RU.get(p.status, p.status)
    lines = [
        f"<b>{escape(p.title)}</b>",
        "",
        f"Упоминаний: <b>{p.mentions_count}</b>",
        f"Первое упоминание: {fd}",
        f"Последнее упоминание: {ld}",
        f"Статус: <b>{escape(st)}</b>",
    ]
    if p.manager_comment:
        lines.append("")
        lines.append(f"Комментарий: {escape(p.manager_comment)}")
    return "\n".join(lines)


def format_group_status_post(p: ProblemRow) -> str:
    st = STATUS_RU.get(p.status, p.status)
    icon = {"in_progress": "🔄", "resolved": "✅", "ignored": "⏸", "new": "📌"}.get(
        p.status, "📢"
    )
    lines = [
        "📢 <b>Обновление по вашим отзывам</b>",
        "",
        f"{icon} <b>{escape(p.title)}</b>",
        "",
        f"Статус: <b>{escape(st)}</b>",
    ]
    if p.manager_comment and p.status in (STATUS_IN_PROGRESS, STATUS_RESOLVED):
        lines.append("")
        lines.append("Комментарий руководителя:")
        lines.append(escape(p.manager_comment))
    lines.append("")
    lines.append(
        "<i>Ответы в опросе анонимны. Спасибо, что помогаете делать смены лучше.</i>"
    )
    return "\n".join(lines)


def format_weekly_digest(rows: list[ProblemRow]) -> str:
    resolved = [p for p in rows if p.status == STATUS_RESOLVED]
    in_prog = [p for p in rows if p.status == STATUS_IN_PROGRESS]
    new = [p for p in rows if p.status == STATUS_NEW]

    lines = [
        "📊 <b>Что изменилось по вашим отзывам</b>",
        "",
    ]
    if resolved:
        lines.append("✅ <b>Решено</b>")
        for p in resolved[:8]:
            lines.append(f"• {escape(p.title)}")
        lines.append("")
    if in_prog:
        lines.append("🔄 <b>В работе</b>")
        for p in in_prog[:8]:
            extra = ""
            if p.manager_comment:
                short = p.manager_comment
                if len(short) > 80:
                    short = short[:77] + "…"
                extra = f" — {escape(short)}"
            lines.append(f"• {escape(p.title)}{extra}")
        lines.append("")
    if new:
        lines.append("🔴 <b>Новые темы</b>")
        for p in new[:6]:
            lines.append(f"• {escape(p.title)} ({p.mentions_count})")
        lines.append("")
    if not (resolved or in_prog or new):
        lines.append(
            "За неделю не накопилось повторяющихся тем по порогам. "
            "Продолжайте коротко отмечать смены — так мы быстрее замечаем сложности."
        )
    else:
        lines.append(
            "<i>Анонимные отклики → действия команды. Оценить смену можно по кнопке из чата.</i>"
        )
    return "\n".join(lines)


def format_manager_problems_report(
    chat_title: str,
    rows: list[ProblemRow],
    *,
    sync_notes: list[str] | None = None,
) -> str:
    lines = [
        f"📋 <b>Проблемы точки</b> — {escape(chat_title)}",
        "",
        format_problem_list(rows, title="Текущий список").replace(
            "<b>Текущий список</b>\n\n", ""
        ),
    ]
    if sync_notes:
        lines.append("")
        lines.append("<b>Обновлено автоматически</b>")
        for n in sync_notes[:8]:
            lines.append(f"• {escape(n)}")
    lines.append("")
    lines.append("Команда: <code>/problems</code> или кнопка «Проблемы».")
    return "\n".join(lines)


def problem_card_keyboard(problem_id: str, status: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    if status != STATUS_IN_PROGRESS:
        rows.append(
            [
                InlineKeyboardButton(
                    text="В работу",
                    callback_data=f"pr:w:{problem_id}:ip",
                )
            ]
        )
    if status != STATUS_RESOLVED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Решено",
                    callback_data=f"pr:w:{problem_id}:rs",
                )
            ]
        )
    if status != STATUS_IGNORED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Игнорировать",
                    callback_data=f"pr:w:{problem_id}:ig",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="← К списку", callback_data="pr:l")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def problems_list_keyboard(rows: list[ProblemRow]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    ik = []
    for p in rows[:12]:
        em = STATUS_EMOJI.get(p.status, "🔴")
        label = f"{em} {p.title[:28]} ({p.mentions_count})"
        ik.append(
            [InlineKeyboardButton(text=label, callback_data=f"pr:v:{p.id}")]
        )
    ik.append(
        [
            InlineKeyboardButton(
                text="⚙️ Кнопки опроса",
                callback_data="pb:cfg",
            )
        ]
    )
    ik.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить из отзывов",
                callback_data="pr:sync",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=ik)


def comment_skip_keyboard(problem_id: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пропустить комментарий",
                    callback_data=f"pr:k:{problem_id}",
                )
            ]
        ]
    )


STATUS_FROM_CB = {"ip": STATUS_IN_PROGRESS, "rs": STATUS_RESOLVED, "ig": STATUS_IGNORED}


def managers_for_chat(data: dict[str, Any], chat_id: int) -> list[int]:
    """Telegram user_id менеджеров с доступом к точке."""
    import pulse_model

    cid = str(chat_id)
    out: set[int] = set()
    org_id = pulse_model.chat_organization_id(data, chat_id)
    for uid_s, profiles in data.get("managers", {}).items():
        if not isinstance(profiles, list):
            continue
        try:
            uid = int(uid_s)
        except ValueError:
            continue
        for p in profiles:
            if not isinstance(p, dict):
                continue
            if p.get("organization_id") != org_id and org_id:
                continue
            role = p.get("role")
            if role == pulse_model.ROLE_NETWORK_ADMIN and p.get("organization_id") == org_id:
                out.add(uid)
            elif role == pulse_model.ROLE_LOCATION_ADMIN:
                locs = [str(x) for x in (p.get("location_chat_ids") or [])]
                if cid in locs:
                    out.add(uid)
    return list(out)
