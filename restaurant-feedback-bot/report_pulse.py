"""
Отчёты для менеджеров: период, метрики, проблемы, комментарии (без user_id).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import pulse_model

import db_pulse

PROBLEM_LABELS: dict[str, str] = {
    "kitchen": "Медленная кухня",
    "conflict": "Конфликт / напряжение",
    "staff": "Нехватка персонала",
    "management": "Плохая организация",
    "stress": "Сильная нагрузка",
    "comment": "Свой комментарий",
}

PERSONAL_FACTOR_LABELS: dict[str, str] = {
    "knowledge": "Не хватает знаний по процессам",
    "fatigue": "Усталость / плохое состояние",
    "time_mgmt": "Не успевал по времени",
    "communication": "Сложности в общении",
    "concentration": "Потеря концентрации",
}

EVENT_PROBLEM = "problem"
EVENT_PERSONAL = "personal_factor"
EVENT_RATING = "rating"
EVENT_COMMENT = "comment"

ACTION_HINTS = (
    "медлен", "очеред", "ждат", "конфликт", "ссор", "груб", "токсич",
    "нехват", "устал", "нагруз", "бардак", "хаос", "организац", "смен",
    "кухн", "бар", "менедж", "руковод", "зарплат", "мотивац",
)

PERIOD_SHIFT = "shift"
PERIOD_WEEK = "week"
PERIOD_MONTH = "month"
PERIOD_CALENDAR = "calendar_month"


class ReportResult(NamedTuple):
    """messages — цельные блоки для PDF; parts — нарезка под лимит Telegram."""

    messages: list[str]
    parts: list[str]
    events: list[EventRow]
    problem_labels: dict[str, str] | None

# «Месячный» отчёт — скользящее окно, не календарный месяц
MONTH_REPORT_DAYS = 21

PERIOD_TITLES = {
    PERIOD_SHIFT: "последняя смена (24 ч)",
    PERIOD_WEEK: "неделя",
    PERIOD_MONTH: "последние 3 недели",
    PERIOD_CALENDAR: "календарный месяц",
}

# Для блока тенденций (месячный отчёт)
WEEKDAY_PREP = (
    "в понедельник",
    "во вторник",
    "в среду",
    "в четверг",
    "в пятницу",
    "в субботу",
    "в воскресенье",
)
MIN_PROBLEM_MARKS_FOR_DOW = 3  # минимум отметок «что повлияло» за месяц


@dataclass
class EventRow:
    created_at: datetime
    event_type: str
    rating: int | None
    problem_code: str | None
    comment_text: str | None
    restaurant_chat_id: int | None
    restaurant_label: str | None
    department: str | None = None


def report_period_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🕐 Смена (24 ч)", callback_data="report_p:shift")],
            [InlineKeyboardButton(text="📅 Неделя", callback_data="report_p:week")],
            [InlineKeyboardButton(text="📆 3 недели", callback_data="report_p:month")],
            [
                InlineKeyboardButton(
                    text="🗓 Месяц (календарь)",
                    callback_data="report_p:calendar_month",
                )
            ],
        ]
    )


def report_calendar_month_keyboard(tz_name: str = "Europe/Moscow"):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    import monthly_ops

    rows = []
    for mk in monthly_ops.recent_month_keys(tz_name, count=4):
        label = monthly_ops.month_label_ru(mk)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📆 {label}",
                    callback_data=f"report_cal:{mk}"[:64],
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_inbox_locations_keyboard(scope: list[tuple[str, str]]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [
            InlineKeyboardButton(
                text=f"📍 {title[:36]}",
                callback_data=f"adm:loc:{cid}"[:64],
            )
        ]
        for cid, title in scope[:25]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def admin_inbox_point_keyboard(chat_id: int | str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Суточная сводка",
                    callback_data=f"adm:digest:{cid}"[:64],
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗓 Отчёт за месяц",
                    callback_data=f"adm:cal:{cid}"[:64],
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 3 недели",
                    callback_data=f"adm:rep3w:{cid}"[:64],
                )
            ],
            [
                InlineKeyboardButton(
                    text="📈 Смена / неделя",
                    callback_data=f"adm:repick:{cid}"[:64],
                )
            ],
        ]
    )


def monthly_plan_prompt_keyboard(chat_id: int | str, month_key: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Задать план на месяц",
                    callback_data=f"report_mplan:{month_key}:{chat_id}"[:64],
                )
            ]
        ]
    )


def report_location_keyboard(
    scope: list[tuple[str, str]],
    *,
    include_all: bool = False,
) -> Any:
    """Выбор точки перед периодом (для главного админа)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list] = []
    if include_all and len(scope) > 1:
        rows.append(
            [InlineKeyboardButton(text="📊 Все точки", callback_data="report_r:all")]
        )
    for cid, title in scope[:20]:
        label = title if len(title) <= 36 else title[:33] + "…"
        rows.append(
            [InlineKeyboardButton(text=f"📍 {label}", callback_data=f"report_r:{cid}")]
        )
    if len(scope) > 20:
        rows.append(
            [
                InlineKeyboardButton(
                    text="… остальные в /admin",
                    callback_data="report_r:__skip__",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def narrow_scope(
    scope: list[tuple[str, str]],
    selected: str | None,
) -> list[tuple[str, str]]:
    """selected: None — как есть; 'all' — все; иначе один chat_id."""
    if not selected or selected == "all":
        return scope
    return [(cid, title) for cid, title in scope if str(cid) == str(selected)]


def chat_scope_for_user(
    data: dict[str, Any],
    user_id: int,
    *,
    is_global_admin: bool,
) -> list[tuple[str, str]]:
    """(chat_id, title) доступные для отчёта."""
    chats = data.get("chats", {})
    allowed = _allowed_chat_ids(data, user_id, is_global_admin)
    out: list[tuple[str, str]] = []
    for cid in sorted(allowed, key=lambda x: int(x) if str(x).lstrip("-").isdigit() else x):
        rec = chats.get(cid)
        if isinstance(rec, dict) and rec.get("removed_at"):
            continue
        title = rec.get("title", f"Точка {cid}") if isinstance(rec, dict) else f"Точка {cid}"
        out.append((cid, str(title)))
    return out


def _allowed_chat_ids(data: dict[str, Any], user_id: int, is_global_admin: bool) -> set[str]:
    if is_global_admin:
        return {
            cid
            for cid, rec in data.get("chats", {}).items()
            if isinstance(rec, dict) and not rec.get("removed_at")
        }
    ids = pulse_model.allowed_chat_ids_for_manager(data, user_id)
    if ids:
        return ids
    return set()


def period_window(period: str, tz_name: str) -> tuple[datetime, datetime, datetime, datetime, str]:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    if period == PERIOD_SHIFT:
        end = now
        start = now - timedelta(hours=24)
        prev_end = start
        prev_start = start - timedelta(hours=24)
        label = PERIOD_TITLES[PERIOD_SHIFT]
    elif period == PERIOD_MONTH:
        end = now
        start = now - timedelta(days=MONTH_REPORT_DAYS)
        prev_end = start
        prev_start = start - timedelta(days=MONTH_REPORT_DAYS)
        label = PERIOD_TITLES[PERIOD_MONTH]
    else:
        end = now
        start = now - timedelta(days=7)
        prev_end = start
        prev_start = start - timedelta(days=7)
        label = PERIOD_TITLES[PERIOD_WEEK]
    return start, end, prev_start, prev_end, label


def _row_from_dict(d: dict[str, Any]) -> EventRow | None:
    ts_raw = d.get("created_at") or d.get("ts")
    if not ts_raw:
        return None
    if isinstance(ts_raw, datetime):
        created = ts_raw
    else:
        try:
            created = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            return None
    cid = d.get("restaurant_chat_id")
    if cid is not None:
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            cid = None
    rating = d.get("rating")
    if rating is not None:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            rating = None
    dept = d.get("department")
    return EventRow(
        created_at=created,
        event_type=str(d.get("event_type") or d.get("event") or ""),
        rating=rating,
        problem_code=d.get("problem_code") or d.get("problem"),
        comment_text=d.get("comment_text") or d.get("comment"),
        restaurant_chat_id=cid,
        restaurant_label=d.get("restaurant_label"),
        department=str(dept) if dept else None,
    )


def _event_department(row: EventRow) -> str:
    """legacy без поля = зал."""
    import chef_survey

    if row.department == chef_survey.DEPARTMENT_KITCHEN:
        return chef_survey.DEPARTMENT_KITCHEN
    if row.event_type == "chef_shift":
        return chef_survey.DEPARTMENT_KITCHEN
    return chef_survey.DEPARTMENT_FLOOR


def filter_events_by_department(
    events: list[EventRow], department: str | None
) -> list[EventRow]:
    import chef_survey

    if not department or department == chef_survey.DEPARTMENT_ALL:
        return events
    return [e for e in events if _event_department(e) == department]


def report_department_keyboard(chat_id: int | str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    import chef_survey

    cid = str(chat_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🍽 Зал",
                    callback_data=f"report_d:floor:{cid}"[:64],
                ),
                InlineKeyboardButton(
                    text="👨‍🍳 Кухня",
                    callback_data=f"report_d:kitchen:{cid}"[:64],
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Весь ресторан",
                    callback_data=f"report_d:all:{cid}"[:64],
                )
            ],
        ]
    )


def build_department_compare_html(
    current: list[EventRow],
    *,
    problem_labels: dict[str, str] | None = None,
) -> list[str]:
    import chef_survey

    def plab(c: str) -> str:
        if problem_labels and c in problem_labels:
            return problem_labels[c]
        if c in chef_survey.CHEF_SURVEY_LABELS:
            return chef_survey.CHEF_SURVEY_LABELS[c]
        return PROBLEM_LABELS.get(c, c)

    def _mark_stats(bucket: list[EventRow]) -> tuple[int, list[tuple[str, int]]]:
        marks: list[str] = []
        for e in bucket:
            if e.event_type == "chef_shift" and e.problem_code:
                marks.append(e.problem_code)
            elif e.event_type == EVENT_PROBLEM and e.problem_code:
                marks.append(e.problem_code)
        return len(marks), Counter(marks).most_common(3)

    floor = filter_events_by_department(current, chef_survey.DEPARTMENT_FLOOR)
    kitchen = filter_events_by_department(current, chef_survey.DEPARTMENT_KITCHEN)
    lines = ["<b>🏠 Зал и кухня</b>"]
    n_rat = _count_ratings(floor)
    avg = _avg_rating(floor)
    avg_s = f"{avg:.1f}" if avg is not None else "—"
    _, floor_top = _mark_stats(floor)
    ft = ""
    if floor_top:
        c, n = floor_top[0]
        ft = f" · чаще: {escape(plab(c))} ({n})"
    lines.append(
        f"<b>Зал:</b> оценок <b>{n_rat}</b> · ср. <b>{avg_s}</b> · "
        f"коммент. <b>{_count_text_comments(floor)}</b>{ft}"
    )
    k_n, k_top = _mark_stats(kitchen)
    kt = ""
    if k_top:
        c, n = k_top[0]
        kt = f" · чаще: {escape(plab(c))} ({n})"
    lines.append(f"<b>Кухня:</b> отметок смены <b>{k_n}</b>{kt}")
    lines.append("")
    return lines


def _event_dedupe_key(row: EventRow) -> tuple:
    ts = row.created_at.isoformat(timespec="seconds")
    return (
        ts,
        row.event_type,
        row.problem_code,
        row.rating,
        row.restaurant_chat_id,
        (row.comment_text or "")[:80],
    )


def _ensure_datetime(value: datetime | date, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value
    t = time(23, 59, 59) if end_of_day else time.min
    return datetime.combine(value, t)


def _align_tz(dt: datetime | date, ref: datetime | date) -> datetime:
    """Сравнение периода и событий в одной зоне (часто из JSONL без tz)."""
    dt = _ensure_datetime(dt)
    ref = _ensure_datetime(ref)
    if dt.tzinfo is None:
        tz = ref.tzinfo
        if tz is None and isinstance(ref, datetime):
            tz = None
        if tz is not None:
            return dt.replace(tzinfo=tz)
    return dt


async def load_events(
    chat_ids: list[int],
    start: datetime | date,
    end: datetime | date,
    *,
    jsonl_path: Path | None,
) -> list[EventRow]:
    """Postgres + JSONL: на Railway часть истории только в файле на Volume."""
    start = _ensure_datetime(start)
    end = _ensure_datetime(end, end_of_day=True)
    rows: list[EventRow] = []
    seen: set[tuple] = set()

    def _add(row: EventRow | None) -> None:
        if not row:
            return
        created = _align_tz(row.created_at, end)
        if created < start or created > end:
            return
        key = _event_dedupe_key(
            EventRow(
                created_at=created,
                event_type=row.event_type,
                rating=row.rating,
                problem_code=row.problem_code,
                comment_text=row.comment_text,
                restaurant_chat_id=row.restaurant_chat_id,
                restaurant_label=row.restaurant_label,
                department=row.department,
            )
        )
        if key in seen:
            return
        seen.add(key)
        rows.append(
            EventRow(
                created_at=created,
                event_type=row.event_type,
                rating=row.rating,
                problem_code=row.problem_code,
                comment_text=row.comment_text,
                restaurant_chat_id=row.restaurant_chat_id,
                restaurant_label=row.restaurant_label,
                department=row.department,
            )
        )

    pool = db_pulse.pool()
    if pool and chat_ids:
        try:
            async with pool.acquire() as conn:
                recs = await conn.fetch(
                    """
                    SELECT created_at, event_type, rating, problem_code, comment_text,
                           restaurant_chat_id, payload
                    FROM feedback_events
                    WHERE restaurant_chat_id = ANY($1::bigint[])
                      AND created_at >= $2 AND created_at <= $3
                    ORDER BY created_at DESC
                    """,
                    chat_ids,
                    start,
                    end,
                )
            for r in recs:
                payload = r["payload"] or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        payload = {}
                dept = payload.get("department") if isinstance(payload, dict) else None
                _add(
                    EventRow(
                        created_at=_ensure_datetime(r["created_at"]),
                        event_type=r["event_type"],
                        rating=r["rating"],
                        problem_code=r["problem_code"],
                        comment_text=r["comment_text"],
                        restaurant_chat_id=r["restaurant_chat_id"],
                        restaurant_label=None,
                        department=str(dept) if dept else None,
                    )
                )
        except Exception as e:
            print("[report load_events postgres]", repr(e))

    if jsonl_path and jsonl_path.exists():
        chat_set = {str(c) for c in chat_ids} if chat_ids else set()
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = d.get("restaurant_chat_id")
            if chat_set:
                if cid is None:
                    continue
                if str(cid) not in chat_set:
                    continue
            _add(_row_from_dict(d))
    return rows


def _avg_rating(events: list[EventRow]) -> float | None:
    vals = [e.rating for e in events if e.event_type == EVENT_RATING and e.rating is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _count_ratings(events: list[EventRow]) -> int:
    return sum(1 for e in events if e.event_type == EVENT_RATING)


def _count_text_comments(events: list[EventRow]) -> int:
    return sum(
        1
        for e in events
        if (e.comment_text or "").strip()
        and e.event_type in (EVENT_COMMENT, EVENT_PROBLEM, EVENT_PERSONAL)
    )


def _code_stats(events: list[EventRow], event_type: str) -> list[tuple[str, int]]:
    cnt: Counter[str] = Counter()
    for e in events:
        if e.event_type == event_type and e.problem_code:
            cnt[e.problem_code] += 1
    return cnt.most_common()


def _trend_line(current: float | None, previous: float | None) -> str:
    if current is None:
        return "Средняя оценка: <i>нет оценок за период</i>"
    cur_s = f"{current:.2f}".replace(".", ",")
    if previous is None:
        return f"Средняя оценка смен: <b>{cur_s}</b> ⭐"
    prev_s = f"{previous:.2f}".replace(".", ",")
    diff = current - previous
    if abs(diff) < 0.05:
        arrow = "→"
        mood = "без заметных изменений"
    elif diff > 0:
        arrow = "↑"
        mood = f"<b>рост</b> на {diff:+.2f}".replace(".", ",")
    else:
        arrow = "↓"
        mood = f"<b>просадка</b> на {diff:+.2f}".replace(".", ",")
    return (
        f"Средняя оценка: <b>{cur_s}</b> ⭐ (было {prev_s})\n"
        f"Динамика: {arrow} {mood}"
    )


def _label_for_code(
    code: str | None,
    event_type: str,
    *,
    problem_labels: dict[str, str] | None = None,
) -> str | None:
    if not code:
        return None
    if event_type == EVENT_PERSONAL:
        return PERSONAL_FACTOR_LABELS.get(code, code)
    if problem_labels and code in problem_labels:
        return problem_labels[code]
    return PROBLEM_LABELS.get(code, code)


def _comment_rows(events: list[EventRow]) -> list[EventRow]:
    return [e for e in events if (e.comment_text or "").strip()]


def _vivid_score(text: str) -> float:
    t = text.strip()
    low = t.lower()
    score = min(len(t) / 35.0, 5.0)
    if any(h in low for h in ACTION_HINTS):
        score += 2.5
    if "!" in t:
        score += 0.8
    if "?" in t:
        score += 0.3
    if any(w in low for w in ("очень", "ужас", "кошмар", "невозмож", "постоянн", "всегда", "никогда")):
        score += 1.2
    return score


def _comment_excerpt(text: str, *, max_len: int) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= max_len:
        return t
    cut = t.rfind(" ", 0, max_len - 1)
    if cut < 30:
        cut = max_len - 1
    return t[:cut].rstrip() + "…"


def _format_comments_block(
    comments: list[tuple[str, str | None, datetime | None]],
    *,
    total_count: int,
    period: str,
) -> list[str]:
    if not comments:
        return []
    lines: list[str] = []
    if period == PERIOD_SHIFT:
        lines.append("<b>Последние комментарии</b> <i>(анонимно)</i>")
        excerpt_len = 160
    else:
        lines.append("<b>Голос команды</b> <i>(выжимка, анонимно)</i>")
        excerpt_len = 110
    for text, plab_c, ts in comments:
        excerpt = _comment_excerpt(text, max_len=excerpt_len)
        ctx = f" — <i>{escape(plab_c)}</i>" if plab_c else ""
        when = ""
        if ts is not None and period == PERIOD_SHIFT:
            try:
                when = ts.strftime("%d.%m %H:%M") + " · "
            except Exception:
                when = ""
        lines.append(f"• {when}«{escape(excerpt)}»{ctx}")
    shown = len(comments)
    if total_count > shown and period != PERIOD_SHIFT:
        rest = total_count - shown
        lines.append(
            f"<i>Ещё {rest} {_comments_word(rest)} за период — полные тексты в отчёте в боте.</i>"
        )
    lines.append("")
    return lines


def _comments_word(n: int) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "комментарий"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "комментария"
    return "комментариев"


def _collect_comments_pdf(
    events: list[EventRow],
    *,
    period: str,
    scope_key: str,
    problem_labels: dict[str, str] | None = None,
    vivid_count: int = 4,
    medium_count: int = 2,
) -> list[tuple[str, str | None, datetime | None]]:
    """PDF: самые яркие + несколько средних по длине (стабильная «случайность»)."""
    rows = _comment_rows(events)
    if period == PERIOD_SHIFT:
        return _collect_comments(
            events, period, limit=5, problem_labels=problem_labels
        )
    if not rows:
        return []

    scored = sorted(
        rows,
        key=lambda e: (_vivid_score(e.comment_text or ""), len(e.comment_text or "")),
        reverse=True,
    )
    vivid = scored[:vivid_count]
    vivid_keys = {
        (e.created_at, (e.comment_text or "")[:80]) for e in vivid
    }
    medium_pool = [
        e
        for e in rows
        if (e.created_at, (e.comment_text or "")[:80]) not in vivid_keys
        and 35 <= len((e.comment_text or "").strip()) <= 140
    ]
    medium_pool.sort(
        key=lambda e: hashlib.md5(
            f"{scope_key}:{e.created_at}:{e.comment_text}".encode()
        ).hexdigest()
    )
    picked = vivid + medium_pool[:medium_count]

    out: list[tuple[str, str | None, datetime | None]] = []
    for e in picked:
        text = (e.comment_text or "").strip()
        plab = _label_for_code(
            e.problem_code, e.event_type, problem_labels=problem_labels
        )
        if not plab and e.event_type == EVENT_COMMENT:
            plab = "комментарий"
        out.append((text, plab, e.created_at))
    return out


def _format_comments_block_pdf(
    comments: list[tuple[str, str | None, datetime | None]],
    *,
    total_count: int,
    period: str,
) -> str:
    if not comments:
        return ""
    lines: list[str] = [
        "<b>Яркие комментарии</b> <i>(анонимно, полный текст)</i>",
    ]
    for text, plab_c, ts in comments:
        ctx = f" — <i>{escape(plab_c)}</i>" if plab_c else ""
        when = ""
        if ts is not None and period == PERIOD_SHIFT:
            try:
                when = ts.strftime("%d.%m %H:%M") + " · "
            except Exception:
                when = ""
        lines.append(f"• {when}«{escape(text)}»{ctx}")
    if total_count > len(comments) and period != PERIOD_SHIFT:
        rest = total_count - len(comments)
        lines.append(
            f"<i>Ещё {rest} {_comments_word(rest)} за период не вошли в PDF.</i>"
        )
    return "\n".join(lines)


def _collect_comments(
    events: list[EventRow],
    period: str,
    *,
    limit: int = 5,
    problem_labels: dict[str, str] | None = None,
) -> list[tuple[str, str | None, datetime | None]]:
    """(text, context_label, created_at) — без user_id."""
    rows = _comment_rows(events)
    if period == PERIOD_SHIFT:
        rows.sort(key=lambda e: e.created_at, reverse=True)
        picked = rows[:limit]
    else:
        rows.sort(
            key=lambda e: (_vivid_score(e.comment_text or ""), len(e.comment_text or "")),
            reverse=True,
        )
        picked = rows[:limit]

    out: list[tuple[str, str | None, datetime | None]] = []
    for e in picked:
        text = (e.comment_text or "").strip()
        plab = _label_for_code(
            e.problem_code, e.event_type, problem_labels=problem_labels
        )
        if not plab and e.event_type == EVENT_COMMENT:
            plab = "комментарий"
        out.append((text, plab, e.created_at))
    return out


@dataclass
class AnalyticalInsight:
    title: str
    factors: list[tuple[str, int]]
    recommendation: str
    priority: int  # lower = more important


def _counts_map(stats: list[tuple[str, int]]) -> dict[str, int]:
    return {code: n for code, n in stats}


def build_analytical_insights(
    problems: list[tuple[str, int]],
    personal: list[tuple[str, int]],
    *,
    avg_current: float | None,
    avg_previous: float | None,
    n_ratings: int,
) -> list[AnalyticalInsight]:
    """Рекомендации по сочетаниям кнопок (экспертные правила)."""
    p = _counts_map(problems)
    f = _counts_map(personal)
    insights: list[AnalyticalInsight] = []

    def g(code: str, bucket: dict[str, int]) -> int:
        return bucket.get(code, 0)

    rating_drop = (
        avg_current is not None
        and avg_previous is not None
        and n_ratings >= 3
        and (avg_previous - avg_current) >= 0.25
    )

    # Правило 1: перегруз кухни
    if g("kitchen", p) >= 5 and g("staff", p) >= 5 and g("stress", p) >= 5:
        insights.append(
            AnalyticalInsight(
                title="Перегрузка кухни в часы пиковой нагрузки",
                factors=[
                    ("kitchen", g("kitchen", p)),
                    ("staff", g("staff", p)),
                    ("stress", g("stress", p)),
                ],
                recommendation=(
                    "Проверьте график кухни и укомплектованность смены; "
                    "зафиксируйте время отдачи горячих блюд и очередь на раздаче."
                ),
                priority=1,
            )
        )

    # Правило 2: организация процессов зал ↔ кухня
    if (
        g("kitchen", p) >= 5
        and g("communication", f) >= 3
        and g("management", p) >= 3
    ):
        insights.append(
            AnalyticalInsight(
                title=(
                    "Задержки связаны с организацией процессов, "
                    "не только с нехваткой персонала"
                ),
                factors=[
                    ("kitchen", g("kitchen", p)),
                    ("communication", g("communication", f)),
                    ("management", g("management", p)),
                ],
                recommendation=(
                    "Разберите передачу заказов зал ↔ кухня: приоритеты, зоны, "
                    "кто подтверждает готовность блюд."
                ),
                priority=2,
            )
        )

    # Правило 3: усталость и напряжение
    if g("fatigue", f) >= 8 and g("stress", p) >= 5 and rating_drop:
        insights.append(
            AnalyticalInsight(
                title="Признаки усталости и напряжения в команде",
                factors=[
                    ("fatigue", g("fatigue", f)),
                    ("stress", g("stress", p)),
                ],
                recommendation=(
                    "Проверьте переработки и плотность графика; "
                    "обсудите ситуацию с ответственными за смену без персональных оценок."
                ),
                priority=1,
            )
        )

    # Правило 4: конфликт + коммуникация
    if g("conflict", p) >= 4 and g("communication", f) >= 4:
        insights.append(
            AnalyticalInsight(
                title="Напряжение в коммуникации на смене",
                factors=[
                    ("conflict", g("conflict", p)),
                    ("communication", g("communication", f)),
                ],
                recommendation=(
                    "Разберите типичные ситуации (бар, зал, кухня); "
                    "зафиксируйте правила общения в часы пиковой нагрузки."
                ),
                priority=2,
            )
        )

    # Правило 5: организация + тайм-менеджмент
    if g("management", p) >= 4 and g("time_mgmt", f) >= 4:
        insights.append(
            AnalyticalInsight(
                title="Сбои в организации смены и распределении времени",
                factors=[
                    ("management", g("management", p)),
                    ("time_mgmt", g("time_mgmt", f)),
                ],
                recommendation=(
                    "Пройдите чек-лист открытия и закрытия смены; "
                    "распределите зоны ответственности в начале смены."
                ),
                priority=3,
            )
        )

    # Правило 6: нехватка знаний
    if g("knowledge", f) >= 6 and g("kitchen", p) >= 3:
        insights.append(
            AnalyticalInsight(
                title="Пробел в знаниях процессов на линии",
                factors=[
                    ("knowledge", g("knowledge", f)),
                    ("kitchen", g("kitchen", p)),
                ],
                recommendation=(
                    "Краткий инструктаж по частым ошибкам смены; "
                    "закрепите ответственных по каждому процессу."
                ),
                priority=3,
            )
        )

    # Правило 7: нехватка персонала
    if g("staff", p) >= 6 and g("kitchen", p) < 4 and g("stress", p) >= 4:
        insights.append(
            AnalyticalInsight(
                title="Нехватка персонала без выраженной перегрузки кухни",
                factors=[("staff", g("staff", p)), ("stress", g("stress", p))],
                recommendation=(
                    "Сверьте численность с планом загрузки; "
                    "рассмотрите усиление смены в пик или перераспределение зон."
                ),
                priority=2,
            )
        )

    # Правило 8: высокая нагрузка при снижении оценки
    if g("stress", p) >= 6 and rating_drop and n_ratings >= 5:
        if not any("усталости" in i.title for i in insights):
            insights.append(
                AnalyticalInsight(
                    title="Падает оценка смен на фоне высокой нагрузки",
                    factors=[("stress", g("stress", p))],
                    recommendation=(
                        "Разберите самый напряжённый день недели; "
                        "оцените, что можно упростить в меню или процессах."
                    ),
                    priority=2,
                )
            )

    insights.sort(key=lambda x: (x.priority, -sum(n for _, n in x.factors)))
    return insights[:3]


def _short_period_range(period: str, start: datetime, end: datetime) -> str:
    if period == PERIOD_SHIFT:
        return f"{start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}"
    return f"{start.strftime('%d.%m')} — {end.strftime('%d.%m')}"


def _html_blockquote(lines: list[str]) -> str:
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def _format_insights_message(
    insights: list[AnalyticalInsight],
    *,
    period: str,
    scope_title: str,
    problem_labels: dict[str, str] | None = None,
) -> str:
    def factor_label(code: str) -> str:
        if problem_labels and code in problem_labels:
            return problem_labels[code]
        if code in PROBLEM_LABELS:
            return PROBLEM_LABELS[code]
        return PERSONAL_FACTOR_LABELS.get(code, code)

    period_short = PERIOD_TITLES.get(period, period)
    blocks: list[str] = []
    for i, ins in enumerate(insights, 1):
        block_lines = [f"<b>{i}. {escape(ins.title)}</b>", ""]
        if ins.factors:
            block_lines.append("<i>Факторы:</i>")
            for code, n in ins.factors:
                block_lines.append(f"• {escape(factor_label(code))} — <b>{n}</b>")
            block_lines.append("")
        block_lines.append("<i>Рекомендация:</i>")
        block_lines.append(escape(ins.recommendation))
        blocks.append(_html_blockquote(block_lines))
    header = (
        f"<b>🧠 Готовые выводы</b>\n"
        f"<i>{escape(period_short)} · {escape(scope_title)}</i>"
    )
    return header + "\n\n" + "\n\n".join(blocks)


def _event_weekday(created_at: datetime) -> int:
    """0 = понедельник (как datetime.weekday())."""
    if created_at.tzinfo is not None:
        return created_at.weekday()
    return created_at.weekday()


def build_monthly_weekday_trends_html(
    events: list[EventRow],
    *,
    problem_labels: dict[str, str] | None = None,
) -> list[str]:
    """Темы «что повлияло» по дням недели — только месячный отчёт."""

    def plab(c: str) -> str:
        if problem_labels and c in problem_labels:
            return problem_labels[c]
        return PROBLEM_LABELS.get(c, c)

    problems_by_dow: list[Counter[str]] = [Counter() for _ in range(7)]

    for e in events:
        if e.event_type == EVENT_PROBLEM and e.problem_code:
            dow = _event_weekday(e.created_at)
            problems_by_dow[dow][e.problem_code] += 1

    month_problems: Counter[str] = Counter()
    for c in problems_by_dow:
        month_problems.update(c)
    if sum(month_problems.values()) < MIN_PROBLEM_MARKS_FOR_DOW:
        return []

    bullets: list[str] = []
    for code, _total in month_problems.most_common(3):
        per_day = [(d, problems_by_dow[d].get(code, 0)) for d in range(7)]
        per_day = [(d, n) for d, n in per_day if n > 0]
        if not per_day:
            continue
        per_day.sort(key=lambda x: x[1], reverse=True)
        d_peak, n_peak = per_day[0]
        if n_peak < 2:
            continue
        label = escape(plab(code))
        if len(per_day) >= 2 and per_day[1][1] >= 2:
            d2, n2 = per_day[1]
            bullets.append(
                f"• {label} — чаще {WEEKDAY_PREP[d_peak]}, "
                f"также {WEEKDAY_PREP[d2]}"
            )
        else:
            bullets.append(f"• {label} — чаще {WEEKDAY_PREP[d_peak]}")

    if not bullets:
        return []

    return [
        "<b>Темы по дням недели</b> <i>(3 нед.)</i>",
        "",
        *bullets,
        "",
    ]


def _fallback_tip(
    problems: list[tuple[str, int]],
    personal: list[tuple[str, int]],
    *,
    problem_labels: dict[str, str] | None = None,
) -> str | None:
    def plab(c: str) -> str:
        if problem_labels and c in problem_labels:
            return problem_labels[c]
        return PROBLEM_LABELS.get(c, c)

    if problems:
        code, n = problems[0]
        return (
            f"Чаще всего отмечали «{plab(code)}» ({n}). "
            f"В «Горящих вопросах» можно взять тему в работу и сообщить команде."
        )
    if personal:
        code, n = personal[0]
        return (
            f"Частый личный фактор: «{PERSONAL_FACTOR_LABELS.get(code, code)}» ({n}). "
            "Имеет смысл обсудить на планёрке без персональных имён."
        )
    return None


def _chef_shift_stats(events: list[EventRow]) -> tuple[int, list[tuple[str, int]]]:
    marks: list[str] = []
    for e in events:
        if e.event_type == "chef_shift" and e.problem_code:
            marks.append(e.problem_code)
    return len(marks), Counter(marks).most_common()


def build_report_html(
    *,
    period: str,
    period_label: str,
    scope_title: str,
    start: datetime,
    end: datetime,
    current: list[EventRow],
    previous: list[EventRow],
    problem_labels: dict[str, str] | None = None,
    engagement_line: str | None = None,
    problem_stats_line: str | None = None,
    ops_summary_line: str | None = None,
    shift_discipline_line: str | None = None,
    departures_line: str | None = None,
    stop_list_line: str | None = None,
    extra_lines: list[str] | None = None,
    department: str | None = None,
) -> list[str]:
    import chef_survey

    def plab(c: str) -> str:
        if problem_labels and c in problem_labels:
            return problem_labels[c]
        if c in chef_survey.CHEF_SURVEY_LABELS:
            return chef_survey.CHEF_SURVEY_LABELS[c]
        return PROBLEM_LABELS.get(c, c)

    kitchen_mode = department == chef_survey.DEPARTMENT_KITCHEN
    n_ratings = _count_ratings(current)
    n_comments = _count_text_comments(current)
    if kitchen_mode:
        n_comments = sum(
            1
            for e in current
            if e.event_type == "chef_shift" and (e.comment_text or "").strip()
        )
    avg_c = _avg_rating(current)
    avg_p = _avg_rating(previous)
    if kitchen_mode:
        n_marks, problems = _chef_shift_stats(current)
        _, prev_marks = _chef_shift_stats(previous)
        personal: list[tuple[str, int]] = []
    else:
        n_marks = n_ratings
        problems = _code_stats(current, EVENT_PROBLEM)
        personal = _code_stats(current, EVENT_PERSONAL)
    comment_limit = 4 if period == PERIOD_SHIFT else 3
    comments = _collect_comments(
        current, period, limit=comment_limit, problem_labels=problem_labels
    )
    insights = build_analytical_insights(
        problems,
        personal,
        avg_current=avg_c,
        avg_previous=avg_p,
        n_ratings=n_ratings,
    )

    period_short = PERIOD_TITLES.get(period, period_label)
    lines = [
        "📊 <b>Сводка Pulse Team</b>",
        "",
        f"<b>{escape(scope_title)}</b>",
        f"<i>Период: {escape(period_short)} · {_short_period_range(period, start, end)}</i>",
        "",
    ]
    if extra_lines:
        lines.extend(extra_lines)
    if kitchen_mode:
        lines.append(f"Отметок смены кухни: <b>{n_marks}</b> · комментариев: <b>{n_comments}</b>")
        lines.append("")
    else:
        lines.append(_trend_line(avg_c, avg_p))
        lines.append(f"Оценок смен: <b>{n_ratings}</b> · комментариев: <b>{n_comments}</b>")
        lines.append("")
    if engagement_line:
        lines.append(engagement_line)
        lines.append("")
    if problem_stats_line:
        lines.append(problem_stats_line)
        lines.append("")
    if ops_summary_line:
        lines.append(ops_summary_line)
        lines.append("")
    if shift_discipline_line:
        lines.append(shift_discipline_line)
        lines.append("")
    if departures_line:
        lines.append(departures_line)
        lines.append("")
    if stop_list_line:
        lines.append(stop_list_line)
        lines.append("")

    if problems:
        title = (
            "<b>Что было сложным на кухне</b>"
            if kitchen_mode
            else "<b>Что мешало на смене</b>"
        )
        lines.append(title)
        for code, n in problems[:5]:
            lines.append(f"• {escape(plab(code))} — <b>{n}</b>")
        lines.append("")
    elif kitchen_mode:
        lines.append("<i>Отметок смены кухни за период нет.</i>\n")
    else:
        lines.append("<i>Темы «что мешало на смене» не выбирали или только оценки.</i>\n")

    if personal and not insights:
        personal_lines = ["<b>Самочувствие и нагрузка</b>"]
        for code, n in personal[:5]:
            label = PERSONAL_FACTOR_LABELS.get(code, code)
            personal_lines.append(f"• {escape(label)} — <b>{n}</b>")
        lines.append(_html_blockquote(personal_lines))
        lines.append("")

    if period in (PERIOD_MONTH, PERIOD_CALENDAR):
        dow_lines = build_monthly_weekday_trends_html(
            current, problem_labels=problem_labels
        )
        if dow_lines:
            lines.extend(dow_lines)

    messages: list[str] = ["\n".join(lines).rstrip()]

    if insights:
        messages.append(
            _format_insights_message(
                insights,
                period=period,
                scope_title=scope_title,
                problem_labels=problem_labels,
            )
        )
    else:
        fb = _fallback_tip(problems, personal, problem_labels=problem_labels)
        if fb:
            extra = messages[0] + "\n\n<b>На что обратить внимание</b>\n🔸 " + escape(fb)
            messages[0] = extra

    if comments:
        comment_block = _format_comments_block(
            comments, total_count=n_comments, period=period
        )
        if comment_block:
            messages.append("\n".join(comment_block).rstrip())
    elif period == PERIOD_SHIFT:
        messages[0] += "\n\n<i>Текстовых комментариев за смену нет.</i>"

    return messages


def split_telegram_html(text: str, limit: int = 3800) -> list[str]:
    """Не режем «Готовые выводы» посередине — иначе дублируются блоки в PDF."""
    if "Готовые выводы" in text or "🧠" in text:
        limit = 4090
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        chunk = line + "\n"
        if size + len(chunk) > limit and buf:
            parts.append("".join(buf).rstrip())
            buf = []
            size = 0
        buf.append(chunk)
        size += len(chunk)
    if buf:
        parts.append("".join(buf).rstrip())
    return parts or [text[:limit]]


async def build_reports_for_manager(
    data: dict[str, Any],
    user_id: int,
    period: str,
    *,
    is_global_admin: bool,
    tz_name: str,
    jsonl_path: Path | None,
    selected_chat: str | None = None,
    department: str | None = None,
) -> ReportResult:
    full_scope = chat_scope_for_user(data, user_id, is_global_admin=is_global_admin)
    scope = narrow_scope(full_scope, selected_chat)
    if not scope:
        msg = (
            "Нет привязанных точек для отчёта. "
            "Обратитесь к администратору Pulse или выполните <code>/link_manager</code>."
        )
        return ReportResult([msg], [msg], [], None)

    tz = tz_name
    if scope:
        rec = data.get("chats", {}).get(scope[0][0])
        if isinstance(rec, dict) and rec.get("timezone"):
            tz = str(rec["timezone"])
    start, end, prev_start, prev_end, plabel = period_window(period, tz)
    chat_ids = [int(cid) for cid, _ in scope]
    import chef_survey

    try:
        import survey_buttons

        problem_labels = survey_buttons.merge_labels_for_chats(data, chat_ids)
        problem_labels.update(chef_survey.merge_labels_for_chats(data, chat_ids))
    except Exception:
        problem_labels = chef_survey.merge_labels_for_chats(data, chat_ids)
    current_raw = await load_events(chat_ids, start, end, jsonl_path=jsonl_path)
    previous_raw = await load_events(
        chat_ids, prev_start, prev_end, jsonl_path=jsonl_path
    )
    dept = department or chef_survey.DEPARTMENT_FLOOR
    if dept == chef_survey.DEPARTMENT_ALL:
        current = current_raw
        previous = previous_raw
    else:
        current = filter_events_by_department(current_raw, dept)
        previous = filter_events_by_department(previous_raw, dept)

    if not current and not (dept == chef_survey.DEPARTMENT_ALL and current_raw):
        if dept == chef_survey.DEPARTMENT_KITCHEN:
            hint = (
                "Попросите шефа нажать «Оценить смену» в личке бота "
                "(напоминания приходят утром и вечером)."
            )
        else:
            hint = (
                "Попросите команду ответить из рабочей группы "
                "(кнопка «Рассказать в личке»)."
            )
        msg = f"За период «{escape(plabel)}» ответов пока нет.\n\n{hint}"
        return ReportResult([msg], [msg], [], None)

    import admin_metrics
    import ops_day

    baseline_map = await admin_metrics.count_unique_users_by_chat(
        chat_ids, jsonl_path=jsonl_path, days=30
    )
    raters_map = await admin_metrics.count_raters_in_period(
        chat_ids, start, end, jsonl_path=jsonl_path
    )
    dep_events = await ops_day.load_departure_events(jsonl_path, 0)

    multi = len(scope) > 1 and (selected_chat in (None, "all"))
    if not multi:
        cid, title = scope[0]
        cid_i = int(cid)
        dept_title = chef_survey.department_title(dept)
        scope_title = f"{title} · {dept_title}"
        rec = data.get("chats", {}).get(cid, {})
        day = ops_day.today_key(
            str(rec.get("timezone", tz)) if isinstance(rec, dict) else tz
        )
        evening_day = ops_day.shift_day_for_evening(
            str(rec.get("timezone", tz)) if isinstance(rec, dict) else tz,
            ops_day.ops_schedule(rec)["morning_post"] if isinstance(rec, dict) else ops_day.DEFAULT_MORNING_POST,
        )
        morning = ops_day.get_morning(rec, day) if isinstance(rec, dict) else {}
        evening = ops_day.get_evening(rec, evening_day) if isinstance(rec, dict) else {}
        ops_bits: list[str] = []
        if ops_day.morning_complete(morning):
            st = "опубликован" if morning.get("posted") else "черновик"
            rp = ops_day._morning_revenue_plan(morning)
            ops_bits.append(
                f"План: {st} · {ops_day.format_money(rp)} · {morning.get('shift_headcount', '—')} чел."
            )
        if ops_day.evening_complete(evening):
            met = ops_day.PLAN_MET_RU.get(str(evening.get("plan_met")), "—")
            pct = evening.get("revenue_pct")
            pct_s = f" {pct}%" if pct is not None else ""
            ops_bits.append(f"Закрытие: {escape(met)}{pct_s}")
        ops_line = (
            "<b>Операционный день</b>\n" + " · ".join(ops_bits)
            if ops_bits
            else None
        )
        tz_loc = str(rec.get("timezone", tz)) if isinstance(rec, dict) else tz
        uo, uc, td = ops_day.shift_discipline_counts(
            rec if isinstance(rec, dict) else {}, start, end, tz_loc
        )
        shift_line = ops_day.format_shift_discipline_line(uo, uc, td)
        pstats = await ops_day.problem_stats_summary(data, cid_i)
        dep_n = ops_day.count_staff_departures(dep_events, cid_i, days=30)
        if period != PERIOD_SHIFT:
            dep_n = ops_day.count_staff_departures(
                dep_events,
                cid_i,
                days=21 if period == PERIOD_MONTH else 7,
            )
        eng_line = await ops_day.engagement_line_for_period(
            data, cid_i, start, end, jsonl_path=jsonl_path
        )
        if not eng_line:
            eng_line = admin_metrics.format_engagement_line(
                raters_map.get(cid, 0),
                baseline_map.get(cid, 0),
                period_label=plabel,
            )
        stop_line = None
        if dept in (
            chef_survey.DEPARTMENT_ALL,
            chef_survey.DEPARTMENT_KITCHEN,
        ):
            stop_line = ops_day.stop_list_line_for_period(
                rec if isinstance(rec, dict) else {},
                start,
                end,
                tz_loc,
                show_empty=(dept == chef_survey.DEPARTMENT_KITCHEN),
            )
        cur_loc = [
            e
            for e in current
            if e.restaurant_chat_id == cid_i or e.restaurant_chat_id is None
        ]
        prev_loc = [
            e
            for e in previous
            if e.restaurant_chat_id == cid_i or e.restaurant_chat_id is None
        ]
        compare_lines = (
            build_department_compare_html(
                [
                    e
                    for e in current_raw
                    if e.restaurant_chat_id == cid_i or e.restaurant_chat_id is None
                ],
                problem_labels=problem_labels,
            )
            if dept == chef_survey.DEPARTMENT_ALL
            else None
        )
        report_msgs = build_report_html(
            period=period,
            period_label=plabel,
            scope_title=scope_title,
            start=start,
            end=end,
            current=cur_loc,
            previous=prev_loc,
            problem_labels=problem_labels,
            engagement_line=eng_line if dept != chef_survey.DEPARTMENT_KITCHEN else None,
            problem_stats_line=ops_day.format_problem_stats_block(pstats),
            ops_summary_line=ops_line if dept != chef_survey.DEPARTMENT_KITCHEN else None,
            shift_discipline_line=(
                shift_line if dept != chef_survey.DEPARTMENT_KITCHEN else None
            ),
            departures_line=(
                f"<b>Уходы кадра:</b> {dep_n} за период"
                if dep_n and dept != chef_survey.DEPARTMENT_KITCHEN
                else None
            ),
            stop_list_line=stop_line,
            extra_lines=compare_lines,
            department=dept,
        )
        messages: list[str] = list(report_msgs)
        parts: list[str] = []
        for msg in report_msgs:
            parts.extend(split_telegram_html(msg))
        return ReportResult(messages, parts, cur_loc, problem_labels)

    messages: list[str] = []
    parts: list[str] = []
    merged_events: list[EventRow] = []
    for cid, title in scope:
        cid_i = int(cid)
        cur = [e for e in current if e.restaurant_chat_id == cid_i]
        if not cur:
            continue
        prev = [e for e in previous if e.restaurant_chat_id == cid_i]
        try:
            import survey_buttons

            plab_map = survey_buttons.merge_labels_for_chats(data, [cid_i])
            plab_map.update(chef_survey.merge_labels_for_chats(data, [cid_i]))
        except Exception:
            plab_map = problem_labels
        dept_title = chef_survey.department_title(dept)
        compare_lines = (
            build_department_compare_html(
                [e for e in current_raw if e.restaurant_chat_id == cid_i],
                problem_labels=plab_map,
            )
            if dept == chef_survey.DEPARTMENT_ALL
            else None
        )
        rec_m = data.get("chats", {}).get(cid, {})
        tz_loc_m = str(rec_m.get("timezone", tz)) if isinstance(rec_m, dict) else tz
        stop_line = None
        if dept in (
            chef_survey.DEPARTMENT_ALL,
            chef_survey.DEPARTMENT_KITCHEN,
        ):
            stop_line = ops_day.stop_list_line_for_period(
                rec_m if isinstance(rec_m, dict) else {},
                start,
                end,
                tz_loc_m,
                show_empty=(dept == chef_survey.DEPARTMENT_KITCHEN),
            )
        report_msgs = build_report_html(
            period=period,
            period_label=plabel,
            scope_title=f"{title} · {dept_title}",
            start=start,
            end=end,
            current=cur,
            previous=prev,
            problem_labels=plab_map,
            stop_list_line=stop_line,
            extra_lines=compare_lines,
            department=dept,
        )
        for msg in report_msgs:
            messages.append(msg)
            parts.extend(split_telegram_html(msg))
        merged_events.extend(cur)
    if not parts:
        msg = f"За период «{escape(plabel)}» по вашим точкам нет данных."
        return ReportResult([msg], [msg], [], None)
    return ReportResult(messages, parts, merged_events, problem_labels)


def build_pdf_comments_html(
    events: list[EventRow],
    *,
    period: str,
    scope_key: str,
    problem_labels: dict[str, str] | None = None,
) -> str:
    n_comments = _count_text_comments(events)
    comments = _collect_comments_pdf(
        events,
        period=period,
        scope_key=scope_key,
        problem_labels=problem_labels,
    )
    return _format_comments_block_pdf(
        comments, total_count=n_comments, period=period
    )


def report_pdf_export_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📄 Выгрузить PDF",
                    callback_data="report_pdf:export",
                )
            ]
        ]
    )


async def build_calendar_month_reports(
    data: dict[str, Any],
    user_id: int,
    month_key: str,
    *,
    is_global_admin: bool,
    tz_name: str,
    jsonl_path: Path | None,
    selected_chat: str | None = None,
) -> tuple[ReportResult, Any | None]:
    """Подробный отчёт за календарный месяц."""
    import admin_metrics
    import monthly_ops
    import ops_day

    full_scope = chat_scope_for_user(data, user_id, is_global_admin=is_global_admin)
    scope = narrow_scope(full_scope, selected_chat)
    if not scope or len(scope) != 1:
        msg = (
            "Календарный месяц строится <b>по одной точке</b>.\n"
            "Выберите конкретную локацию в «Отчёт» или «Сводки»."
        )
        return ReportResult([msg], [msg], [], None), None
    cid, title = scope[0]
    cid_i = int(cid)
    rec = data.get("chats", {}).get(cid, {})
    if isinstance(rec, dict) and rec.get("timezone"):
        tz = str(rec["timezone"])
    else:
        tz = tz_name

    try:
        start, end, prev_start, prev_end, plabel = monthly_ops.calendar_month_window(
            month_key, tz
        )
    except ValueError:
        msg = "Некорректный месяц."
        return ReportResult([msg], [msg], [], None), None

    stats = monthly_ops.aggregate_calendar_month(
        rec if isinstance(rec, dict) else {}, month_key, tz
    )
    chat_ids = [cid_i]
    try:
        import survey_buttons

        problem_labels = survey_buttons.merge_labels_for_chats(data, chat_ids)
    except Exception:
        problem_labels = None

    current = await load_events(chat_ids, start, end, jsonl_path=jsonl_path)
    previous = await load_events(
        chat_ids, prev_start, prev_end, jsonl_path=jsonl_path
    )
    cur = [e for e in current if e.restaurant_chat_id == cid_i or e.restaurant_chat_id is None]
    prev = [e for e in previous if e.restaurant_chat_id == cid_i or e.restaurant_chat_id is None]

    if not cur and not stats.get("days_with_close"):
        empty_kb = None
        if not monthly_ops.monthly_plan_amount(
            rec if isinstance(rec, dict) else {}, month_key
        ):
            empty_kb = monthly_plan_prompt_keyboard(cid, month_key)
        return (
            ReportResult(
                [
                    f"За <b>{escape(plabel)}</b> по точке «{escape(title)}» "
                    "нет ни ответов команды, ни закрытых смен.\n\n"
                    "Когда появятся данные — отчёт заполнится автоматически."
                ],
                [
                    f"За <b>{escape(plabel)}</b> по точке «{escape(title)}» "
                    "нет ни ответов команды, ни закрытых смен.\n\n"
                    "Когда появятся данные — отчёт заполнится автоматически."
                ],
                [],
                problem_labels,
            ),
            empty_kb,
        )

    raters_map = await admin_metrics.count_raters_in_period(
        chat_ids, start, end, jsonl_path=jsonl_path
    )
    dep_events = await ops_day.load_departure_events(jsonl_path, 0)
    dep_n = ops_day.count_staff_departures(dep_events, cid_i, days=31)
    pstats = await ops_day.problem_stats_summary(data, cid_i)
    eng_line = await ops_day.engagement_line_for_period(
        data, cid_i, start, end, jsonl_path=jsonl_path
    )
    if not eng_line:
        baseline_map = await admin_metrics.count_unique_users_by_chat(
            chat_ids, jsonl_path=jsonl_path, days=30
        )
        eng_line = admin_metrics.format_engagement_line(
            raters_map.get(cid, 0),
            baseline_map.get(cid, 0),
            period_label=plabel,
        )

    finance_block = monthly_ops.format_calendar_month_ops_block(stats)
    discipline_block = monthly_ops.format_calendar_month_discipline_block(stats)

    range_s = _short_period_range(PERIOD_CALENDAR, start, end)
    header = [
        "📊 <b>Отчёт за календарный месяц</b>",
        "",
        f"<b>{escape(title)}</b>",
        f"<i>{escape(plabel)} · {escape(range_s)}</i>",
        "",
        finance_block,
        "",
        discipline_block,
        "",
    ]

    if not monthly_ops.monthly_plan_amount(rec if isinstance(rec, dict) else {}, month_key):
        header.append(
            "<i>💡 Задайте план на месяц — в конце периода увидите % выполнения.</i>\n"
        )

    tz_loc = str(rec.get("timezone", tz)) if isinstance(rec, dict) else tz
    stop_line = ops_day.stop_list_line_for_period(
        rec if isinstance(rec, dict) else {},
        start,
        end,
        tz_loc,
    )

    report_msgs = build_report_html(
        period=PERIOD_CALENDAR,
        period_label=plabel,
        scope_title=title,
        start=start,
        end=end,
        current=cur,
        previous=prev,
        problem_labels=problem_labels,
        engagement_line=eng_line,
        problem_stats_line=ops_day.format_problem_stats_block(pstats),
        ops_summary_line=None,
        stop_list_line=stop_line,
        departures_line=(
            f"<b>Уходы кадра за месяц:</b> {dep_n}" if dep_n else None
        ),
    )
    if report_msgs:
        body = report_msgs[0]
        if body.startswith("📊 <b>Сводка Pulse Team</b>"):
            body = body.replace(
                "📊 <b>Сводка Pulse Team</b>",
                "📊 <b>Команда и обратная связь</b>",
                1,
            )
        messages = ["\n".join(header) + "\n" + body] + report_msgs[1:]
        parts: list[str] = []
        parts.extend(split_telegram_html(messages[0].rstrip()))
        for extra in messages[1:]:
            parts.extend(split_telegram_html(extra))
    else:
        messages = ["\n".join(header).rstrip()]
        parts = split_telegram_html(messages[0])

    kb = None
    if not monthly_ops.monthly_plan_amount(rec if isinstance(rec, dict) else {}, month_key):
        kb = monthly_plan_prompt_keyboard(cid, month_key)
    return (
        ReportResult(messages, parts, cur, problem_labels),
        kb,
    )
