"""
Telegram Mini App API: initData, роли, дашборд, доступы по QR/инвайту.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl


def validate_webapp_init_data(
    init_data: str, *, bot_token: str, max_age_sec: int = 86400
) -> dict[str, str] | None:
    """Проверка подписи Telegram WebApp initData. Возвращает dict полей или None."""
    if not init_data or not bot_token:
        return None
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    calc = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date") or "0")
    except ValueError:
        return None
    if max_age_sec > 0 and abs(time.time() - auth_date) > max_age_sec:
        return None
    return parsed


def parse_user(fields: dict[str, str]) -> dict[str, Any] | None:
    raw = fields.get("user")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def resolve_miniapp_role(
    data: dict[str, Any],
    user_id: int,
    *,
    is_global_admin: bool,
) -> dict[str, Any]:
    """Роль для UI mini app + список экранов."""
    import pulse_model

    if is_global_admin:
        role = "owner"
        label = "Владелец / админ Pulse"
    elif pulse_model.has_ai_auditor_access(data, user_id) and pulse_model.is_happiness_manager_only(
        data, user_id
    ):
        role = "happiness"
        label = pulse_model.role_label_ru(pulse_model.ROLE_HAPPINESS_MANAGER)
    elif pulse_model.has_manager_access(data, user_id):
        role = "manager"
        profiles = pulse_model.manager_profiles(data, user_id)
        if any(p.get("role") == pulse_model.ROLE_NETWORK_ADMIN for p in profiles):
            role = "network"
            label = pulse_model.role_label_ru(pulse_model.ROLE_NETWORK_ADMIN)
        else:
            label = "Управляющий"
    elif pulse_model.has_chef_access(data, user_id):
        role = "chef"
        label = pulse_model.role_label_ru(pulse_model.ROLE_CHEF)
    else:
        role = "staff"
        label = "Команда смены"

    screens = _screens_for_role(role)
    locations = _locations_for_user(data, user_id, is_global_admin=is_global_admin)
    return {
        "role": role,
        "role_label": label,
        "screens": screens,
        "locations": locations,
        "feedback_in_bot": True,
        "app_mode": "app",
        "note": (
            "Пульс точки, отзывы зал/кухня, горящие и доступы — здесь. "
            "Линейка пишет отзыв о смене из группы в боте (кнопка в личку)."
        ),
    }


def _screens_for_role(role: str) -> list[dict[str, str]]:
    staff = [
        {"id": "home", "title": "Обзор", "blurb": "Что видно команде", "status": "ready"},
        {"id": "training", "title": "Обучение", "blurb": "Материалы", "status": "ready"},
        {
            "id": "feedback_bot",
            "title": "Написать отзыв",
            "blurb": "Пока через бота — из группы",
            "status": "bot",
        },
    ]
    manager = [
        {
            "id": "home",
            "title": "Обзор точки",
            "blurb": "Пульс, зал и кухня, вовлечённость",
            "status": "ready",
        },
        {
            "id": "reviews",
            "title": "Отзывы",
            "blurb": "Комментарии по залу / кухне",
            "status": "ready",
        },
        {
            "id": "engagement",
            "title": "Вовлечённость",
            "blurb": "Кто отвечает на опрос",
            "status": "ready",
        },
        {
            "id": "signals",
            "title": "Горящие вопросы",
            "blurb": "Активные сигналы",
            "status": "ready",
        },
        {
            "id": "ai",
            "title": "ИИ-советы",
            "blurb": "Первые выводы по отзывам",
            "status": "ready",
        },
        {
            "id": "access",
            "title": "Доступы",
            "blurb": "Роль + QR или инвайт-ссылка",
            "status": "ready",
        },
    ]
    owner = [
        {"id": "home", "title": "Обзор", "blurb": "Пульс точек", "status": "ready"},
        {"id": "reviews", "title": "Отзывы", "blurb": "Зал / кухня", "status": "ready"},
        {"id": "signals", "title": "Горящие", "blurb": "Сигналы", "status": "ready"},
        {"id": "ai", "title": "ИИ-советы", "blurb": "Намётки", "status": "ready"},
        {
            "id": "access",
            "title": "Доступы",
            "blurb": "Роль + QR или инвайт",
            "status": "ready",
        },
        {
            "id": "ai_audit",
            "title": "ИИ-аудит",
            "blurb": "Полный аудит — в боте",
            "status": "bot",
        },
        {"id": "billing", "title": "Оплаты", "blurb": "Скоро", "status": "soon"},
    ]
    if role == "staff":
        return staff
    if role == "chef":
        return [
            {"id": "home", "title": "Кухня · обзор", "blurb": "Сигналы кухни", "status": "ready"},
            {"id": "reviews", "title": "Отзывы", "blurb": "Зал и кухня", "status": "ready"},
            {
                "id": "feedback_bot",
                "title": "Оценить смену",
                "blurb": "Пока через бота",
                "status": "bot",
            },
        ]
    if role in ("manager", "network", "happiness"):
        base = list(manager)
        if role in ("network", "happiness"):
            base.insert(
                1,
                {
                    "id": "ai_audit",
                    "title": "ИИ-аудит",
                    "blurb": "Полный аудит — в боте",
                    "status": "bot",
                },
            )
        return base
    if role == "owner":
        return owner
    return staff


def _locations_for_user(
    data: dict[str, Any], user_id: int, *, is_global_admin: bool
) -> list[dict[str, str]]:
    import report_pulse

    scope = report_pulse.chat_scope_for_user(
        data, user_id, is_global_admin=is_global_admin
    )
    return [{"id": cid, "title": title} for cid, title in scope[:40]]


def _auth_user(request: Any, bot_token: str) -> tuple[dict[str, Any] | None, Any]:
    from aiohttp import web

    auth = request.headers.get("Authorization") or ""
    init_data = ""
    if auth.lower().startswith("tma "):
        init_data = auth[4:].strip()
    if not init_data:
        init_data = request.query.get("initData") or ""
    fields = validate_webapp_init_data(init_data, bot_token=bot_token)
    if not fields:
        return None, web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    user = parse_user(fields)
    if not user or not user.get("id"):
        return None, web.json_response({"ok": False, "error": "no_user"}, status=401)
    return user, None


def make_aiohttp_app(
    *,
    bot_token: str,
    load_data: Callable,
    is_global_admin_fn: Callable[[int], bool],
    bot_username: str = "",
    jsonl_path: Path | str | None = None,
    save_data: Callable | None = None,
    resolve_username: Callable | None = None,
):
    from aiohttp import web

    import miniapp_access
    import miniapp_dashboard
    import staff_assign

    miniapp_dir = os.getenv("MINIAPP_STATIC_DIR", "").strip()
    if not miniapp_dir:
        here = os.path.dirname(os.path.abspath(__file__))
        candidate_bot = os.path.join(here, "miniapp")
        root = os.path.dirname(here)
        candidate_docs = os.path.join(root, "docs", "sostoyanie-smeny", "app")
        if os.path.isdir(candidate_bot):
            miniapp_dir = candidate_bot
        elif os.path.isdir(candidate_docs):
            miniapp_dir = candidate_docs
        else:
            miniapp_dir = candidate_bot

    jpath = Path(jsonl_path) if jsonl_path else None

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    async def me(request: web.Request) -> web.Response:
        user, err = _auth_user(request, bot_token)
        if err:
            return err
        assert user is not None
        uid = int(user["id"])
        data = await load_data()
        profile = resolve_miniapp_role(
            data, uid, is_global_admin=is_global_admin_fn(uid)
        )
        start_param = ""
        # initData may include start_param
        auth = request.headers.get("Authorization") or ""
        init_data = auth[4:].strip() if auth.lower().startswith("tma ") else ""
        if init_data:
            fields = validate_webapp_init_data(init_data, bot_token=bot_token) or {}
            start_param = str(fields.get("start_param") or "")
        return web.json_response(
            {
                "ok": True,
                "user": {
                    "id": uid,
                    "first_name": user.get("first_name") or "",
                    "username": user.get("username") or "",
                },
                "bot_username": bot_username,
                "start_param": start_param,
                **profile,
            }
        )

    async def dashboard(request: web.Request) -> web.Response:
        user, err = _auth_user(request, bot_token)
        if err:
            return err
        assert user is not None
        uid = int(user["id"])
        data = await load_data()
        pm = __import__("pulse_model")
        if not (
            is_global_admin_fn(uid)
            or pm.has_manager_access(data, uid)
            or pm.has_chef_access(data, uid)
            or pm.has_ai_auditor_access(data, uid)
        ):
            return web.json_response(
                {"ok": False, "error": "forbidden", "message": "Дашборд для управляющих"},
                status=403,
            )
        chat_raw = request.query.get("chat_id") or ""
        period = (request.query.get("period") or "week").strip()
        chat_id = None
        if chat_raw.strip():
            try:
                chat_id = int(chat_raw)
            except ValueError:
                return web.json_response(
                    {"ok": False, "error": "bad_chat_id"}, status=400
                )
        payload = await miniapp_dashboard.build_dashboard(
            data,
            uid,
            is_global_admin=is_global_admin_fn(uid),
            chat_id=chat_id,
            period=period,
            jsonl_path=jpath,
        )
        return web.json_response(payload)

    async def access_options(request: web.Request) -> web.Response:
        user, err = _auth_user(request, bot_token)
        if err:
            return err
        assert user is not None
        uid = int(user["id"])
        data = await load_data()
        payload = miniapp_access.access_options_payload(
            data, uid, is_global_admin=is_global_admin_fn(uid)
        )
        status = 200 if payload.get("ok") else 403
        return web.json_response(payload, status=status)

    async def _read_json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    async def access_invite(request: web.Request) -> web.Response:
        user, err = _auth_user(request, bot_token)
        if err:
            return err
        assert user is not None
        if save_data is None:
            return web.json_response({"ok": False, "error": "readonly"}, status=503)
        uid = int(user["id"])
        data = await load_data()
        ga = is_global_admin_fn(uid)
        if not staff_assign.can_manage_staff(data, uid, is_global_admin=ga):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        body = await _read_json(request)
        role_code = str(body.get("role_code") or body.get("role") or "").strip()
        role = staff_assign.ROLE_CODES.get(role_code) or (
            role_code if role_code in staff_assign.ROLE_CODES.values() else ""
        )
        org_id = str(body.get("org_id") or "").strip()
        chat_id = body.get("chat_id")
        if chat_id is not None and str(chat_id).strip() != "":
            try:
                chat_id = int(chat_id)
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "bad_chat_id"}, status=400)
        else:
            chat_id = None
        if not org_id and chat_id is not None:
            rec = data.get("chats", {}).get(str(chat_id)) or {}
            org_id = str(rec.get("organization_id") or "")
        # placeholder target for validate role/place rights
        ok, err_msg = staff_assign.validate_assignment(
            data,
            uid,
            is_global_admin=ga,
            target_uid=1,
            org_id=org_id,
            role=role,
            chat_id=chat_id,
        )
        if not ok:
            return web.json_response({"ok": False, "error": err_msg or "invalid"}, status=400)
        invite = miniapp_access.create_invite(
            data,
            created_by=uid,
            org_id=org_id,
            role=role,
            chat_id=chat_id,
            bot_username=bot_username,
        )
        await save_data(data)
        return web.json_response({"ok": True, **invite})

    async def access_grant(request: web.Request) -> web.Response:
        user, err = _auth_user(request, bot_token)
        if err:
            return err
        assert user is not None
        if save_data is None:
            return web.json_response({"ok": False, "error": "readonly"}, status=503)
        uid = int(user["id"])
        data = await load_data()
        ga = is_global_admin_fn(uid)
        if not staff_assign.can_manage_staff(data, uid, is_global_admin=ga):
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)
        body = await _read_json(request)
        role_code = str(body.get("role_code") or body.get("role") or "").strip()
        role = staff_assign.ROLE_CODES.get(role_code) or (
            role_code if role_code in staff_assign.ROLE_CODES.values() else ""
        )
        org_id = str(body.get("org_id") or "").strip()
        chat_id = body.get("chat_id")
        if chat_id is not None and str(chat_id).strip() != "":
            try:
                chat_id = int(chat_id)
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "bad_chat_id"}, status=400)
        else:
            chat_id = None
        if not org_id and chat_id is not None:
            rec = data.get("chats", {}).get(str(chat_id)) or {}
            org_id = str(rec.get("organization_id") or "")

        target_uid: int | None = None
        raw = str(body.get("qr") or body.get("identity") or body.get("username") or "").strip()
        if body.get("user_id") is not None:
            try:
                target_uid = int(body["user_id"])
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "bad_user_id"}, status=400)
        elif raw:
            parsed = miniapp_access.parse_identity_payload(raw)
            if not parsed.get("ok"):
                return web.json_response(
                    {"ok": False, "error": parsed.get("hint") or "unparsed"}, status=400
                )
            if parsed["kind"] == "user_id":
                target_uid = int(parsed["user_id"])
            elif parsed["kind"] == "username":
                if resolve_username is None:
                    return web.json_response(
                        {
                            "ok": False,
                            "error": (
                                "Username из QR пока не резолвится. "
                                "Создайте инвайт-QR — сотрудник откроет ссылку сам."
                            ),
                        },
                        status=400,
                    )
                try:
                    target_uid = await resolve_username(str(parsed["username"]))
                except Exception as e:
                    print(f"[miniapp-resolve] {e}")
                    target_uid = None
                if not target_uid:
                    return web.json_response(
                        {
                            "ok": False,
                            "error": (
                                f"Не нашли @{parsed['username']}. "
                                "Нужен публичный @username или инвайт-QR."
                            ),
                        },
                        status=404,
                    )
            elif parsed["kind"] == "invite":
                return web.json_response(
                    {
                        "ok": False,
                        "error": "Это инвайт-ссылка — её должен открыть сотрудник у себя.",
                    },
                    status=400,
                )
        if not target_uid:
            return web.json_response({"ok": False, "error": "Укажите QR, @username или ID"}, status=400)

        ok, err_msg = staff_assign.validate_assignment(
            data,
            uid,
            is_global_admin=ga,
            target_uid=target_uid,
            org_id=org_id,
            role=role,
            chat_id=chat_id,
        )
        if not ok:
            return web.json_response({"ok": False, "error": err_msg or "invalid"}, status=400)
        staff_assign.apply_assignment(
            data, target_uid=target_uid, org_id=org_id, role=role, chat_id=chat_id
        )
        await save_data(data)
        place = org_id
        if chat_id is not None:
            place = str((data.get("chats", {}).get(str(chat_id)) or {}).get("title") or chat_id)
        return web.json_response(
            {
                "ok": True,
                "target_user_id": target_uid,
                "role": role,
                "role_label": __import__("pulse_model").role_label_ru(role),
                "place": place,
            }
        )

    async def access_redeem(request: web.Request) -> web.Response:
        user, err = _auth_user(request, bot_token)
        if err:
            return err
        assert user is not None
        if save_data is None:
            return web.json_response({"ok": False, "error": "readonly"}, status=503)
        uid = int(user["id"])
        body = await _read_json(request)
        token = str(body.get("token") or body.get("start_param") or "").strip()
        data = await load_data()
        ok, err_msg, inv = miniapp_access.redeem_invite(data, token=token, user_id=uid)
        if not ok:
            return web.json_response({"ok": False, "error": err_msg or "fail"}, status=400)
        await save_data(data)
        assert inv is not None
        role = str(inv.get("role") or "")
        return web.json_response(
            {
                "ok": True,
                "role": role,
                "role_label": __import__("pulse_model").role_label_ru(role),
                "org_id": inv.get("org_id"),
                "chat_id": inv.get("chat_id"),
            }
        )

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "miniapp"})

    app = web.Application(middlewares=[cors_middleware])
    for path in (
        "/api/miniapp/me",
        "/api/miniapp/dashboard",
        "/api/miniapp/access/options",
        "/api/miniapp/access/invite",
        "/api/miniapp/access/grant",
        "/api/miniapp/access/redeem",
    ):
        app.router.add_route("OPTIONS", path, health)
    app.router.add_get("/api/miniapp/me", me)
    app.router.add_get("/api/miniapp/dashboard", dashboard)
    app.router.add_get("/api/miniapp/access/options", access_options)
    app.router.add_post("/api/miniapp/access/invite", access_invite)
    app.router.add_post("/api/miniapp/access/grant", access_grant)
    app.router.add_post("/api/miniapp/access/redeem", access_redeem)
    app.router.add_get("/api/miniapp/health", health)
    if os.path.isdir(miniapp_dir):
        index_path = os.path.join(miniapp_dir, "index.html")

        async def serve_index(_request: web.Request) -> web.StreamResponse:
            if not os.path.isfile(index_path):
                return web.Response(text="Mini App index.html не найден", status=404)
            return web.FileResponse(index_path)

        app.router.add_get("/", serve_index)
        app.router.add_get("/index.html", serve_index)
        app.router.add_static("/", miniapp_dir, show_index=False)
    return app


async def start_miniapp_server(
    *,
    bot_token: str,
    load_data: Callable,
    is_global_admin_fn: Callable[[int], bool],
    bot_username: str = "",
    host: str = "0.0.0.0",
    port: int | None = None,
    jsonl_path: Path | str | None = None,
    save_data: Callable | None = None,
    resolve_username: Callable | None = None,
) -> None:
    from aiohttp import web

    port = port or int(os.getenv("MINIAPP_PORT", os.getenv("PORT", "8080")))
    app = make_aiohttp_app(
        bot_token=bot_token,
        load_data=load_data,
        is_global_admin_fn=is_global_admin_fn,
        bot_username=bot_username,
        jsonl_path=jsonl_path,
        save_data=save_data,
        resolve_username=resolve_username,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"[miniapp-http] http://{host}:{port}/  (static + /api/miniapp/*)")
