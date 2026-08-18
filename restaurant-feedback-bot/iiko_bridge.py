"""
iiko × Pulse — слой вовлечённости (шаг 1).

Справочник Telegram ↔ сотрудник iiko, разрешение закрыть личную смену
после короткой отметки в боте, HTTP для кассы/плагина.

Шаг 1 не ставит замок на терминал. Только карта людей + permit + API.
"""
from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

ROLE_FLOOR = "floor"
ROLE_KITCHEN = "kitchen"


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
    if not isinstance(data.get("iiko_out_tokens"), dict):
        data["iiko_out_tokens"] = {}


def permit_key(employee_id: str, day: str) -> str:
    return f"{employee_id.strip()}:{day}"


def upsert_staff(
    data: dict[str, Any],
    *,
    telegram_id: int,
    iiko_employee_id: str,
    chat_id: int,
    role: str = ROLE_FLOOR,
) -> dict[str, Any]:
    ensure_tables(data)
    emp = iiko_employee_id.strip()
    if not emp:
        raise ValueError("empty iiko_employee_id")
    role = ROLE_KITCHEN if role == ROLE_KITCHEN else ROLE_FLOOR
    row = {
        "telegram_id": int(telegram_id),
        "iiko_employee_id": emp,
        "chat_id": int(chat_id),
        "role": role,
        "linked_at": _now_iso(),
    }
    staff: list[dict[str, Any]] = data["iiko_staff"]
    replaced = False
    for i, old in enumerate(staff):
        if not isinstance(old, dict):
            continue
        if int(old.get("telegram_id") or 0) == int(telegram_id) or str(
            old.get("iiko_employee_id") or ""
        ) == emp:
            staff[i] = row
            replaced = True
            break
    if not replaced:
        staff.append(row)
    return row


def unlink_staff(data: dict[str, Any], *, telegram_id: int | None = None, iiko_employee_id: str | None = None) -> int:
    ensure_tables(data)
    before = len(data["iiko_staff"])
    data["iiko_staff"] = [
        r
        for r in data["iiko_staff"]
        if isinstance(r, dict)
        and not (
            (telegram_id is not None and int(r.get("telegram_id") or 0) == int(telegram_id))
            or (
                iiko_employee_id
                and str(r.get("iiko_employee_id") or "") == iiko_employee_id.strip()
            )
        )
    ]
    return before - len(data["iiko_staff"])


def staff_by_telegram(data: dict[str, Any], telegram_id: int) -> dict[str, Any] | None:
    ensure_tables(data)
    for r in data["iiko_staff"]:
        if isinstance(r, dict) and int(r.get("telegram_id") or 0) == int(telegram_id):
            return r
    return None


def staff_by_employee(data: dict[str, Any], employee_id: str) -> dict[str, Any] | None:
    ensure_tables(data)
    emp = (employee_id or "").strip()
    for r in data["iiko_staff"]:
        if isinstance(r, dict) and str(r.get("iiko_employee_id") or "") == emp:
            return r
    return None


def grant_permit(
    data: dict[str, Any],
    *,
    telegram_id: int,
    day: str | None = None,
) -> dict[str, Any] | None:
    """После завершённого опроса: можно закрывать личную смену в iiko."""
    row = staff_by_telegram(data, telegram_id)
    if not row:
        return None
    day = day or business_date()
    rec = {
        "ok": True,
        "telegram_id": int(telegram_id),
        "iiko_employee_id": row["iiko_employee_id"],
        "chat_id": int(row["chat_id"]),
        "business_date": day,
        "at": _now_iso(),
    }
    data["iiko_permits"][permit_key(row["iiko_employee_id"], day)] = rec
    return rec


def clockout_permit(data: dict[str, Any], employee_id: str, day: str | None = None) -> dict[str, Any]:
    ensure_tables(data)
    day = day or business_date()
    rec = data["iiko_permits"].get(permit_key(employee_id, day))
    if isinstance(rec, dict) and rec.get("ok"):
        return {"ok": True, "employeeId": employee_id, "date": day, "at": rec.get("at")}
    return {"ok": False, "employeeId": employee_id, "date": day}


def issue_out_token(data: dict[str, Any], employee_id: str) -> str:
    ensure_tables(data)
    row = staff_by_employee(data, employee_id)
    if not row:
        raise KeyError("unknown employee")
    token = secrets.token_urlsafe(12)
    data["iiko_out_tokens"][token] = {
        "iiko_employee_id": row["iiko_employee_id"],
        "telegram_id": row["telegram_id"],
        "chat_id": row["chat_id"],
        "at": _now_iso(),
    }
    return token


def consume_out_token(data: dict[str, Any], token: str) -> dict[str, Any] | None:
    ensure_tables(data)
    rec = data["iiko_out_tokens"].pop(token, None)
    return rec if isinstance(rec, dict) else None


def deep_link(bot_username: str, token: str) -> str:
    user = (bot_username or "").lstrip("@")
    return f"https://t.me/{user}?start=out_{token}"


def api_key_ok(provided: str | None, expected: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


# --- HTTP (aiohttp, уже зависимость aiogram) ---

def make_aiohttp_app(load_data, save_data, *, api_key: str, bot_username: str):
    from aiohttp import web

    def _auth(request) -> bool:
        got = request.headers.get("X-Iiko-Key") or request.query.get("key")
        return api_key_ok(got, api_key)

    async def clockout(request: web.Request) -> web.Response:
        if not _auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        emp = str(request.query.get("employeeId") or "").strip()
        if not emp:
            return web.json_response({"error": "employeeId required"}, status=400)
        day = str(request.query.get("date") or "").strip() or None
        data = await load_data()
        return web.json_response(clockout_permit(data, emp, day))

    async def whoami(request: web.Request) -> web.Response:
        if not _auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        emp = str(body.get("employeeId") or request.query.get("employeeId") or "").strip()
        data = await load_data()
        row = staff_by_employee(data, emp)
        if not row:
            return web.json_response({"error": "unknown employee"}, status=404)
        token = issue_out_token(data, emp)
        await save_data(data)
        return web.json_response(
            {
                "employeeId": emp,
                "telegramId": row["telegram_id"],
                "chatId": row["chat_id"],
                "token": token,
                "deepLink": deep_link(bot_username, token),
            }
        )

    async def staff_list(request: web.Request) -> web.Response:
        if not _auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        data = await load_data()
        ensure_tables(data)
        public = [
            {
                "telegramId": r.get("telegram_id"),
                "employeeId": r.get("iiko_employee_id"),
                "chatId": r.get("chat_id"),
                "role": r.get("role"),
            }
            for r in data["iiko_staff"]
            if isinstance(r, dict)
        ]
        return web.json_response({"staff": public})

    app = web.Application()
    app.router.add_get("/v1/iiko/clockout-permit", clockout)
    app.router.add_post("/v1/iiko/whoami", whoami)
    app.router.add_get("/v1/iiko/staff", staff_list)
    return app


async def start_http_server(load_data, save_data, *, api_key: str, bot_username: str) -> None:
    from aiohttp import web

    host = os.getenv("IIKO_HTTP_HOST", "127.0.0.1")
    port = int(os.getenv("IIKO_HTTP_PORT", "8091"))
    app = make_aiohttp_app(load_data, save_data, api_key=api_key, bot_username=bot_username)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"[iiko-http] {host}:{port}  GET /v1/iiko/clockout-permit  POST /v1/iiko/whoami")
