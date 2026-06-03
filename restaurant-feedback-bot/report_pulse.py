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

PERSONAL_FACTOR_LABELS: dict[str, str] = {
    "knowledge": "Нехватка знаний по процессам",
    "fatigue": "Усталость / эмоциональное состояние",
    "time_mgmt": "Сложности с тайм-менеджментом",
    "communication": "Сложности в коммуникации",
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

# «Месячный» отчёт — скользящее окно, не календарный месяц
MONTH_REPORT_DAYS = 21

PERIOD_TITLES = {
    PERIOD_SHIFT: "последняя смена (24 ч)",
    PERIOD_WEEK: "неделя",
    PERIOD_MONTH: "последние 3 недели",
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


def report_period_keyboard():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🕐 Смена (24 ч)", callback_data="report_p:shift")],
            [InlineKeyboardButton(text="📅 Неделя", callback_data="report_p:week")],
            [InlineKeyboardButton(text="📆 3 недели", callback_data="report_p:month")],
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
                      AND created_at >= $2 AND created_at <= $3
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
            if created < start or created > end:
                continue
            rows.append(row)
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


def _format_insights_html(insights: list[AnalyticalInsight]) -> list[str]:
    lines: list[str] = []
    for i, ins in enumerate(insights, 1):
        lines.append(f"<b>{i}. {escape(ins.title)}</b>")
        if ins.factors:
            lines.append("<i>Факторы:</i>")
            for code, n in ins.factors:
                label = PROBLEM_LABELS.get(code) or PERSONAL_FACTOR_LABELS.get(code, code)
                lines.append(f"• {escape(label)} — <b>{n}</b>")
        lines.append(f"<i>Рекомендация:</i> {escape(ins.recommendation)}")
        lines.append("")
    return lines


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
            f"В «Проблемах» можно взять тему в работу и сообщить команде."
        )
    if personal:
        code, n = personal[0]
        return (
            f"Частый личный фактор: «{PERSONAL_FACTOR_LABELS.get(code, code)}» ({n}). "
            "Имеет смысл обсудить на планёрке без персональных имён."
        )
    return None


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
) -> str:
    def plab(c: str) -> str:
        if problem_labels and c in problem_labels:
            return problem_labels[c]
        return PROBLEM_LABELS.get(c, c)
    n_ratings = _count_ratings(current)
    n_comments = _count_text_comments(current)
    avg_c = _avg_rating(current)
    avg_p = _avg_rating(previous)
    problems = _code_stats(current, EVENT_PROBLEM)
    personal = _code_stats(current, EVENT_PERSONAL)
    comment_limit = 8 if period == PERIOD_SHIFT else 5
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

    date_fmt = "%d.%m.%Y %H:%M"
    lines = [
        f"📊 <b>Отчёт Pulse Team</b>",
        f"<b>{escape(scope_title)}</b>",
        f"Период: <b>{escape(period_label)}</b>",
        f"{start.strftime(date_fmt)} — {end.strftime(date_fmt)}",
        "",
        _trend_line(avg_c, avg_p),
        f"Оценок смен за период: <b>{n_ratings}</b>",
        f"Текстовых комментариев: <b>{n_comments}</b>",
        "",
    ]

    if problems:
        lines.append("<b>Что влияло на смену</b> <i>(кнопки)</i>")
        for code, n in problems[:5]:
            lines.append(f"• {escape(plab(code))} — <b>{n}</b>")
        lines.append("")
    else:
        lines.append("<i>Темы «что повлияло» не выбирали или только оценки.</i>\n")

    if personal:
        lines.append("<b>Личные факторы смены</b>")
        for code, n in personal[:5]:
            label = PERSONAL_FACTOR_LABELS.get(code, code)
            lines.append(f"• {escape(label)} — <b>{n}</b>")
        lines.append("")

    if comments:
        if period == PERIOD_SHIFT:
            lines.append("<b>Последние комментарии</b> <i>(анонимно, новые сверху)</i>")
        else:
            lines.append("<b>Яркие комментарии</b> <i>(анонимно)</i>")
        for text, plab, ts in comments:
            tag = f"<i>{escape(plab)}</i> — " if plab else ""
            when = ""
            if ts is not None and period == PERIOD_SHIFT:
                try:
                    when = ts.strftime("%d.%m %H:%M") + " · "
                except Exception:
                    when = ""
            lines.append(f"💬 {when}{tag}«{escape(text)}»")
        lines.append("")
    else:
        lines.append("<i>Текстовых комментариев за период нет.</i>\n")

    if period == PERIOD_MONTH:
        dow_lines = build_monthly_weekday_trends_html(
            current, problem_labels=problem_labels
        )
        if dow_lines:
            lines.extend(dow_lines)

    if insights:
        lines.append("<b>🧠 Аналитические выводы</b>")
        lines.extend(_format_insights_html(insights))
    else:
        fb = _fallback_tip(problems, personal, problem_labels=problem_labels)
        if fb:
            lines.append("<b>На что обратить внимание</b>")
            lines.append(f"🔸 {escape(fb)}")
        else:
            lines.append(
                "<i>Выводы появятся, когда накопится больше откликов за период.</i>"
            )

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
    try:
        import survey_buttons

        problem_labels = survey_buttons.merge_labels_for_chats(data, chat_ids)
    except Exception:
        problem_labels = None
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
            period=period,
            period_label=plabel,
            scope_title=title,
            start=start,
            end=end,
            current=[e for e in current if e.restaurant_chat_id == int(cid) or e.restaurant_chat_id is None],
            previous=[e for e in previous if e.restaurant_chat_id == int(cid) or e.restaurant_chat_id is None],
            problem_labels=problem_labels,
        )
        return split_telegram_html(body)

    parts: list[str] = []
    for cid, title in scope:
        cid_i = int(cid)
        cur = [e for e in current if e.restaurant_chat_id == cid_i]
        if not cur:
            continue
        prev = [e for e in previous if e.restaurant_chat_id == cid_i]
        try:
            import survey_buttons

            plab_map = survey_buttons.merge_labels_for_chats(data, [cid_i])
        except Exception:
            plab_map = problem_labels
        body = build_report_html(
            period=period,
            period_label=plabel,
            scope_title=title,
            start=start,
            end=end,
            current=cur,
            previous=prev,
            problem_labels=plab_map,
        )
        parts.extend(split_telegram_html(body))
    if not parts:
        return [
            f"За период «{escape(plabel)}» по вашим точкам нет данных."
        ]
    return parts
