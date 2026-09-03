"""
Mini App: горящие, наставник, отчёт, организации.
Обёртки над problems_pulse / ai_advisor / report_pulse / pulse_model.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import ai_advisor
import manager_alerts as ma
import problems_pulse
import pulse_model
import report_pulse


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    text = re.sub(r"</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def problem_public(p: problems_pulse.ProblemRow) -> dict[str, Any]:
    return {
        "id": p.id,
        "title": p.title,
        "status": p.status,
        "status_ru": problems_pulse.STATUS_RU.get(p.status, p.status),
        "mentions": p.mentions_count,
        "chat_id": str(p.restaurant_chat_id),
        "problem_key": p.problem_key,
        "manager_comment": p.manager_comment or "",
        "first_detected_at": p.first_detected_at.isoformat() if p.first_detected_at else None,
        "last_detected_at": p.last_detected_at.isoformat() if p.last_detected_at else None,
        "card_text": _strip_html(problems_pulse.format_problem_card(p)),
    }


def theme_code_from_problem_key(problem_key: str) -> str:
    key = (problem_key or "").strip()
    if key.startswith("ai_"):
        key = key[3:]
    if key.startswith("manual_"):
        return "comment_trend"
    return key or "comment_trend"


async def list_hot_problems(
    data: dict[str, Any],
    chat_id: int,
    *,
    view: str = problems_pulse.VIEW_ACTIVE,
    sync: bool = False,
    jsonl_path: Path | None = None,
) -> dict[str, Any]:
    rec = data.get("chats", {}).get(str(chat_id)) or {}
    sync_note = None
    if sync:
        try:
            await problems_pulse.sync_problems_from_period(
                data,
                chat_id,
                rec.get("organization_id"),
                jsonl_path=jsonl_path,
                tz_name=rec.get("timezone") or "Europe/Moscow",
                days=problems_pulse.SIGNALS_SYNC_DAYS,
            )
        except Exception as e:
            print(f"[miniapp-hot-sync] {chat_id}: {e}")
            sync_note = "Не удалось обновить из отзывов — показан сохранённый список."
    # siblings: зал+кухня одной точки
    rows: list[problems_pulse.ProblemRow] = []
    for cid in pulse_model.sibling_chat_ids_for_location(data, chat_id):
        part = await problems_pulse.list_problems_for_chat(
            data, cid, include_ignored=True, view=view
        )
        rows.extend(part)
    rows.sort(
        key=lambda p: (
            0 if p.status == problems_pulse.STATUS_NEW else 1,
            -(p.mentions_count or 0),
            p.title or "",
        )
    )
    return {
        "ok": True,
        "chat_id": str(chat_id),
        "title": str(rec.get("title") or chat_id),
        "view": view,
        "sync_note": sync_note,
        "problems": [problem_public(p) for p in rows[:40]],
        "statuses": [
            {"id": problems_pulse.STATUS_NEW, "label": problems_pulse.STATUS_RU[problems_pulse.STATUS_NEW]},
            {
                "id": problems_pulse.STATUS_IN_PROGRESS,
                "label": problems_pulse.STATUS_RU[problems_pulse.STATUS_IN_PROGRESS],
            },
            {
                "id": problems_pulse.STATUS_RESOLVED,
                "label": problems_pulse.STATUS_RU[problems_pulse.STATUS_RESOLVED],
            },
            {
                "id": problems_pulse.STATUS_IGNORED,
                "label": problems_pulse.STATUS_RU[problems_pulse.STATUS_IGNORED],
            },
        ],
    }


async def set_problem_status(
    data: dict[str, Any],
    problem_id: str,
    status: str,
    *,
    comment: str | None = None,
) -> dict[str, Any]:
    updated = await problems_pulse.update_problem_status(
        data, problem_id, status, comment
    )
    if not updated:
        return {"ok": False, "error": "Не удалось обновить статус"}
    return {"ok": True, "problem": problem_public(updated)}


async def mentor_for_problem(
    data: dict[str, Any],
    problem_id: str,
    *,
    jsonl_path: Path | None = None,
) -> dict[str, Any]:
    prob = await problems_pulse.get_problem(data, problem_id)
    if not prob:
        return {"ok": False, "error": "Тема не найдена"}
    chat_id = prob.restaurant_chat_id
    rec = data.get("chats", {}).get(str(chat_id)) or {}
    title = str(rec.get("title") or chat_id)
    tz_name = str(rec.get("timezone") or "Europe/Moscow")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    window = timedelta(hours=ma.ALERT_WINDOW_HOURS)
    cur_events = await report_pulse.load_events(
        [chat_id], now - window, now, jsonl_path=jsonl_path
    )
    theme = theme_code_from_problem_key(prob.problem_key)
    scoped = ai_advisor.filter_events_for_theme(
        cur_events, theme, problem_key=prob.problem_key
    )
    comments = ai_advisor.extract_comments(scoped, limit=8)
    button_n = sum(
        1
        for e in scoped
        if e.event_type == report_pulse.EVENT_PROBLEM
        and (e.problem_code or "") in {theme, prob.problem_key}
    )
    if not comments and button_n <= 0 and prob.mentions_count <= 0:
        return {
            "ok": False,
            "error": (
                f"По теме «{prob.title}» пока мало отзывов для разбора. "
                "Когда линия отметит кнопку или напишет комментарий — наставник разберёт её."
            ),
        }
    alert = ma.ManagerAlert(
        kind="comment_trend",
        code=theme if theme in ai_advisor.PROBLEM_RU else "comment_trend",
        title=prob.title,
        body_lines=[
            f"Выбрана тема «Горящих»: {prob.title}.",
            f"Отметок по теме: {max(prob.mentions_count, button_n)}.",
            "Разбирай ТОЛЬКО эту тему, не соседние.",
        ],
        recommendation=ma.RECOMMENDATIONS.get(theme, ma.RECOMMENDATIONS["rating_drop"]),
        comments=comments[:4],
        priority=1,
        problem_key=theme or prob.problem_key,
    )
    pack = None
    used_template = False
    if ai_advisor._client_or_none() is None:
        html = ai_advisor.template_mentor_advice(alert, restaurant_title=title)
        pack = ai_advisor.AdvicePack(
            text=html,
            learn=ai_advisor.fallback_learn_more(theme),
            theme_code=theme,
        )
        used_template = True
    else:
        try:
            pack = await ai_advisor.build_advice(
                alert,
                restaurant_title=title,
                events=scoped,
                lock_theme=True,
            )
        except Exception as e:
            print(f"[miniapp-mentor] {e}")
            pack = None
    if not pack:
        return {
            "ok": False,
            "error": "Не удалось получить совет. Проверьте OPENAI_API_KEY на Railway.",
        }
    learn = None
    if pack.learn:
        learn = {
            "title": pack.learn.title,
            "blurb": pack.learn.blurb,
            "reference": pack.learn.reference,
            "kind": pack.learn.kind,
        }
    return {
        "ok": True,
        "problem_id": prob.id,
        "problem_title": prob.title,
        "restaurant_title": title,
        "html": pack.text,
        "text": _strip_html(pack.text),
        "learn": learn,
        "template": used_template,
    }


async def mentor_for_chat(
    data: dict[str, Any],
    chat_id: int,
    *,
    period: str = report_pulse.PERIOD_WEEK,
    jsonl_path: Path | None = None,
) -> dict[str, Any]:
    """Наставник по точке: берём первую активную горящую или общий разбор недели."""
    hot = await list_hot_problems(
        data, chat_id, view=problems_pulse.VIEW_ACTIVE, sync=True, jsonl_path=jsonl_path
    )
    problems = hot.get("problems") or []
    if problems:
        return await mentor_for_problem(
            data, str(problems[0]["id"]), jsonl_path=jsonl_path
        )
    rec = data.get("chats", {}).get(str(chat_id)) or {}
    title = str(rec.get("title") or chat_id)
    tz = str(rec.get("timezone") or "Europe/Moscow")
    start, end, prev_start, prev_end, plabel = report_pulse.period_window(period, tz)
    cids = pulse_model.sibling_chat_ids_for_location(data, chat_id)
    events = await report_pulse.load_events(cids, start, end, jsonl_path=jsonl_path)
    prev = await report_pulse.load_events(cids, prev_start, prev_end, jsonl_path=jsonl_path)
    if not events:
        return {
            "ok": False,
            "error": "Пока нет отзывов за период — наставнику не на что опереться.",
        }
    pack = await ai_advisor.build_advice_from_events(
        events,
        prev,
        restaurant_title=title,
        data=data,
        chat_id=chat_id,
    )
    if not pack:
        # шаблонный разбор, если OpenAI недоступен
        comments = ai_advisor.extract_comments(events, limit=5)
        alert = ma.ManagerAlert(
            kind="comment_trend",
            code="comment_trend",
            title=f"Разбор · {plabel}",
            body_lines=[f"Период: {plabel}", f"Событий: {len(events)}"],
            recommendation=ma.RECOMMENDATIONS.get("rating_drop", "Разберите повторы в отзывах."),
            comments=comments,
            priority=2,
            problem_key="comment_trend",
        )
        html = ai_advisor.template_mentor_advice(alert, restaurant_title=title)
        return {
            "ok": True,
            "problem_id": None,
            "problem_title": f"Обзор · {plabel}",
            "restaurant_title": title,
            "html": html,
            "text": _strip_html(html),
            "learn": {
                "title": "Регулярный разбор",
                "blurb": "Смотрите горящие темы и повторяйте короткие разборы после смены.",
                "reference": "",
                "kind": "template",
            },
            "template": True,
        }
    learn = None
    if pack.learn:
        learn = {
            "title": pack.learn.title,
            "blurb": pack.learn.blurb,
            "reference": pack.learn.reference,
            "kind": pack.learn.kind,
        }
    return {
        "ok": True,
        "problem_id": None,
        "problem_title": f"Обзор · {plabel}",
        "restaurant_title": title,
        "html": pack.text,
        "text": _strip_html(pack.text),
        "learn": learn,
        "template": False,
    }


async def report_payload(
    data: dict[str, Any],
    user_id: int,
    *,
    is_global_admin: bool,
    chat_id: int | None,
    period: str = report_pulse.PERIOD_WEEK,
    department: str | None = None,
    jsonl_path: Path | None = None,
) -> dict[str, Any]:
    import chef_survey

    rec = data.get("chats", {}).get(str(chat_id)) if chat_id is not None else {}
    tz = str((rec or {}).get("timezone") or "Europe/Moscow")
    dept = department or chef_survey.DEPARTMENT_ALL
    result = await report_pulse.build_reports_for_manager(
        data,
        user_id,
        period,
        is_global_admin=is_global_admin,
        tz_name=tz,
        jsonl_path=jsonl_path,
        selected_chat=str(chat_id) if chat_id is not None else None,
        department=dept,
    )
    blocks = []
    for html in result.messages:
        blocks.append({"html": html, "text": _strip_html(html)})
    return {
        "ok": True,
        "chat_id": str(chat_id) if chat_id is not None else None,
        "period": period,
        "department": dept,
        "blocks": blocks,
        "title": str((rec or {}).get("title") or "Отчёт"),
    }


def orgs_list(data: dict[str, Any], *, is_global_admin: bool, user_id: int) -> list[dict[str, Any]]:
    orgs = data.get("organizations") or {}
    out: list[dict[str, Any]] = []
    if is_global_admin:
        items = list(orgs.items())
    else:
        allowed = set()
        for p in pulse_model.manager_profiles(data, user_id):
            oid = p.get("organization_id")
            if oid:
                allowed.add(str(oid))
        items = [(oid, orgs[oid]) for oid in allowed if oid in orgs]
    for oid, org in sorted(items, key=lambda x: str((x[1] or {}).get("name") or x[0])):
        if not isinstance(org, dict):
            continue
        chats = []
        for cid, rec in pulse_model.list_chats_for_org(data, oid):
            if rec.get("removed_at") or rec.get("active") is False:
                continue
            chats.append(
                {
                    "id": cid,
                    "title": str(rec.get("title") or cid),
                    "department": pulse_model.chat_department(data, int(cid)),
                }
            )
        out.append(
            {
                "id": oid,
                "name": str(org.get("name") or oid),
                "subscription": str(org.get("subscription") or pulse_model.SUB_ACTIVE),
                "chats": chats,
            }
        )
    return out


def manager_org_ids(data: dict[str, Any], user_id: int) -> set[str]:
    out: set[str] = set()
    for p in pulse_model.manager_profiles(data, user_id):
        if isinstance(p, dict) and p.get("organization_id"):
            role = p.get("role")
            if role in (
                pulse_model.ROLE_NETWORK_ADMIN,
                pulse_model.ROLE_SENIOR_MANAGER,
                pulse_model.ROLE_LOCATION_ADMIN,
                pulse_model.ROLE_HAPPINESS_MANAGER,
            ):
                out.add(str(p["organization_id"]))
    return out


def can_link_org_chats(
    data: dict[str, Any], user_id: int, *, is_global_admin: bool, org_id: str | None = None
) -> bool:
    import staff_assign

    if is_global_admin:
        return True
    if not staff_assign.can_manage_staff(data, user_id, is_global_admin=False):
        return False
    if org_id is None:
        return bool(manager_org_ids(data, user_id))
    return str(org_id) in manager_org_ids(data, user_id)


def unlinked_chats(data: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for cid, rec in (data.get("chats") or {}).items():
        if not isinstance(rec, dict):
            continue
        if rec.get("removed_at") or rec.get("active") is False:
            continue
        if rec.get("organization_id"):
            continue
        out.append({"id": cid, "title": str(rec.get("title") or cid)})
    return sorted(out, key=lambda x: x["title"].lower())


def create_org_and_maybe_link(
    data: dict[str, Any],
    *,
    name: str,
    chat_id: int | None = None,
    department: str | None = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Укажите название организации"}
    oid = pulse_model.create_organization(data, name)
    linked = False
    note = None
    if chat_id is not None:
        result = link_existing_chat(
            data, org_id=oid, chat_id=chat_id, department=department
        )
        linked = bool(result.get("ok"))
        note = result.get("note") or result.get("warning")
        if not linked:
            return {
                "ok": True,
                "org_id": oid,
                "name": name,
                "linked": False,
                "warning": "Организация создана, но чат не найден в базе (добавьте бота в группу).",
            }
    return {"ok": True, "org_id": oid, "name": name, "linked": linked, "note": note}


def link_existing_chat(
    data: dict[str, Any],
    *,
    org_id: str,
    chat_id: int,
    department: str | None = None,
    peer_floor_chat_id: int | None = None,
) -> dict[str, Any]:
    if org_id not in (data.get("organizations") or {}):
        return {"ok": False, "error": "Организация не найдена"}
    dept = pulse_model.parse_chat_department(department) if department else None
    ok = pulse_model.link_chat_to_organization(
        data, chat_id, org_id, department=dept
    )
    if not ok:
        return {
            "ok": False,
            "error": "Чат не найден. Сначала добавьте бота в группу точки.",
        }
    note = None
    if dept == pulse_model.CHAT_DEPT_FLOOR or dept is None:
        pulse_model.ensure_chat_location_id(data, chat_id)
        if dept is None:
            note = "Департамент не указан — по умолчанию зал. Можно сменить на кухню."
    elif dept == pulse_model.CHAT_DEPT_KITCHEN:
        floors = [
            (cid, title)
            for cid, title in pulse_model.floor_chats_in_org(data, org_id)
            if cid != chat_id
        ]
        if peer_floor_chat_id is not None:
            pulse_model.pair_chats_same_location(data, chat_id, peer_floor_chat_id)
            note = "Кухня связана с выбранным залом."
        elif len(floors) == 1:
            pulse_model.pair_chats_same_location(data, chat_id, floors[0][0])
            note = f"Кухня связана с залом «{floors[0][1]}»."
        elif not floors:
            pulse_model.ensure_chat_location_id(data, chat_id)
            note = "Чата зала пока нет — подключите зал, потом кухню привяжем к нему."
        else:
            note = "Несколько залов в сети — укажите peer_floor_chat_id или привяжите в группе."
    rec = data.get("chats", {}).get(str(chat_id)) or {}
    return {
        "ok": True,
        "org_id": org_id,
        "chat_id": str(chat_id),
        "title": str(rec.get("title") or chat_id),
        "department": dept or pulse_model.chat_department(data, chat_id),
        "note": note,
        "floor_options": [
            {"id": str(cid), "title": title}
            for cid, title in pulse_model.floor_chats_in_org(data, org_id)
            if cid != chat_id
        ]
        if dept == pulse_model.CHAT_DEPT_KITCHEN
        else [],
    }


def connect_guide(
    data: dict[str, Any], user_id: int, *, is_global_admin: bool, bot_username: str = ""
) -> dict[str, Any]:
    orgs = orgs_list(data, is_global_admin=is_global_admin, user_id=user_id)
    can_link = can_link_org_chats(data, user_id, is_global_admin=is_global_admin)
    steps = [
        "Создайте два групповых чата точки: зал и кухня.",
        "Добавьте бота в оба чата (достаточно один раз).",
        "В Mini App: Доступы → «Подключить чат» — выберите организацию, чат и Зал/Кухня.",
        "Либо в группе напишите /link_org org_id floor или /link_org org_id kitchen.",
    ]
    commands = []
    for o in orgs:
        oid = o["id"]
        commands.append(
            {
                "org_id": oid,
                "name": o["name"],
                "floor": f"/link_org {oid} floor",
                "kitchen": f"/link_org {oid} kitchen",
            }
        )
    un = (bot_username or "").lstrip("@")
    return {
        "ok": True,
        "can_link": can_link,
        "can_create": is_global_admin,
        "steps": steps,
        "orgs": orgs,
        "commands": commands,
        "unlinked_chats": unlinked_chats(data) if can_link else [],
        "add_bot_url": f"https://t.me/{un}?startgroup=open" if un else "",
    }


def staff_at_chat(data: dict[str, Any], chat_id: int) -> dict[str, Any]:
    import staff_assign

    rec = data.get("chats", {}).get(str(chat_id)) or {}
    org_id = str(rec.get("organization_id") or "")
    staff: list[dict[str, Any]] = []
    if org_id:
        for uid, label, code in staff_assign.list_staff_at_location(data, chat_id, org_id):
            staff.append(
                {
                    "user_id": uid,
                    "role_label": label,
                    "role_code": code,
                    "role": staff_assign.ROLE_CODES.get(code) or code,
                }
            )
        for uid in staff_assign.list_network_admins(data, org_id):
            staff.append(
                {
                    "user_id": uid,
                    "role_label": pulse_model.role_label_ru(pulse_model.ROLE_NETWORK_ADMIN),
                    "role_code": "nw",
                    "role": pulse_model.ROLE_NETWORK_ADMIN,
                    "network": True,
                }
            )
    return {
        "ok": True,
        "chat_id": str(chat_id),
        "org_id": org_id,
        "title": str(rec.get("title") or chat_id),
        "staff": staff,
    }