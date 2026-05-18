"""
Отчёты для менеджеров: период, метрики, проблемы, комментарии (без user_id).
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any
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

ACTION_HINTS = (
    "медлен", "очеред", "ждат", "конфликт", "ссор", "груб", "токсич",
    "нехват", "устал", "нагруз", "бардак", "хаос", "организац", "смен",
    "кухн", "бар", "менедж", "руковод", "зарплат", "мотивац",
)

PERIOD_SHIFT = "shift"
PERIOD_WEEK = "week"
PERIOD_MONTH = "month"

PERIOD_TITLES = {
    PERIOD_SHIFT: "последняя смена (24 ч)",
    PERIOD_WEEK: "неделя",
    PERIOD_MONTH: "месяц",
}


@dataclass
class EventRow:
    created_at: datetime
    event_type: str
    rating: int | None
    problem_code: str | None
    comment_text: str | None
    restaurant_chat_id: int | None
    restaurant_label: str | None


def report_period_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🕐 Смена (24 ч)", callback_data="report_p:shift")],
            [InlineKeyboardButton(text="📅 Неделя", callback_data="report_p:week")],
            [InlineKeyboardButton(text="📆 Месяц", callback_data="report_p:month")],
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
    ids = pulse_model.allowed_chat_ids_for_manager(data, user_id)
    if ids:
        return ids
    if is_global_admin:
        return {cid for cid, rec in data.get("chats", {}).items() if isinstance(rec, dict)}
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
        start = now - timedelta(days=30)
        prev_end = start
        prev_start = start - timedelta(days=30)
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
    return EventRow(
        created_at=created,
        event_type=str(d.get("event_type") or d.get("event") or ""),
        rating=rating,
        problem_code=d.get("problem_code") or d.get("problem"),
        comment_text=d.get("comment_text") or d.get("comment"),
        restaurant_chat_id=cid,
        restaurant_label=d.get("restaurant_label"),
    )


async def load_events(
    chat_ids: list[int],
    start: datetime,
    end: datetime,
    *,
    jsonl_path: Path | None,
) -> list[EventRow]:
    rows: list[EventRow] = []
    pool = db_pulse.pool()
    if pool and chat_ids:
        try:
            async with pool.acquire() as conn:
                recs = await conn.fetch(
                    """
                    SELECT created_at, event_type, rating, problem_code, comment_text,
                           restaurant_chat_id
                    FROM feedback_events
                    WHERE restaurant_chat_id = ANY($1::bigint[])
                      AND created_at >= $2 AND created_at < $3
                    ORDER BY created_at DESC
                    """,
                    chat_ids,
                    start,
                    end,
                )
            for r in recs:
                rows.append(
                    EventRow(
                        created_at=r["created_at"],
                        event_type=r["event_type"],
                        rating=r["rating"],
                        problem_code=r["problem_code"],
                        comment_text=r["comment_text"],
                        restaurant_chat_id=r["restaurant_chat_id"],
                        restaurant_label=None,
                    )
                )
            if rows:
                return rows
        except Exception as e:
            print("[report load_events postgres]", repr(e))

    if jsonl_path and jsonl_path.exists():
        chat_set = {str(c) for c in chat_ids}
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = d.get("restaurant_chat_id")
            if chat_set and str(cid) not in chat_set:
                continue
            row = _row_from_dict(d)
            if not row:
                continue
            created = row.created_at
            if created.tzinfo is None and start.tzinfo is not None:
                created = created.replace(tzinfo=start.tzinfo)
            if created < start or created >= end:
                continue
            rows.append(row)
    return rows


def _avg_rating(events: list[EventRow]) -> float | None:
    vals = [e.rating for e in events if e.event_type == "rating" and e.rating is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


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


def _problem_stats(events: list[EventRow]) -> list[tuple[str, int]]:
    cnt: Counter[str] = Counter()
    for e in events:
        if e.event_type == "problem" and e.problem_code:
            cnt[e.problem_code] += 1
    return cnt.most_common()


def _collect_comments(events: list[EventRow]) -> list[tuple[str, str | None]]:
    """(text, problem_label optional) без идентификаторов людей."""
    out: list[tuple[str, str | None]] = []
    for e in events:
        text = (e.comment_text or "").strip()
        if not text:
            continue
        plab = None
        if e.problem_code:
            plab = PROBLEM_LABELS.get(e.problem_code, e.problem_code)
        out.append((text, plab))
    out.sort(key=lambda x: len(x[0]), reverse=True)
    return out[:5]


def _actionable_bullets(problems: list[tuple[str, int]], comments: list[tuple[str, str | None]]) -> list[str]:
    tips: list[str] = []
    for code, n in problems[:3]:
        label = PROBLEM_LABELS.get(code, code)
        tips.append(f"Разобрать тему «{label}» — отмечено {n} раз(а).")
    for text, plab in comments:
        low = text.lower()
        if any(h in low for h in ACTION_HINTS):
            short = text if len(text) <= 120 else text[:117] + "…"
            prefix = f"({plab}) " if plab else ""
            tips.append(f"Обратить внимание: {prefix}{short}")
        if len(tips) >= 5:
            break
    if not tips and problems:
        code, n = problems[0]
        tips.append(
            f"Сфокусироваться на «{PROBLEM_LABELS.get(code, code)}» — самая частая тема ({n})."
        )
    return tips[:5]


def build_report_html(
    *,
    period_label: str,
    scope_title: str,
    start: datetime,
    end: datetime,
    current: list[EventRow],
    previous: list[EventRow],
) -> str:
    n_ratings = sum(1 for e in current if e.event_type == "rating")
    avg_c = _avg_rating(current)
    avg_p = _avg_rating(previous)
    problems = _problem_stats(current)
    comments = _collect_comments(current)
    tips = _actionable_bullets(problems, comments)

    date_fmt = "%d.%m.%Y %H:%M"
    lines = [
        f"📊 <b>Отчёт Pulse Team</b>",
        f"<b>{escape(scope_title)}</b>",
        f"Период: <b>{escape(period_label)}</b>",
        f"{start.strftime(date_fmt)} — {end.strftime(date_fmt)}",
        "",
        _trend_line(avg_c, avg_p),
        f"Оценок за период: <b>{n_ratings}</b>",
        "",
    ]

    if problems:
        lines.append("<b>Частые темы</b>")
        for code, n in problems[:5]:
            label = PROBLEM_LABELS.get(code, code)
            lines.append(f"• {escape(label)} — <b>{n}</b>")
        lines.append("")
    else:
        lines.append("<i>Отдельные темы проблем не выбирали — только оценки или мало ответов.</i>\n")

    if comments:
        lines.append("<b>Заметные комментарии</b> <i>(анонимно)</i>")
        for text, plab in comments:
            tag = f"<i>{escape(plab)}</i> — " if plab else ""
            lines.append(f"💬 {tag}«{escape(text)}»")
        lines.append("")
    else:
        lines.append("<i>Текстовых комментариев за период нет.</i>\n")

    if tips:
        lines.append("<b>На что обратить внимание</b>")
        for t in tips:
            lines.append(f"🔸 {escape(t)}")
    else:
        lines.append("<i>Рекомендации появятся, когда накопится больше откликов.</i>")

    return "\n".join(lines)


def split_telegram_html(text: str, limit: int = 3800) -> list[str]:
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
) -> list[str]:
    full_scope = chat_scope_for_user(data, user_id, is_global_admin=is_global_admin)
    scope = narrow_scope(full_scope, selected_chat)
    if not scope:
        return [
            "Нет привязанных точек для отчёта. "
            "Обратитесь к администратору Pulse или выполните <code>/link_manager</code>."
        ]

    tz = tz_name
    if scope:
        rec = data.get("chats", {}).get(scope[0][0])
        if isinstance(rec, dict) and rec.get("timezone"):
            tz = str(rec["timezone"])
    start, end, prev_start, prev_end, plabel = period_window(period, tz)
    chat_ids = [int(cid) for cid, _ in scope]
    current = await load_events(chat_ids, start, end, jsonl_path=jsonl_path)
    previous = await load_events(chat_ids, prev_start, prev_end, jsonl_path=jsonl_path)

    if not current:
        return [
            f"За период «{escape(plabel)}» ответов пока нет.\n\n"
            "Попросите команду пройти check-in из рабочей группы (кнопка «Оценить смену в личке»)."
        ]

    multi = len(scope) > 1 and (selected_chat in (None, "all"))
    if not multi:
        cid, title = scope[0]
        body = build_report_html(
            period_label=plabel,
            scope_title=title,
            start=start,
            end=end,
            current=[e for e in current if e.restaurant_chat_id == int(cid) or e.restaurant_chat_id is None],
            previous=[e for e in previous if e.restaurant_chat_id == int(cid) or e.restaurant_chat_id is None],
        )
        return split_telegram_html(body)

    parts: list[str] = []
    for cid, title in scope:
        cid_i = int(cid)
        cur = [e for e in current if e.restaurant_chat_id == cid_i]
        if not cur:
            continue
        prev = [e for e in previous if e.restaurant_chat_id == cid_i]
        body = build_report_html(
            period_label=plabel,
            scope_title=title,
            start=start,
            end=end,
            current=cur,
            previous=prev,
        )
        parts.extend(split_telegram_html(body))
    if not parts:
        return [
            f"За период «{escape(plabel)}» по вашим точкам нет данных."
        ]
    return parts
