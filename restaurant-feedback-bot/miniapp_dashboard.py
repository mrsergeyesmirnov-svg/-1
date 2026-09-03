"""
Данные для Mini App: обзор точки (зал/кухня), вовлечённость, горящие, ИИ-намётки.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import chef_survey
import pulse_model
import report_pulse


def _dept_bucket(events: list[report_pulse.EventRow], dept: str) -> dict[str, Any]:
    bucket = report_pulse.filter_events_by_department(events, dept)
    ratings = [
        e.rating
        for e in bucket
        if e.event_type == report_pulse.EVENT_RATING and e.rating is not None
    ]
    comments = [
        {
            "text": (e.comment_text or "").strip()[:280],
            "at": e.created_at.isoformat() if e.created_at else None,
            "problem": e.problem_code,
        }
        for e in report_pulse._comment_rows(bucket)[:8]
        if (e.comment_text or "").strip()
    ]
    blockers = Counter()
    for e in bucket:
        if e.event_type in (report_pulse.EVENT_PROBLEM, "boost", "chef_shift") and e.problem_code:
            blockers[e.problem_code] += 1
    top = [
        {
            "code": code,
            "label": report_pulse.PROBLEM_LABELS.get(code)
            or chef_survey.CHEF_SURVEY_LABELS.get(code, code),
            "count": n,
        }
        for code, n in blockers.most_common(5)
    ]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    return {
        "department": dept,
        "title": pulse_model.department_title_ru(dept)
        if dept in (pulse_model.CHAT_DEPT_FLOOR, pulse_model.CHAT_DEPT_KITCHEN)
        else chef_survey.department_title(dept),
        "ratings_count": len(ratings),
        "avg_rating": avg,
        "comments_count": len(comments),
        "comments": comments,
        "top_blockers": top,
    }


def _ai_teaser(floor: dict[str, Any], kitchen: dict[str, Any], hot_n: int) -> dict[str, Any]:
    tips: list[str] = []
    for bucket in (floor, kitchen):
        for b in bucket.get("top_blockers") or []:
            code = b.get("code") or ""
            hint = report_pulse.DAY_INSIGHT_HINTS.get(code)
            if hint and hint not in tips:
                tips.append(hint)
            if len(tips) >= 3:
                break
        if len(tips) >= 3:
            break
    if hot_n:
        tips.insert(
            0,
            f"Сейчас {hot_n} горящих вопрос(а) — разберите статусы до пика следующей смены.",
        )
    if not tips:
        tips = [
            "Пока мало сигналов. Когда появятся отзывы зала и кухни — здесь будут живые советы.",
            "Смотрите вовлечённость: если мало оценок — напомните кнопку в группе.",
        ]
    return {
        "title": "ИИ-наставник · черновик",
        "blurb": "Первые выводы по отзывам. Полный разбор и «Подробнее» — дальше в приложении.",
        "tips": tips[:4],
    }


async def build_dashboard(
    data: dict[str, Any],
    user_id: int,
    *,
    is_global_admin: bool,
    chat_id: int | None = None,
    period: str = report_pulse.PERIOD_WEEK,
    jsonl_path: Path | None = None,
) -> dict[str, Any]:
    scope = report_pulse.chat_scope_for_user(
        data, user_id, is_global_admin=is_global_admin
    )
    if not scope:
        return {
            "ok": True,
            "empty": True,
            "message": "Нет привязанных точек. Подключите чат через /link_org и роль менеджера.",
            "locations": [],
        }

    locations = [{"id": cid, "title": title} for cid, title in scope[:40]]
    pick = str(chat_id) if chat_id is not None else str(scope[0][0])
    if pick not in {loc["id"] for loc in locations} and not is_global_admin:
        pick = locations[0]["id"]
    try:
        pick_i = int(pick)
    except ValueError:
        pick_i = int(scope[0][0])

    title = next((t for c, t in scope if str(c) == str(pick_i)), str(pick_i))
    rec = data.get("chats", {}).get(str(pick_i)) or {}
    tz = str(rec.get("timezone") or "Europe/Moscow")
    period_key = period if period in (
        report_pulse.PERIOD_SHIFT,
        report_pulse.PERIOD_WEEK,
        report_pulse.PERIOD_MONTH,
    ) else report_pulse.PERIOD_WEEK
    start, end, _ps, _pe, plabel = report_pulse.period_window(period_key, tz)

    chat_ids = pulse_model.sibling_chat_ids_for_location(data, pick_i)
    events = await report_pulse.load_events(
        chat_ids, start, end, jsonl_path=jsonl_path
    )

    floor = _dept_bucket(events, chef_survey.DEPARTMENT_FLOOR)
    kitchen = _dept_bucket(events, chef_survey.DEPARTMENT_KITCHEN)
    all_b = _dept_bucket(events, chef_survey.DEPARTMENT_ALL)

    ratings_n = all_b["ratings_count"]
    try:
        import admin_metrics

        baseline = await admin_metrics.count_unique_users_by_chat(
            chat_ids, jsonl_path=jsonl_path, days=30
        )
        raters_map = await admin_metrics.count_raters_in_period(
            chat_ids, start, end, jsonl_path=jsonl_path
        )
        base_n = sum(baseline.get(str(c), 0) for c in chat_ids) or 0
        rater_n = sum(raters_map.get(str(c), 0) for c in chat_ids) or 0
    except Exception:
        base_n, rater_n = 0, ratings_n

    engagement_pct = None
    if base_n > 0:
        engagement_pct = round(100.0 * min(rater_n, base_n) / base_n, 1)

    hot: list[dict[str, Any]] = []
    import problems_pulse

    for cid in chat_ids:
        try:
            rows = await problems_pulse.list_problems_for_chat(
                data, cid, include_ignored=False, view=problems_pulse.VIEW_ACTIVE
            )
        except Exception:
            rows = []
        for p in rows[:12]:
            hot.append(
                {
                    "id": p.id,
                    "title": p.title,
                    "status": p.status,
                    "status_ru": problems_pulse.STATUS_RU.get(p.status, p.status),
                    "mentions": p.mentions_count,
                    "chat_id": str(p.restaurant_chat_id),
                }
            )
    hot.sort(key=lambda x: (-int(x.get("mentions") or 0), x.get("title") or ""))

    return {
        "ok": True,
        "empty": False,
        "chat_id": str(pick_i),
        "title": title,
        "period": period_key,
        "period_label": plabel,
        "locations": locations,
        "engagement": {
            "raters": rater_n,
            "baseline_30d": base_n,
            "pct": engagement_pct,
            "ratings": ratings_n,
            "avg_rating": all_b["avg_rating"],
        },
        "floor": floor,
        "kitchen": kitchen,
        "hot_problems": hot[:15],
        "hot_count": len(hot),
        "ai": _ai_teaser(floor, kitchen, len(hot)),
        "feedback_hint": (
            "Линейка пишет отзыв в боте (кнопка из группы). "
            "Сводка, зал/кухня и доступы — в приложении."
        ),
    }
