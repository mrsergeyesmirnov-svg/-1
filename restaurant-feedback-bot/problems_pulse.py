"""
Горящие вопросы (сигналы смен): пороги, статусы, дайджесты.
Хранение: PostgreSQL (если есть) или bot_data.json → ключ problems_store.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import db_pulse
import pulse_model
import report_pulse

# Синхронизация с отзывов за последние N дней (месяц для списка у менеджера)
SIGNALS_SYNC_DAYS = 30

# Пороги отметок за период синхронизации (кнопка problem_* в опросе)
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

VIEW_ACTIVE = "active"
VIEW_ARCHIVE = "archive"
VIEW_ALL = "all"

ACTIVE_STATUSES = frozenset({STATUS_NEW, STATUS_IN_PROGRESS})
ARCHIVE_STATUSES = frozenset({STATUS_RESOLVED, STATUS_IGNORED})

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
    view: str = VIEW_ACTIVE,
) -> list[ProblemRow]:
    pool = db_pulse.pool()
    if pool:
        rows = await db_pulse.fetch_problems_for_chat(chat_id, include_ignored=include_ignored)
        out = [_row_from_dict(r) for r in rows]
    else:
        out = []
        for d in _store_list(data):
            if int(d.get("restaurant_chat_id", 0)) != int(chat_id):
                continue
            if not include_ignored and d.get("status") == STATUS_IGNORED:
                continue
            out.append(_row_from_dict(d))

    if view == VIEW_ACTIVE:
        out = [p for p in out if p.status in ACTIVE_STATUSES]
    elif view == VIEW_ARCHIVE:
        out = [p for p in out if p.status in ARCHIVE_STATUSES]
    status_order = {
        STATUS_NEW: 0,
        STATUS_IN_PROGRESS: 1,
        STATUS_RESOLVED: 2,
        STATUS_IGNORED: 3,
    }

    def _sort_key(p: ProblemRow) -> tuple:
        fd = p.first_detected_at.timestamp() if p.first_detected_at else 0
        return (status_order.get(p.status, 9), fd)

    out.sort(key=_sort_key)
    return out


async def get_problem(data: dict[str, Any], problem_id: str) -> ProblemRow | None:
    pid_s = str(problem_id).strip()
    pool = db_pulse.pool()
    if pool:
        d = await db_pulse.fetch_problem_by_id(pid_s)
        if d:
            return _row_from_dict(d)
    for d in _store_list(data):
        if str(d.get("id")) == pid_s:
            return _row_from_dict(d)
    return None


def _closed_at_for_reopen(p: ProblemRow) -> datetime | None:
    """Момент закрытия темы — от него считаем новые отметки для возврата в активные."""
    if p.status == STATUS_RESOLVED:
        return p.resolved_at
    if p.status == STATUS_IGNORED:
        return p.updated_at
    return None


def _count_problems_in_events(
    events: list[report_pulse.EventRow],
    *,
    since: datetime | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        if e.event_type != report_pulse.EVENT_PROBLEM or not e.problem_code:
            continue
        if since is not None and e.created_at <= since:
            continue
        counts[e.problem_code] = counts.get(e.problem_code, 0) + 1
    return counts


def _looks_like_technical_title(title: str, problem_key: str) -> bool:
    t = (title or "").strip()
    if not t or t == problem_key:
        return True
    if problem_key.startswith("manual_") and t.startswith("manual_"):
        return True
    if problem_key.startswith("c_") and t == problem_key:
        return True
    return False


def resolve_problem_title_for_sync(
    problem_key: str,
    titles: dict[str, str],
    existing: ProblemRow | None,
) -> str:
    """Название для реестра: из кнопок опроса, не затирать ручные и уже сохранённые."""
    if problem_key in titles:
        return titles[problem_key]
    if problem_key in PROBLEM_TITLES:
        return PROBLEM_TITLES[problem_key]
    if existing:
        prev = (existing.title or "").strip()
        if prev and not _looks_like_technical_title(prev, problem_key):
            return prev
    return problem_key


async def sync_problem_titles_from_buttons(
    data: dict[str, Any],
    chat_id: int,
) -> int:
    """Подтянуть подписи кнопок опроса в названия тем (починка c_… / затираения)."""
    try:
        import survey_buttons

        titles = survey_buttons.titles_map(data, chat_id)
    except Exception:
        return 0
    if not titles:
        return 0
    rows = await list_problems_for_chat(data, chat_id, view=VIEW_ALL)
    pool = db_pulse.pool()
    fixed = 0
    for p in rows:
        if p.problem_key.startswith("manual_"):
            continue
        new_title = titles.get(p.problem_key) or PROBLEM_TITLES.get(p.problem_key)
        if not new_title or new_title == p.title:
            continue
        if not _looks_like_technical_title(p.title, p.problem_key):
            if p.problem_key not in titles:
                continue
        if pool:
            if await db_pulse.update_problem_title(p.id, new_title):
                fixed += 1
        else:
            for d in _store_list(data):
                if str(d.get("id")) == str(p.id):
                    d["title"] = new_title
                    fixed += 1
                    break
    return fixed


async def _upsert_local(
    data: dict[str, Any],
    *,
    chat_id: int,
    org_id: str | None,
    problem_key: str,
    title: str,
    count: int,
    now: datetime,
    threshold: int = 3,
    mentions_since_close: int = 0,
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
            prev_title = (d.get("title") or "").strip()
            if not _looks_like_technical_title(title, problem_key):
                d["title"] = title
            elif prev_title and not _looks_like_technical_title(prev_title, problem_key):
                d["title"] = prev_title
            else:
                d["title"] = title
            d["last_detected_at"] = now.isoformat()
            if prev_status in (STATUS_RESOLVED, STATUS_IGNORED):
                if mentions_since_close >= threshold:
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
    days: int = SIGNALS_SYNC_DAYS,
) -> list[tuple[ProblemRow, bool]]:
    """Создаёт/обновляет сигналы по порогам за период. (row, is_new)."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    end = datetime.now(tz)
    start = end - timedelta(days=days)
    events = await report_pulse.load_events(
        [chat_id], start, end, jsonl_path=jsonl_path
    )
    counts = _count_problems_in_events(events)

    now = datetime.now(tz)
    changes: list[tuple[ProblemRow, bool]] = []
    pool = db_pulse.pool()

    existing_rows = await list_problems_for_chat(data, chat_id, view=VIEW_ALL)
    existing_by_key = {p.problem_key: p for p in existing_rows}

    try:
        import survey_buttons

        thresholds = survey_buttons.all_thresholds_map(data, chat_id)
        titles = survey_buttons.titles_map(data, chat_id)
    except Exception:
        thresholds = dict(THRESHOLDS)
        titles = PROBLEM_TITLES

    existing_keys = set(existing_by_key.keys())
    all_keys = set(counts.keys()) | existing_keys | set(thresholds.keys())

    for key in all_keys:
        cnt = counts.get(key, 0)
        threshold = thresholds.get(key, THRESHOLDS.get(key, 3))
        exists = key in existing_keys
        if not exists and cnt < threshold:
            continue
        title = resolve_problem_title_for_sync(
            key, titles, existing_by_key.get(key)
        )
        mentions_since_close = 0
        prev = existing_by_key.get(key)
        if prev and prev.status in ARCHIVE_STATUSES:
            closed_at = _closed_at_for_reopen(prev)
            if closed_at:
                closed_at = report_pulse._align_tz(closed_at, now)
                since_counts = _count_problems_in_events(events, since=closed_at)
                mentions_since_close = since_counts.get(key, 0)
        if pool:
            row_d, created = await db_pulse.upsert_problem(
                restaurant_chat_id=chat_id,
                organization_id=org_id,
                problem_key=key,
                title=title,
                mentions_count=cnt,
                now=now,
                threshold=threshold,
                mentions_since_close=mentions_since_close,
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
                threshold=threshold,
                mentions_since_close=mentions_since_close,
            )
            changes.append((row, created))
    await sync_problem_titles_from_buttons(data, chat_id)
    return changes


async def create_manual_problem(
    data: dict[str, Any],
    chat_id: int,
    org_id: str | None,
    title: str,
    *,
    now: datetime | None = None,
) -> ProblemRow:
    """Своя тема вручную (не из опроса)."""
    title = title.strip()
    now = now or datetime.now().astimezone()
    problem_key = f"manual_{_new_local_id()[2:]}"
    pool = db_pulse.pool()
    if pool:
        d = await db_pulse.insert_manual_problem(
            restaurant_chat_id=chat_id,
            organization_id=org_id,
            problem_key=problem_key,
            title=title,
            now=now,
        )
        return _row_from_dict(d)
    store = _store_list(data)
    nid = _new_local_id()
    rec = {
        "id": nid,
        "organization_id": org_id,
        "restaurant_chat_id": chat_id,
        "problem_key": problem_key,
        "title": title,
        "source_type": "manual",
        "mentions_count": 0,
        "status": STATUS_NEW,
        "manager_comment": None,
        "first_detected_at": now.isoformat(),
        "last_detected_at": now.isoformat(),
        "resolved_at": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    store.append(rec)
    return _row_from_dict(rec)


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


def _signals_list_body(rows: list[ProblemRow], *, sync_days: int = SIGNALS_SYNC_DAYS) -> str:
    """Тело списка без заголовка (для вложения в отчёт менеджеру)."""
    if not rows:
        return (
            f"За последние <b>{sync_days} дн.</b> нет тем по порогам отметок в опросе.\n"
            "Нажмите «Обновить из отзывов»."
        )
    lines = [
        f"<i>Учёт отметок за {sync_days} дн. · дата — когда тема впервые всплыла</i>",
        "",
    ]
    for p in rows[:20]:
        em = STATUS_EMOJI.get(p.status, "🔴")
        st = STATUS_RU.get(p.status, p.status)
        fd = _fmt_date(p.first_detected_at)
        ld = _fmt_date(p.last_detected_at)
        lines.append(
            f"{em} <b>{escape(p.title)}</b> — <b>{p.mentions_count}</b> отметок\n"
            f"   с <b>{fd}</b> · обновлено {ld} · {escape(st)}"
        )
    if len(rows) > 20:
        lines.append(f"\n<i>… и ещё {len(rows) - 20}, откройте по кнопкам ниже</i>")
    return "\n".join(lines)


def _fmt_date(dt: datetime | None) -> str:
    if not dt:
        return "—"
    try:
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return "—"


def format_problem_list(
    rows: list[ProblemRow],
    *,
    title: str,
    chat_title: str | None = None,
    sync_days: int = SIGNALS_SYNC_DAYS,
    archive_hint: bool = True,
) -> str:
    header = escape(title)
    if chat_title:
        header = f"{header} — {escape(chat_title)}"
    if not rows:
        empty = (
            f"За последние <b>{sync_days} дн.</b> нет активных тем по порогам отметок в опросе.\n"
            "Нажмите «Обновить из отзывов» или добавьте «Свою тему»."
        )
        if archive_hint:
            empty += "\n\n📦 Решённые темы — в «Архиве»."
        return f"<b>{header}</b>\n\n{empty}"
    body = _signals_list_body(rows, sync_days=sync_days)
    return f"<b>{header}</b>\n\n{body}"


def format_archive_list(
    rows: list[ProblemRow],
    *,
    chat_title: str | None = None,
) -> str:
    header = "📦 Архив"
    if chat_title:
        header = f"{header} — {escape(chat_title)}"
    if not rows:
        return (
            f"<b>{header}</b>\n\n"
            "Пока пусто. Сюда попадают темы со статусом «Решена» или «Игнорируется»."
        )
    lines = [
        f"<b>{header}</b>",
        "<i>Можно открыть тему и вернуть в работу вручную. "
        "Из архива автоматически вернётся только если после закрытия снова набрался порог отметок.</i>",
        "",
    ]
    for p in rows[:25]:
        em = STATUS_EMOJI.get(p.status, "⚪")
        st = STATUS_RU.get(p.status, p.status)
        fd = _fmt_date(p.first_detected_at)
        rs = _fmt_date(p.resolved_at) if p.status == STATUS_RESOLVED else "—"
        lines.append(
            f"{em} <b>{escape(p.title)}</b> · {escape(st)}\n"
            f"   с {fd} · закрыто {rs} · {p.mentions_count} отметок"
        )
    if len(rows) > 25:
        lines.append(f"\n<i>… и ещё {len(rows) - 25}</i>")
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
    """Дайджест в группу смены: только прогресс (решено / в работе), без списка новых тем."""
    resolved = [p for p in rows if p.status == STATUS_RESOLVED]
    in_prog = [p for p in rows if p.status == STATUS_IN_PROGRESS]
    has_new = any(p.status == STATUS_NEW for p in rows)

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

    if resolved or in_prog:
        lines.append(
            "<i>Анонимные отклики → действия команды. Спасибо, что помогаете делать смены лучше.</i>"
        )
    elif has_new:
        lines.append(
            "На этой неделе пока нет обновлений для команды — управляющий разбирает сигналы смен.\n"
            "Продолжайте коротко отмечать смены: так мы быстрее замечаем, что улучшить."
        )
    else:
        lines.append(
            "За период не накопилось повторяющихся тем по порогам. "
            "Продолжайте коротко отмечать смены — так мы быстрее замечаем сложности."
        )
    return "\n".join(lines)


def format_manager_problems_report(
    chat_title: str,
    rows: list[ProblemRow],
    *,
    sync_notes: list[str] | None = None,
) -> str:
    lines = [
        f"🔥 <b>Горящие вопросы</b> — {escape(chat_title)}",
        "",
        _signals_list_body(rows, sync_days=SIGNALS_SYNC_DAYS),
    ]
    if sync_notes:
        lines.append("")
        lines.append("<b>Обновлено из отзывов</b>")
        for n in sync_notes[:8]:
            lines.append(f"• {escape(n)}")
    lines.append("")
    lines.append(
        "Кнопки ниже — открыть тему и сменить статус. "
        "Решённые — в «Архиве». "
        f"Команда: <code>/signals</code> или «{pulse_model.BTN_SIGNALS}»."
    )
    return "\n".join(lines)


def pr_callback(*parts: str) -> str:
    """callback_data для кнопок «Горящие вопросы» (лимит Telegram — 64 байта)."""
    data = "pr:" + ":".join(str(p) for p in parts)
    if len(data.encode("utf-8")) > 64:
        data = data[:64]
    return data


def parse_pr_callback(data: str) -> tuple[str, list[str]]:
    """pr:action:tail… → (action, [сегменты после action])."""
    if not data or not data.startswith("pr:"):
        return "", []
    rest = data[3:]
    if not rest:
        return "", []
    action, _, tail = rest.partition(":")
    if not tail:
        return action, []
    return action, tail.split(":")


def _looks_like_telegram_chat_id(s: str) -> bool:
    s = s.strip()
    if not s.lstrip("-").isdigit():
        return False
    try:
        n = int(s)
        return n < 0 or n > 1_000_000
    except ValueError:
        return False


def pr_problem_id_from_segments(segments: list[str]) -> str | None:
    """id темы: pr:v:5 или старый pr:v:-5180581902:5."""
    if not segments:
        return None
    if len(segments) >= 2 and _looks_like_telegram_chat_id(segments[0]):
        return segments[1]
    return segments[0]


def pr_status_change_from_segments(segments: list[str]) -> tuple[str | None, str | None]:
    """pr:w:pid:code или старый pr:w:-chat:pid:code."""
    if len(segments) >= 3 and _looks_like_telegram_chat_id(segments[0]):
        return segments[1], segments[2]
    if len(segments) >= 2:
        return segments[0], segments[1]
    return None, None


def parse_pr_chat_pick(data: str) -> int | None:
    """pr:c:-5180581902 → chat_id (без split по «:» внутри id)."""
    prefix = "pr:c:"
    if not data or not data.startswith(prefix):
        return None
    raw = data[len(prefix) :].strip()
    if not raw:
        return None
    head = raw.split(":", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def signals_location_keyboard(scope: list[tuple[str, str]]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [
            InlineKeyboardButton(
                text=f"📍 {title[:36]}",
                callback_data=pr_callback("c", cid),
            )
        ]
        for cid, title in scope[:15]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def problem_card_keyboard(problem_id: str, status: str, chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows = []
    if status != STATUS_IN_PROGRESS:
        rows.append(
            [
                InlineKeyboardButton(
                    text="В работу",
                    callback_data=pr_callback("w", problem_id, "ip"),
                )
            ]
        )
    if status != STATUS_RESOLVED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Решено",
                    callback_data=pr_callback("w", problem_id, "rs"),
                )
            ]
        )
    if status != STATUS_IGNORED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Игнорировать",
                    callback_data=pr_callback("w", problem_id, "ig"),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="← К списку", callback_data=pr_callback("l", cid))]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def problems_list_keyboard(
    rows: list[ProblemRow],
    *,
    archive_count: int = 0,
    chat_id: int,
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    ik = []
    for p in rows[:20]:
        em = STATUS_EMOJI.get(p.status, "🔴")
        fd = _fmt_date(p.first_detected_at)
        short = p.title[:22] if len(p.title) > 22 else p.title
        label = f"{em} {short} с {fd} ({p.mentions_count})"
        if len(label) > 64:
            label = label[:61] + "…"
        ik.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=pr_callback("v", p.id),
                )
            ]
        )
    ik.append(
        [InlineKeyboardButton(text="➕ Своя тема", callback_data=pr_callback("add", cid))]
    )
    ik.append(
        [
            InlineKeyboardButton(
                text="⚙️ Кнопки опроса",
                callback_data=f"pb:cfg:{cid}",
            )
        ]
    )
    arch_label = "📦 Архив"
    if archive_count:
        arch_label = f"📦 Архив ({archive_count})"
    ik.append([InlineKeyboardButton(text=arch_label, callback_data=pr_callback("arch", cid))])
    ik.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить из отзывов",
                callback_data=pr_callback("sync", cid),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=ik)


def problems_archive_keyboard(rows: list[ProblemRow], *, chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    ik = []
    for p in rows[:25]:
        em = STATUS_EMOJI.get(p.status, "⚪")
        short = p.title[:26] if len(p.title) > 26 else p.title
        ik.append(
            [
                InlineKeyboardButton(
                    text=f"{em} {short}",
                    callback_data=pr_callback("v", p.id),
                )
            ]
        )
    ik.append(
        [InlineKeyboardButton(text="← К активным", callback_data=pr_callback("l", cid))]
    )
    return InlineKeyboardMarkup(inline_keyboard=ik)


def comment_skip_keyboard(problem_id: str, chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пропустить комментарий",
                    callback_data=pr_callback("k", problem_id),
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
