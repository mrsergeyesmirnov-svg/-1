"""
iiko × Pulse: опрос на терминале → закрытие личной смены.
Telegram не закрывает смену и не шлёт людям в личку «иди оцени».
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

DEPT_HALL = "hall"
DEPT_KITCHEN = "kitchen"
BAD_RATING_MAX = 2
BAD_NUDGE_THRESHOLD = 3
NUDGE_COOLDOWN_HOURS = 6

BLOCKERS = [
    {"code": "team", "label": "👥 Команда"},
    {"code": "kitchen", "label": "👨‍🍳 Кухня"},
    {"code": "guests", "label": "🙋 Гости"},
    {"code": "processes", "label": "⚙️ Процессы"},
    {"code": "self", "label": "🧠 Моё состояние"},
    {"code": "ok", "label": "✨ Ничего, все прошло хорошо"},
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def business_date(tz_name: str = "Europe/Moscow") -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date().isoformat()


def ensure_tables(data: dict[str, Any]) -> None:
    if not isinstance(data.get("iiko_staff"), list):
        data["iiko_staff"] = []
    if not isinstance(data.get("iiko_permits"), dict):
        data["iiko_permits"] = {}
    if not isinstance(data.get("iiko_points"), dict):
        data["iiko_points"] = {}
    if not isinstance(data.get("iiko_nudge_sent"), dict):
        data["iiko_nudge_sent"] = {}
    if not isinstance(data.get("iiko_surveys"), list):
        data["iiko_surveys"] = []


def permit_key(employee_id: str, day: str) -> str:
    return f"{employee_id.strip()}:{day}"


def anon_employee(employee_id: str) -> str:
    return hashlib.sha256(employee_id.strip().encode()).hexdigest()[:12]


def bind_point(
    data: dict[str, Any],
    *,
    iiko_org_id: str,
    chat_id: int,
    hall_chat_id: int | None = None,
    kitchen_chat_id: int | None = None,
) -> dict[str, Any]:
    ensure_tables(data)
    oid = iiko_org_id.strip()
    rec = {
        "chat_id": int(chat_id),
        "hall_chat_id": int(hall_chat_id) if hall_chat_id else int(chat_id),
        "kitchen_chat_id": int(kitchen_chat_id) if kitchen_chat_id else int(chat_id),
        "at": _now_iso(),
    }
    data["iiko_points"][oid] = rec
    return rec


def point_for_org(data: dict[str, Any], iiko_org_id: str) -> dict[str, Any] | None:
    ensure_tables(data)
    rec = data["iiko_points"].get((iiko_org_id or "").strip())
    return rec if isinstance(rec, dict) else None


def _dept(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if any(x in s for x in ("kitchen", "кух", "повар", "hot", "cold", "chef")):
        return DEPT_KITCHEN
    return DEPT_HALL


def _norm_emp_id(raw: Any) -> str:
    s = str(raw or "").strip().strip("{}").strip()
    if s.lower() in ("id", "uuid", "guid", "код", "code", "none", "null"):
        return ""
    return s


def _row_emp_id(keys: dict[str, Any]) -> str:
    for k in (
        "id",
        "uuid",
        "guid",
        "employeeid",
        "employee_id",
        "employeeid1",
        "код",
        "code",
        "ид",
        "сотрудникid",
    ):
        got = _norm_emp_id(keys.get(k))
        if got:
            return got
    return ""


def import_employees_csv(data: dict[str, Any], text: str, *, default_chat_id: int | None = None) -> int:
    """
    Пачка из выгрузки iiko (Excel → CSV или копипаст). Telegram не нужен.
    Для закрытия смены справочник не обязателен: касса уже знает PIN.
    CSV только чтобы отличить зал от кухни, если плагин сам не шлёт department.
    """
    ensure_tables(data)
    raw = text.strip().lstrip("\ufeff")
    if not raw:
        return 0
    sample = raw.splitlines()[0]
    semi, comma, tab = sample.count(";"), sample.count(","), sample.count("\t")
    delimiter = ";" if semi >= max(comma, tab) else ("\t" if tab > comma else ",")
    reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
    added = 0
    by_id = {
        str(r.get("iiko_employee_id")): i
        for i, r in enumerate(data["iiko_staff"])
        if isinstance(r, dict) and r.get("iiko_employee_id")
    }
    for row in reader:
        keys = {str(k).strip().lower().replace(" ", ""): v for k, v in row.items() if k}
        emp = _row_emp_id(keys)
        if not emp:
            continue
        name = str(
            keys.get("name")
            or keys.get("фио")
            or keys.get("fio")
            or keys.get("имя")
            or keys.get("сотрудник")
            or ""
        ).strip()
        dept = _dept(
            str(
                keys.get("department")
                or keys.get("должность")
                or keys.get("role")
                or keys.get("job")
                or keys.get("роль")
                or ""
            )
        )
        rec = {
            "iiko_employee_id": emp,
            "name": name,
            "role": dept,
            "chat_id": default_chat_id,
            "telegram_id": None,
            "linked_at": _now_iso(),
        }
        if emp in by_id:
            prev = data["iiko_staff"][by_id[emp]]
            if isinstance(prev, dict) and not name:
                rec["name"] = str(prev.get("name") or "")
            data["iiko_staff"][by_id[emp]] = rec
        else:
            data["iiko_staff"].append(rec)
            by_id[emp] = len(data["iiko_staff"]) - 1
        added += 1
    return added


def staff_dept(data: dict[str, Any], employee_id: str, fallback: str | None = None) -> str:
    ensure_tables(data)
    emp = (employee_id or "").strip()
    for r in data["iiko_staff"]:
        if isinstance(r, dict) and str(r.get("iiko_employee_id") or "") == emp:
            return _dept(str(r.get("role") or fallback or DEPT_HALL))
    return _dept(fallback or DEPT_HALL)


def submit_shift_survey(
    data: dict[str, Any],
    *,
    employee_id: str,
    rating: int,
    blocker: str,
    iiko_org_id: str,
    department: str | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Опрос с терминала. Без него permit нет → плагин не закрывает смену."""
    ensure_tables(data)
    emp = (employee_id or "").strip()
    if not emp:
        raise ValueError("employeeId required")
    rating = int(rating)
    if rating < 1 or rating > 5:
        raise ValueError("rating 1..5")
    point = point_for_org(data, iiko_org_id)
    if not point:
        raise KeyError("unknown iiko organization — /iiko_point")
    day = day or business_date()
    dept = staff_dept(data, emp, department)
    blocker = (blocker or "ok").strip() or "ok"
    rec = {
        "ok": True,
        "iiko_employee_id": emp,
        "anon": anon_employee(emp),
        "chat_id": int(point["chat_id"]),
        "business_date": day,
        "rating": rating,
        "blocker": blocker,
        "department": dept,
        "at": _now_iso(),
    }
    data["iiko_permits"][permit_key(emp, day)] = rec
    log = data.setdefault("iiko_surveys", [])
    if not isinstance(log, list):
        log = []
        data["iiko_surveys"] = log
    log.append(rec)
    return rec


def clockout_permit(data: dict[str, Any], employee_id: str, day: str | None = None) -> dict[str, Any]:
    ensure_tables(data)
    day = day or business_date()
    rec = data["iiko_permits"].get(permit_key(employee_id, day))
    if isinstance(rec, dict) and rec.get("ok"):
        return {"ok": True, "employeeId": employee_id, "date": day, "at": rec.get("at")}
    return {"ok": False, "employeeId": employee_id, "date": day}


def count_bad_today(data: dict[str, Any], *, chat_id: int, department: str, day: str | None = None) -> int:
    ensure_tables(data)
    day = day or business_date()
    n = 0
    for rec in data.get("iiko_surveys") or []:
        if not isinstance(rec, dict):
            continue
        if rec.get("business_date") != day:
            continue
        if int(rec.get("chat_id") or 0) != int(chat_id):
            continue
        if rec.get("department") != department:
            continue
        if int(rec.get("rating") or 5) <= BAD_RATING_MAX:
            n += 1
    return n


def take_nudge(
    data: dict[str, Any],
    *,
    chat_id: int,
    department: str,
    day: str | None = None,
) -> dict[str, Any] | None:
    """Если за день ≥3 плохих оценок по залу или кухне — один анонимный дожим в чат."""
    ensure_tables(data)
    day = day or business_date()
    bad = count_bad_today(data, chat_id=chat_id, department=department, day=day)
    if bad < BAD_NUDGE_THRESHOLD:
        return None
    key = f"{chat_id}:{department}:{day}"
    if data["iiko_nudge_sent"].get(key):
        return None
    data["iiko_nudge_sent"][key] = _now_iso()
    point = None
    for rec in (data.get("iiko_points") or {}).values():
        if isinstance(rec, dict) and int(rec.get("chat_id") or 0) == int(chat_id):
            point = rec
            break
    target = int(chat_id)
    if point:
        if department == DEPT_KITCHEN:
            target = int(point.get("kitchen_chat_id") or chat_id)
        else:
            target = int(point.get("hall_chat_id") or chat_id)
    where = "кухне" if department == DEPT_KITCHEN else "залу"
    text = (
        f"Поймали, что на {where} что-то идёт не так — без имён.\n"
        "Расскажите подробнее в личке с ботом."
    )
    return {
        "chat_id": target,
        "link_chat_id": target,
        "department": department,
        "bad": bad,
        "text": text,
    }


def survey_event(rec: dict[str, Any]) -> dict[str, Any]:
    """Событие в лог Pulse: без имени, с анонимным ключом."""
    return {
        "event": "rating",
        "user_id": None,
        "anon_staff": rec.get("anon"),
        "restaurant_chat_id": rec.get("chat_id"),
        "rating": rec.get("rating"),
        "problem": rec.get("blocker") if rec.get("blocker") != "ok" else None,
        "department": "kitchen" if rec.get("department") == DEPT_KITCHEN else "floor",
        "source": "iiko_terminal",
    }


def api_key_ok(provided: str | None, expected: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def make_aiohttp_app(load_data, save_data, *, api_key: str, on_survey=None):
    from aiohttp import web

    def _auth(request) -> bool:
        got = request.headers.get("X-Iiko-Key") or request.query.get("key")
        return api_key_ok(got, api_key)

    async def options(request: web.Request) -> web.Response:
        if not _auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"rating": [1, 2, 3, 4, 5], "blockers": BLOCKERS})

    async def survey(request: web.Request) -> web.Response:
        if not _auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "json required"}, status=400)
        data = await load_data()
        try:
            rec = submit_shift_survey(
                data,
                employee_id=str(body.get("employeeId") or ""),
                rating=int(body.get("rating") or 0),
                blocker=str(body.get("blocker") or "ok"),
                iiko_org_id=str(body.get("organizationId") or body.get("orgId") or ""),
                department=body.get("department"),
            )
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except KeyError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        nudge = take_nudge(
            data,
            chat_id=int(rec["chat_id"]),
            department=str(rec["department"]),
            day=str(rec["business_date"]),
        )
        await save_data(data)
        if on_survey:
            await on_survey(survey_event(rec), nudge)
        return web.json_response({"ok": True, "closeShift": True})

    async def clockout(request: web.Request) -> web.Response:
        if not _auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        emp = str(request.query.get("employeeId") or "").strip()
        if not emp:
            return web.json_response({"error": "employeeId required"}, status=400)
        data = await load_data()
        return web.json_response(clockout_permit(data, emp))

    app = web.Application()
    app.router.add_get("/v1/iiko/survey-options", options)
    app.router.add_post("/v1/iiko/shift-survey", survey)
    app.router.add_get("/v1/iiko/clockout-permit", clockout)
    return app


async def start_http_server(load_data, save_data, *, api_key: str, on_survey=None) -> None:
    from aiohttp import web

    host = os.getenv("IIKO_HTTP_HOST", "127.0.0.1")
    port = int(os.getenv("IIKO_HTTP_PORT", "8091"))
    app = make_aiohttp_app(load_data, save_data, api_key=api_key, on_survey=on_survey)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"[iiko-http] {host}:{port}  POST /v1/iiko/shift-survey  GET /v1/iiko/clockout-permit")
