"""
Telegram Mini App API: проверка initData и профиль по роли.

Эндпоинты рассчитаны на хостинг рядом с ботом (aiohttp) или отдельно.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any
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
        # уточняем сеть vs точка
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
        "note": (
            "Оценку и отзыв о смене линейка по-прежнему оставляет в боте "
            "(кнопка из группы → личка)."
        ),
    }


def _screens_for_role(role: str) -> list[dict[str, str]]:
    staff = [
        {
            "id": "tests",
            "title": "Тесты",
            "blurb": "Проверка знаний стандартов и SOP",
            "status": "soon",
        },
        {
            "id": "training",
            "title": "Обучение",
            "blurb": "Материалы и инструкции точки",
            "status": "ready",
        },
        {
            "id": "feedback_bot",
            "title": "Отзыв о смене",
            "blurb": "Только через бота — из группы «в личку»",
            "status": "bot",
        },
    ]
    manager = [
        {
            "id": "reports",
            "title": "Отчёты",
            "blurb": "Сводка смены, недели, месяца",
            "status": "ready",
        },
        {
            "id": "signals",
            "title": "Горящие вопросы",
            "blurb": "Проблемы смены и статусы",
            "status": "ready",
        },
        {
            "id": "materials",
            "title": "Материалы",
            "blurb": "Загрузка файлов и папки обучения",
            "status": "ready",
        },
        {
            "id": "access",
            "title": "Доступы",
            "blurb": "Подключить / отозвать роли",
            "status": "ready",
        },
    ]
    owner = [
        {
            "id": "ai_audit",
            "title": "ИИ-аудит",
            "blurb": "Индекс здоровья точки по голосу и файлам",
            "status": "ready",
        },
        {
            "id": "network_summary",
            "title": "Сводка по точкам",
            "blurb": "Все локации сети в одном экране",
            "status": "ready",
        },
        {
            "id": "billing",
            "title": "Оплаты и тарифы",
            "blurb": "Подписки, лимиты, статусы",
            "status": "soon",
        },
        {
            "id": "reports",
            "title": "Отчёты",
            "blurb": "Глубокая аналитика по запросу",
            "status": "ready",
        },
    ]
    if role == "staff":
        return staff
    if role == "chef":
        return [
            {
                "id": "training",
                "title": "Обучение",
                "blurb": "Материалы кухни",
                "status": "ready",
            },
            {
                "id": "stop",
                "title": "Стоп-лист",
                "blurb": "В боте: задать / дописать / посмотреть",
                "status": "bot",
            },
            {
                "id": "feedback_bot",
                "title": "Оценка смены кухни",
                "blurb": "Через бота",
                "status": "bot",
            },
        ]
    if role in ("manager", "network", "happiness"):
        base = list(manager)
        if role in ("network", "happiness"):
            base.insert(
                0,
                {
                    "id": "ai_audit",
                    "title": "ИИ-аудит",
                    "blurb": "Индекс операционного здоровья",
                    "status": "ready",
                },
            )
        if role == "network":
            base.insert(
                1,
                {
                    "id": "network_summary",
                    "title": "Сводка по точкам",
                    "blurb": "Все точки организации",
                    "status": "ready",
                },
            )
        return base
    if role == "owner":
        return owner + [
            s for s in manager if s["id"] not in {x["id"] for x in owner}
        ]
    return staff


def _locations_for_user(
    data: dict[str, Any], user_id: int, *, is_global_admin: bool
) -> list[dict[str, str]]:
    import pulse_model
    import report_pulse

    scope = report_pulse.chat_scope_for_user(
        data, user_id, is_global_admin=is_global_admin
    )
    return [{"id": cid, "title": title} for cid, title in scope[:40]]


def make_aiohttp_app(*, bot_token: str, load_data, is_global_admin_fn, bot_username: str = ""):
    from aiohttp import web

    miniapp_dir = os.getenv("MINIAPP_STATIC_DIR", "").strip()
    if not miniapp_dir:
        # предпочтительно копия рядом с ботом (Railway)
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

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return resp

    async def me(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization") or ""
        init_data = ""
        if auth.lower().startswith("tma "):
            init_data = auth[4:].strip()
        if not init_data:
            init_data = request.query.get("initData") or ""
        fields = validate_webapp_init_data(init_data, bot_token=bot_token)
        if not fields:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        user = parse_user(fields)
        if not user or not user.get("id"):
            return web.json_response({"ok": False, "error": "no_user"}, status=401)
        uid = int(user["id"])
        data = await load_data()
        profile = resolve_miniapp_role(
            data, uid, is_global_admin=is_global_admin_fn(uid)
        )
        return web.json_response(
            {
                "ok": True,
                "user": {
                    "id": uid,
                    "first_name": user.get("first_name") or "",
                    "username": user.get("username") or "",
                },
                "bot_username": bot_username,
                **profile,
            }
        )

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "miniapp"})

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_route("OPTIONS", "/api/miniapp/me", health)
    app.router.add_get("/api/miniapp/me", me)
    app.router.add_get("/api/miniapp/health", health)
    if os.path.isdir(miniapp_dir):
        app.router.add_static("/", miniapp_dir, show_index=True)
    return app


async def start_miniapp_server(
    *,
    bot_token: str,
    load_data,
    is_global_admin_fn,
    bot_username: str = "",
    host: str = "0.0.0.0",
    port: int | None = None,
) -> None:
    from aiohttp import web

    port = port or int(os.getenv("MINIAPP_PORT", os.getenv("PORT", "8080")))
    app = make_aiohttp_app(
        bot_token=bot_token,
        load_data=load_data,
        is_global_admin_fn=is_global_admin_fn,
        bot_username=bot_username,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"[miniapp-http] http://{host}:{port}/  (static + /api/miniapp/me)")
