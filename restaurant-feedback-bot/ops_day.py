"""
Операционный день точки (фаза 1): план утром, закрытие вечером, уход кадра, задания.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, time
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
DEFAULT_CHEF_STOP_DEADLINE = "11:45"

MORNING_REMINDER_MINUTES = 5
EVENING_REMINDER_MINUTES = 10
CHEF_STOP_REMINDER_MINUTES = 5

MORNING_STEPS = ("revenue_plan", "shift_headcount", "roster")
EVENING_STEPS = ("revenue", "comment")

PLAN_PARTIAL_PCT = 85  # >=85% — частично, >=100% — выполнен


def parse_amount(raw: str) -> int | None:
    digits = re.sub(r"[^\d]", "", (raw or "").strip())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def format_money(amount: int | None) -> str:
    if amount is None:
        return "—"
    return f"{amount:,}".replace(",", " ") + " ₽"


def compute_plan_status(
    plan_amount: int | None, actual_amount: int | None
) -> tuple[str | None, float | None]:
    if not plan_amount or plan_amount <= 0 or actual_amount is None:
        return None, None
    pct = 100.0 * actual_amount / plan_amount
    if pct >= 100:
        return PLAN_MET_YES, pct
    if pct >= PLAN_PARTIAL_PCT:
        return PLAN_MET_PARTIAL, pct
    return PLAN_MET_NO, pct


def _morning_revenue_plan(morning: dict[str, Any]) -> int | None:
    v = parse_amount(str(morning.get("revenue_plan") or ""))
    if v is not None:
        return v
    legacy = (morning.get("plan") or "").strip()
    return parse_amount(legacy)


def morning_complete(morning: dict[str, Any]) -> bool:
    plan = _morning_revenue_plan(morning)
    try:
        hc = int(morning.get("shift_headcount") or 0)
    except (TypeError, ValueError):
        hc = 0
    return plan is not None and plan > 0 and hc > 0


def evening_complete(evening: dict[str, Any]) -> bool:
    return parse_amount(str(evening.get("revenue") or "")) is not None


def format_morning_group_post(
    chat_title: str,
    *,
    revenue_plan: int | None,
    shift_headcount: int,
    chef_stop_text: str = "",
    roster: str,
) -> str:
    lines = [
        f"☀️ <b>План дня</b> · {escape(chat_title)}",
        "",
        f"<b>План по выручке:</b> {escape(format_money(revenue_plan))}",
        f"<b>На смене:</b> {shift_headcount} чел.",
    ]
    stop = (chef_stop_text or "").strip()
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
    plan_met: str | None,
    revenue_plan: int | None,
    revenue_actual: int | None,
    revenue_pct: float | None,
    comment: str,
    morning_posted: bool,
) -> str:
    lines = [f"🌙 <b>Итоги дня</b> · {escape(chat_title)}", ""]
    if plan_met and revenue_pct is not None:
        met_ru = PLAN_MET_RU.get(plan_met, "—")
        lines.append(f"<b>План:</b> {escape(met_ru)} · <b>{revenue_pct:.0f}%</b>")
    elif plan_met:
        met_ru = PLAN_MET_RU.get(plan_met, "—")
        lines.append(f"<b>План:</b> {escape(met_ru)}")
    elif revenue_actual is not None:
        lines.append("<b>План:</b> <i>утренний план по выручке не задан</i>")
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
    revenue_plan: int,
    shift_headcount: int,
    roster: str,
    by_uid: int,
) -> None:
    rec = _chat_rec(data, chat_id)
    m = get_morning(rec, day)
    m.update(
        {
            "revenue_plan": revenue_plan,
            "shift_headcount": shift_headcount,
            "roster": roster.strip(),
            "by_uid": by_uid,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    _prune_old_days(rec)


def get_chef_stop(rec: dict[str, Any], day: str) -> dict[str, Any]:
    return _day_bucket(rec, day).setdefault("chef_stop", {})


def chef_stop_has_content(stop: dict[str, Any]) -> bool:
    text = (stop.get("text") or "").strip()
    return bool(text) and text not in ("—", "-", "нет", "нет стопа")


def save_chef_stop(
    data: dict[str, Any],
    chat_id: int,
    *,
    day: str,
    text: str,
    by_uid: int,
) -> dict[str, Any]:
    rec = _chat_rec(data, chat_id)
    stop = get_chef_stop(rec, day)
    prev_version = int(stop.get("version") or 0)
    version = prev_version + 1 if chef_stop_has_content(stop) else 1
    stop.update(
        {
            "text": text.strip(),
            "version": version,
            "by_uid": by_uid,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    _prune_old_days(rec)
    return stop


def append_chef_stop(
    data: dict[str, Any],
    chat_id: int,
    *,
    day: str,
    addition: str,
    by_uid: int,
) -> dict[str, Any]:
    rec = _chat_rec(data, chat_id)
    stop = get_chef_stop(rec, day)
    existing = str(stop.get("text") or "").strip() if chef_stop_has_content(stop) else ""
    piece = addition.strip()
    if existing:
        text = f"{existing}\n{piece}"
    else:
        text = piece
    return save_chef_stop(data, chat_id, day=day, text=text, by_uid=by_uid)


def format_chef_stop_group_post(
    chat_title: str, text: str, *, updated: bool = False
) -> str:
    tag = " (обновление)" if updated else ""
    return (
        f"🛑 <b>Стоп-лист{tag}</b> · {escape(chat_title)}\n\n"
        f"{escape(text.strip())}"
    )


def chef_stop_text_for_day(rec: dict[str, Any], day: str) -> str:
    stop = get_chef_stop(rec, day)
    if chef_stop_has_content(stop):
        return str(stop.get("text") or "")
    morning = get_morning(rec, day)
    legacy = (morning.get("stop_list") or "").strip()
    return legacy


def save_evening_draft(
    data: dict[str, Any],
    chat_id: int,
    *,
    day: str,
    revenue: str,
    comment: str,
    by_uid: int,
) -> None:
    rec = _chat_rec(data, chat_id)
    morning = get_morning(rec, day)
    plan_amount = _morning_revenue_plan(morning)
    actual = parse_amount(revenue)
    plan_met, pct = compute_plan_status(plan_amount, actual)
    ev = get_evening(rec, day)
    ev.update(
        {
            "revenue": revenue.strip(),
            "revenue_actual": actual,
            "revenue_pct": round(pct, 1) if pct is not None else None,
            "plan_met": plan_met,
            "comment": comment.strip(),
            "by_uid": by_uid,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    _prune_old_days(rec)


def mark_morning_posted(rec: dict[str, Any], day: str) -> None:
    m = get_morning(rec, day)
    m["posted"] = True
    m["posted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")


def mark_evening_posted(rec: dict[str, Any], day: str) -> None:
    ev = get_evening(rec, day)
    ev["posted"] = True
    ev["posted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")


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
        "chef_stop_deadline": str(
            raw.get("chef_stop_deadline") or DEFAULT_CHEF_STOP_DEADLINE
        ),
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


def get_day_bucket(rec: dict[str, Any], day: str) -> dict[str, Any]:
    return _day_bucket(rec, day)


def day_was_opened(rec: dict[str, Any], day: str) -> bool:
    bucket = get_day_bucket(rec, day)
    if bucket.get("shift_opened_at"):
        return True
    return bool(get_morning(rec, day).get("posted"))


def day_was_closed(rec: dict[str, Any], day: str) -> bool:
    bucket = get_day_bucket(rec, day)
    if bucket.get("shift_closed_at"):
        return True
    return bool(get_evening(rec, day).get("posted"))


def consecutive_unopened_days(rec: dict[str, Any], tz_name: str, *, lookback: int = 30) -> int:
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    d = datetime.now(tz).date() - timedelta(days=1)
    streak = 0
    for _ in range(lookback):
        if day_was_opened(rec, d.isoformat()):
            break
        streak += 1
        d -= timedelta(days=1)
    return streak


def shift_discipline_counts(
    rec: dict[str, Any], start: datetime, end: datetime, tz_name: str
) -> tuple[int, int, int]:
    """(дней без открытия, дней без закрытия, всего дней в периоде)."""
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    cur = start.astimezone(tz).date()
    end_d = end.astimezone(tz).date()
    unopened = 0
    unclosed = 0
    total = 0
    while cur <= end_d:
        total += 1
        day = cur.isoformat()
        opened = day_was_opened(rec, day)
        closed = day_was_closed(rec, day)
        if not opened:
            unopened += 1
        elif not closed:
            unclosed += 1
        cur += timedelta(days=1)
    return unopened, unclosed, total


def format_shift_discipline_line(unopened: int, unclosed: int, total: int) -> str:
    if total <= 0:
        return ""
    return (
        f"<b>Дисциплина смен:</b> без открытия <b>{unopened}</b> · "
        f"без закрытия <b>{unclosed}</b> · дней в периоде <b>{total}</b>"
    )


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


def find_publishable_morning(
    rec: dict[str, Any], prefer_day: str
) -> tuple[str, dict[str, Any]] | None:
    """Черновик плана для публикации: prefer_day или последний неопубликованный."""
    m = get_morning(rec, prefer_day)
    if morning_complete(m) and not m.get("posted"):
        return prefer_day, m
    days = rec.get("ops_days")
    if not isinstance(days, dict):
        return None
    best: tuple[str, dict[str, Any]] | None = None
    for day_key, bucket in days.items():
        if not isinstance(bucket, dict):
            continue
        candidate = bucket.get("morning")
        if not isinstance(candidate, dict):
            continue
        if not morning_complete(candidate) or candidate.get("posted"):
            continue
        if best is None or day_key > best[0]:
            best = (day_key, candidate)
    return best


def find_publishable_evening(
    rec: dict[str, Any], prefer_day: str
) -> tuple[str, dict[str, Any]] | None:
    """Черновик закрытия для публикации: prefer_day или последний неопубликованный."""
    ev = get_evening(rec, prefer_day)
    if evening_complete(ev) and not ev.get("posted"):
        return prefer_day, ev
    days = rec.get("ops_days")
    if not isinstance(days, dict):
        return None
    best: tuple[str, dict[str, Any]] | None = None
    for day_key, bucket in days.items():
        if not isinstance(bucket, dict):
            continue
        candidate = bucket.get("evening")
        if not isinstance(candidate, dict):
            continue
        if not evening_complete(candidate) or candidate.get("posted"):
            continue
        if best is None or day_key > best[0]:
            best = (day_key, candidate)
    return best


def morning_actions_keyboard(chat_id: int, day: str, *, posted: bool):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    if not posted:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📣 Опубликовать в чат сейчас",
                    callback_data=f"ops:mpub:{cid}:{day}"[:64],
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"ops:medit:{cid}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def evening_actions_keyboard(chat_id: int, day: str, *, posted: bool):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    if not posted:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📣 Опубликовать итог в чат",
                    callback_data=f"ops:epub:{cid}:{day}"[:64],
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
    return pulse_model.can_assign_tasks_role(data, uid)


def list_assignable_locations(
    data: dict[str, Any], assigner_uid: int, *, is_global_admin: bool
) -> list[tuple[int, str]]:
    """(chat_id, название группы) — точки для назначения задания."""
    import report_pulse

    scope = report_pulse.chat_scope_for_user(
        data, assigner_uid, is_global_admin=is_global_admin
    )
    return [(int(cid), str(title)) for cid, title in scope]


def assign_task_locations_keyboard(locations: list[tuple[int, str]]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [
            InlineKeyboardButton(
                text=f"📍 {title[:40]}",
                callback_data=f"ops:aloc:{cid}"[:64],
            )
        ]
        for cid, title in locations[:20]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def assign_tasks_to_location(
    data: dict[str, Any],
    *,
    from_uid: int,
    chat_id: int,
    text: str,
) -> list[dict[str, Any]]:
    mgrs = problems_pulse.managers_for_chat(data, chat_id)
    if not mgrs:
        mgrs = [from_uid]
    created = []
    for to_uid in mgrs:
        created.append(
            assign_task(
                data,
                from_uid=from_uid,
                to_uid=int(to_uid),
                chat_id=chat_id,
                text=text,
            )
        )
    return created


def chat_title(data: dict[str, Any], chat_id: int | str | None) -> str:
    if chat_id is None:
        return ""
    rec = data.get("chats", {}).get(str(chat_id), {})
    if isinstance(rec, dict) and rec.get("title"):
        return str(rec["title"])
    return str(chat_id)


def format_engagement_shift_line(raters: int, expected: int) -> str:
    if expected <= 0:
        if raters > 0:
            return f"<b>Вовлечённость:</b> ответили <b>{raters}</b> (число на смене не задано)"
        return "<b>Вовлечённость:</b> <i>укажите число на смене в «Плане дня»</i>"
    pct = min(100, round(100 * raters / expected))
    quiet = " · ⚠️ низкий отклик" if pct < 50 and expected >= 3 else ""
    return (
        f"<b>Вовлечённость:</b> <b>{pct}%</b> "
        f"({raters} из {expected} на смене){quiet}"
    )


async def engagement_line_for_shift_day(
    data: dict[str, Any],
    chat_id: int,
    day: str,
    *,
    jsonl_path,
    tz_name: str,
) -> str:
    rec = data.get("chats", {}).get(str(chat_id), {})
    morning = get_morning(rec if isinstance(rec, dict) else {}, day)
    try:
        expected = int(morning.get("shift_headcount") or 0)
    except (TypeError, ValueError):
        expected = 0
    if expected <= 0:
        return format_engagement_shift_line(0, 0)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    from datetime import date as date_cls

    d = date_cls.fromisoformat(day)
    start = datetime.combine(d, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    import admin_metrics

    raters = (
        await admin_metrics.count_raters_in_period(
            [chat_id], start, end, jsonl_path=jsonl_path
        )
    ).get(str(chat_id), 0)
    return format_engagement_shift_line(raters, expected)


async def engagement_line_for_period(
    data: dict[str, Any],
    chat_id: int,
    start: datetime,
    end: datetime,
    *,
    jsonl_path,
) -> str | None:
    rec = data.get("chats", {}).get(str(chat_id), {})
    days_map = rec.get("ops_days", {}) if isinstance(rec, dict) else {}
    if not isinstance(days_map, dict):
        return None
    import admin_metrics
    from datetime import date as date_cls

    daily_pcts: list[int] = []
    total_raters = 0
    total_expected = 0
    for day_key, bucket in days_map.items():
        try:
            d = date_cls.fromisoformat(day_key)
        except ValueError:
            continue
        morning = (bucket or {}).get("morning", {}) if isinstance(bucket, dict) else {}
        try:
            hc = int(morning.get("shift_headcount") or 0)
        except (TypeError, ValueError):
            continue
        if hc <= 0:
            continue
        tz = start.tzinfo or ZoneInfo("Europe/Moscow")
        day_start = datetime.combine(d, time.min, tzinfo=tz)
        if day_start < start or day_start > end:
            continue
        day_end = day_start + timedelta(days=1)
        raters = (
            await admin_metrics.count_raters_in_period(
                [chat_id], day_start, day_end, jsonl_path=jsonl_path
            )
        ).get(str(chat_id), 0)
        total_raters += raters
        total_expected += hc
        daily_pcts.append(min(100, round(100 * raters / hc)))
    if not daily_pcts:
        return None
    avg = round(sum(daily_pcts) / len(daily_pcts))
    return (
        f"<b>Вовлечённость:</b> <b>{avg}%</b> в среднем за смену "
        f"({total_raters} ответов / {total_expected} чел·смен, {len(daily_pcts)} дн.)"
    )


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


def format_tasks_list(
    tasks: list[dict], data: dict[str, Any] | None = None, *, title: str = "Задания"
) -> str:
    if not tasks:
        return f"<b>{escape(title)}</b>\n\n<i>Открытых заданий нет.</i>"
    lines = [f"<b>{escape(title)}</b>", ""]
    for t in tasks[:15]:
        dl = t.get("deadline")
        dl_s = f" · до {escape(str(dl))}" if dl else ""
        loc = ""
        if data and t.get("chat_id"):
            lt = chat_title(data, t.get("chat_id"))
            if lt:
                loc = f"📍 {escape(lt)} · "
        lines.append(f"• {loc}{escape(str(t.get('text', '')))}{dl_s}")
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
        rp = _morning_revenue_plan(morning)
        hc = morning.get("shift_headcount", "—")
        lines.append(
            f"<b>Утренний план:</b> {posted} · "
            f"план {escape(format_money(rp))} · на смене {hc} чел."
        )
    else:
        lines.append("<b>Утренний план:</b> не заполнен")

    if evening_complete(evening):
        met = PLAN_MET_RU.get(str(evening.get("plan_met")), "—")
        pct = evening.get("revenue_pct")
        pct_s = f" · <b>{pct}%</b>" if pct is not None else ""
        rev = format_money(parse_amount(str(evening.get("revenue") or "")))
        posted_e = " · в чате" if evening.get("posted") else ""
        if evening.get("plan_met"):
            lines.append(f"<b>Закрытие дня:</b> {escape(met)} · {escape(rev)}{pct_s}{posted_e}")
        else:
            lines.append(f"<b>Закрытие дня:</b> {escape(rev)}{posted_e}")
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
