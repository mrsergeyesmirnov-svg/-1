"""
Операционный день точки (фаза 1): план утром, закрытие вечером, уход кадра, задания.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import problems_pulse
import pulse_model

PLAN_MET_YES = "yes"
PLAN_MET_PARTIAL = "partial"
PLAN_MET_NO = "no"

PLAN_MET_RU = {
    PLAN_MET_YES: "✅ План выполнен",
    PLAN_MET_PARTIAL: "🟡 Частично",
    PLAN_MET_NO: "❌ Не выполнен",
}

DEFAULT_MORNING_POST = "11:30"
DEFAULT_EVENING_POST = "00:00"

MORNING_REMINDER_MINUTES = 5
EVENING_REMINDER_MINUTES = 10

MORNING_STEPS = ("plan", "stop", "roster")
EVENING_STEPS = ("revenue", "plan_met", "comment")


def _minutes_from_hhmm(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def hhmm_from_minutes(total: int) -> str:
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def reminder_hhmm(post_hhmm: str, minutes_before: int) -> str:
    return hhmm_from_minutes(_minutes_from_hhmm(post_hhmm) - minutes_before)


def _chat_rec(data: dict[str, Any], chat_id: int) -> dict[str, Any]:
    chats = data.setdefault("chats", {})
    cid = str(chat_id)
    if cid not in chats:
        chats[cid] = {}
    return chats[cid]


def ops_schedule(rec: dict[str, Any]) -> dict[str, str]:
    raw = rec.get("ops_schedule") if isinstance(rec.get("ops_schedule"), dict) else {}
    return {
        "morning_post": str(raw.get("morning_post") or DEFAULT_MORNING_POST),
        "evening_post": str(raw.get("evening_post") or DEFAULT_EVENING_POST),
    }


def today_key(tz_name: str = "Europe/Moscow") -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    return datetime.now(tz).date().isoformat()


def shift_day_for_evening(tz_name: str, morning_post: str) -> str:
    """День смены для закрытия: до утреннего плана — ещё «вчера» (итоги в 00:00)."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    now_m = now.hour * 60 + now.minute
    if now_m < _minutes_from_hhmm(morning_post):
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def _day_bucket(rec: dict[str, Any], day: str) -> dict[str, Any]:
    days = rec.setdefault("ops_days", {})
    if not isinstance(days, dict):
        days = {}
        rec["ops_days"] = days
    if day not in days or not isinstance(days.get(day), dict):
        days[day] = {}
    return days[day]


def _prune_old_days(rec: dict[str, Any], *, keep: int = 45) -> None:
    days = rec.get("ops_days")
    if not isinstance(days, dict) or len(days) <= keep:
        return
    for k in sorted(days.keys())[:-keep]:
        days.pop(k, None)


def get_morning(rec: dict[str, Any], day: str) -> dict[str, Any]:
    return _day_bucket(rec, day).setdefault("morning", {})


def get_evening(rec: dict[str, Any], day: str) -> dict[str, Any]:
    return _day_bucket(rec, day).setdefault("evening", {})


def morning_complete(morning: dict[str, Any]) -> bool:
    return bool((morning.get("plan") or "").strip())


def evening_complete(evening: dict[str, Any]) -> bool:
    return evening.get("plan_met") in (PLAN_MET_YES, PLAN_MET_PARTIAL, PLAN_MET_NO)


def format_morning_group_post(
    chat_title: str,
    *,
    plan: str,
    stop_list: str,
    roster: str,
) -> str:
    lines = [
        f"☀️ <b>План дня</b> · {escape(chat_title)}",
        "",
        f"<b>План</b>\n{escape(plan.strip())}",
    ]
    stop = (stop_list or "").strip()
    if stop and stop not in ("—", "-", "нет", "нет стопа"):
        lines.extend(["", f"<b>Стоп-лист</b>\n{escape(stop)}"])
    roster_s = (roster or "").strip()
    if roster_s and roster_s not in ("—", "-"):
        lines.extend(["", f"<b>Расстановка</b>\n{escape(roster_s)}"])
    lines.append("\n<i>На брифинге операционку не дублируем — всё здесь.</i>")
    return "\n".join(lines)


def format_evening_group_post(
    chat_title: str,
    *,
    plan_met: str,
    revenue: str | None,
    comment: str,
    morning_posted: bool,
) -> str:
    met_ru = PLAN_MET_RU.get(plan_met, plan_met)
    lines = [
        f"🌙 <b>Итоги дня</b> · {escape(chat_title)}",
        "",
        f"<b>План дня:</b> {escape(met_ru)}",
    ]
    if revenue:
        lines.append(f"<b>Выручка:</b> {escape(revenue)}")
    comment_s = (comment or "").strip()
    if comment_s and comment_s not in ("—", "-"):
        lines.extend(["", f"<b>Комментарий</b>\n{escape(comment_s)}"])
    if not morning_posted:
        lines.append("\n<i>Утренний план сегодня не публиковался.</i>")
    return "\n".join(lines)


def save_morning_draft(
    data: dict[str, Any],
    chat_id: int,
    *,
    day: str,
    plan: str,
    stop_list: str,
    roster: str,
    by_uid: int,
) -> None:
    rec = _chat_rec(data, chat_id)
    m = get_morning(rec, day)
    m.update(
        {
            "plan": plan.strip(),
            "stop_list": stop_list.strip(),
            "roster": roster.strip(),
            "by_uid": by_uid,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    _prune_old_days(rec)


def mark_morning_posted(rec: dict[str, Any], day: str) -> None:
    m = get_morning(rec, day)
    m["posted"] = True
    m["posted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")


def save_evening_draft(
    data: dict[str, Any],
    chat_id: int,
    *,
    day: str,
    revenue: str,
    plan_met: str,
    comment: str,
    by_uid: int,
) -> None:
    rec = _chat_rec(data, chat_id)
    ev = get_evening(rec, day)
    ev.update(
        {
            "revenue": revenue.strip(),
            "plan_met": plan_met,
            "comment": comment.strip(),
            "by_uid": by_uid,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    _prune_old_days(rec)


def mark_evening_posted(rec: dict[str, Any], day: str) -> None:
    ev = get_evening(rec, day)
    ev["posted"] = True
    ev["posted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")


def morning_location_keyboard(scope: list[tuple[str, str]], prefix: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [
            InlineKeyboardButton(
                text=f"📍 {title[:36]}",
                callback_data=f"{prefix}:{cid}"[:64],
            )
        ]
        for cid, title in scope[:15]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def evening_plan_met_keyboard(chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Выполнен",
                    callback_data=f"ops:pm:{cid}:{PLAN_MET_YES}",
                ),
                InlineKeyboardButton(
                    text="🟡 Частично",
                    callback_data=f"ops:pm:{cid}:{PLAN_MET_PARTIAL}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Не выполнен",
                    callback_data=f"ops:pm:{cid}:{PLAN_MET_NO}",
                )
            ],
        ]
    )


def morning_actions_keyboard(chat_id: int, *, posted: bool):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    if not posted:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📣 Опубликовать в чат сейчас",
                    callback_data=f"ops:mpub:{cid}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"ops:medit:{cid}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def evening_actions_keyboard(chat_id: int, *, posted: bool):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    if not posted:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📣 Опубликовать итог в чат",
                    callback_data=f"ops:epub:{cid}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"ops:eedit:{cid}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _new_task_id() -> str:
    return "t_" + secrets.token_hex(4)


def can_assign_tasks(data: dict[str, Any], uid: int, *, is_global_admin: bool) -> bool:
    if is_global_admin:
        return True
    for p in pulse_model.manager_profiles(data, uid):
        if p.get("role") == pulse_model.ROLE_NETWORK_ADMIN:
            return True
    return False


def list_assignable_managers(
    data: dict[str, Any], assigner_uid: int, *, is_global_admin: bool
) -> list[tuple[int, str]]:
    """(telegram user_id, подпись) менеджеров, которым можно назначить задачу."""
    org_ids: set[str] | None = None
    if not is_global_admin:
        org_ids = {
            str(p.get("organization_id"))
            for p in pulse_model.manager_profiles(data, assigner_uid)
            if p.get("role") == pulse_model.ROLE_NETWORK_ADMIN and p.get("organization_id")
        }
        if not org_ids:
            return []
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for uid_s, profiles in data.get("managers", {}).items():
        if not isinstance(profiles, list):
            continue
        try:
            uid_i = int(uid_s)
        except (TypeError, ValueError):
            continue
        if uid_i == assigner_uid:
            continue
        for p in profiles:
            if not isinstance(p, dict):
                continue
            oid = str(p.get("organization_id") or "")
            if org_ids is not None and oid not in org_ids:
                continue
            if p.get("role") not in (
                pulse_model.ROLE_LOCATION_ADMIN,
                pulse_model.ROLE_NETWORK_ADMIN,
            ):
                continue
            if uid_i in seen:
                break
            seen.add(uid_i)
            role = "управляющий" if p.get("role") == pulse_model.ROLE_NETWORK_ADMIN else "менеджер"
            locs = p.get("location_chat_ids") or []
            loc_hint = f" · {len(locs)} т." if locs else ""
            out.append((uid_i, f"{role}{loc_hint} (id {uid_i})"))
            break
    return sorted(out, key=lambda x: x[0])


def assign_task_managers_keyboard(managers: list[tuple[int, str]]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [
            InlineKeyboardButton(
                text=label[:40],
                callback_data=f"ops:ato:{uid}"[:64],
            )
        ]
        for uid, label in managers[:20]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def assign_task(
    data: dict[str, Any],
    *,
    from_uid: int,
    to_uid: int,
    chat_id: int | None,
    text: str,
    deadline: str | None = None,
) -> dict[str, Any]:
    rec = data.setdefault("manager_tasks", [])
    if not isinstance(rec, list):
        rec = []
        data["manager_tasks"] = rec
    task = {
        "id": _new_task_id(),
        "from_uid": from_uid,
        "to_uid": to_uid,
        "chat_id": chat_id,
        "text": text.strip(),
        "deadline": deadline,
        "status": "open",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    rec.append(task)
    if len(rec) > 200:
        data["manager_tasks"] = rec[-200:]
    return task


def tasks_for_user(data: dict[str, Any], uid: int, *, open_only: bool = True) -> list[dict]:
    raw = data.get("manager_tasks")
    if not isinstance(raw, list):
        return []
    out = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        if int(t.get("to_uid", 0)) != int(uid):
            continue
        if open_only and t.get("status") != "open":
            continue
        out.append(t)
    return sorted(out, key=lambda x: x.get("created_at", ""), reverse=True)


def complete_task(data: dict[str, Any], task_id: str, uid: int) -> bool:
    raw = data.get("manager_tasks")
    if not isinstance(raw, list):
        return False
    for t in raw:
        if isinstance(t, dict) and t.get("id") == task_id and int(t.get("to_uid", 0)) == uid:
            t["status"] = "done"
            t["done_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            return True
    return False


def format_tasks_list(tasks: list[dict], *, title: str = "Задания") -> str:
    if not tasks:
        return f"<b>{escape(title)}</b>\n\n<i>Открытых заданий нет.</i>"
    lines = [f"<b>{escape(title)}</b>", ""]
    for t in tasks[:15]:
        dl = t.get("deadline")
        dl_s = f" · до {escape(str(dl))}" if dl else ""
        lines.append(f"• {escape(str(t.get('text', '')))}{dl_s}")
    return "\n".join(lines)


def tasks_keyboard(tasks: list[dict]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for t in tasks[:10]:
        tid = t.get("id")
        if not tid:
            continue
        short = str(t.get("text", ""))[:28]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ {short}",
                    callback_data=f"ops:tdone:{tid}"[:64],
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def problem_stats_summary(data: dict[str, Any], chat_id: int) -> dict[str, int]:
    rows = await problems_pulse.list_problems_for_chat(
        data, chat_id, view=problems_pulse.VIEW_ALL
    )
    stats = {"new": 0, "in_progress": 0, "resolved": 0, "ignored": 0, "total": len(rows)}
    for p in rows:
        st = p.status
        if st in stats:
            stats[st] += 1
    stats["active"] = stats["new"] + stats["in_progress"]
    stats["closed"] = stats["resolved"] + stats["ignored"]
    return stats


def format_problem_stats_block(stats: dict[str, int]) -> str:
    if stats.get("total", 0) == 0:
        return "<b>Горящие вопросы</b>\n<i>Активных тем нет.</i>"
    return (
        "<b>Горящие вопросы</b>\n"
        f"🔴 Новые: <b>{stats.get('new', 0)}</b> · "
        f"🟡 В работе: <b>{stats.get('in_progress', 0)}</b> · "
        f"🟢 Решено: <b>{stats.get('resolved', 0)}</b>"
    )


async def build_daily_manager_digest(
    data: dict[str, Any],
    chat_id: int,
    *,
    day: str,
    engagement_line: str,
    departures_30d: int,
) -> str:
    rec = data.get("chats", {}).get(str(chat_id), {})
    if not isinstance(rec, dict):
        rec = {}
    title = str(rec.get("title", chat_id))
    morning = get_morning(rec, day)
    evening = get_evening(rec, day)
    stats = await problem_stats_summary(data, chat_id)

    lines = [
        f"📋 <b>Суточная сводка</b> · {escape(title)}",
        f"<i>{escape(day)}</i>",
        "",
        engagement_line,
        "",
        format_problem_stats_block(stats),
        "",
    ]

    if morning_complete(morning):
        posted = "опубликован" if morning.get("posted") else "черновик"
        lines.append(f"<b>Утренний план:</b> {posted}")
    else:
        lines.append("<b>Утренний план:</b> не заполнен")

    if evening_complete(evening):
        met = PLAN_MET_RU.get(str(evening.get("plan_met")), "—")
        rev = evening.get("revenue")
        rev_s = f" · выручка {escape(str(rev))}" if rev else ""
        posted_e = " · в чате" if evening.get("posted") else ""
        lines.append(f"<b>Закрытие дня:</b> {escape(met)}{rev_s}{posted_e}")
    else:
        lines.append("<b>Закрытие дня:</b> не заполнено")

    if departures_30d:
        lines.append(f"\n<b>Уходы кадра за 30 дн.:</b> {departures_30d}")

    return "\n".join(lines)


def count_staff_departures(
    events: list[dict[str, Any]],
    chat_id: int,
    *,
    days: int = 30,
) -> int:
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    n = 0
    for e in events:
        if e.get("event") != "staff_departure":
            continue
        if int(e.get("restaurant_chat_id") or 0) != int(chat_id):
            continue
        ts_raw = e.get("ts")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=ZoneInfo("UTC"))
                if ts < cutoff:
                    continue
            except ValueError:
                pass
        n += 1
    return n


async def load_departure_events(jsonl_path, chat_id: int) -> list[dict[str, Any]]:
    import json
    from pathlib import Path

    p = Path(jsonl_path) if jsonl_path else None
    if not p or not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("event") == "staff_departure":
            out.append(d)
    return out
