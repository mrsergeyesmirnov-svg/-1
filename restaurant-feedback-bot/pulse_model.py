"""
Модель данных: организация → точки (групповые чаты) → роли менеджеров.

Задел под ИИ-отчёты:
- Сырые события остаются в feedback_log.jsonl (rating / problem / comment) с restaurant_chat_id.
- Для недельного или по запросу ИИ-прогона агрегируйте строки за период по списку chat_id
  (все точки сети или только точки менеджера из manager_bindings).
- Внешний скрипт/cron: read JSONL → сформировать prompt (метрики + выдержки комментариев) →
  вызов LLM → запись результата в файл / Google Doc / сообщение в Telegram.
Не храните большие тексты отчёта в bot_data.json — только ссылки или last_report_at.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from html import escape
from typing import Any

# --- Кнопки меню (точное совпадение текста) ---
BTN_REPORT = "Отчёт"
BTN_SUBSCRIPTION = "Подписка/статус"
BTN_SUPPORT = "Поддержка"
BTN_CONNECT = "Как подключить точку"
MANAGER_MENU_BUTTONS = frozenset(
    {BTN_REPORT, BTN_SUBSCRIPTION, BTN_SUPPORT, BTN_CONNECT}
)

ROLE_NETWORK_ADMIN = "network_admin"
ROLE_LOCATION_ADMIN = "location_admin"

SUB_ACTIVE = "active"
SUB_GRACE = "grace"
SUB_SUSPENDED = "suspended"


def default_data() -> dict[str, Any]:
    return {
        "organizations": {},
        "chats": {},
        "managers": {},
        "private_slugs": {},
        "last_auto_sent": {},
    }


def new_org_id() -> str:
    return "org_" + secrets.token_hex(4)


def migrate_in_place(data: dict[str, Any]) -> bool:
    """Дополняет старые файлы полями организаций и привязкой чатов. Возвращает True, если нужно сохранить."""
    changed = False
    if "organizations" not in data:
        data["organizations"] = {}
        changed = True
    if "managers" not in data:
        data["managers"] = {}
        changed = True
    chats: dict[str, Any] = data.setdefault("chats", {})
    orgs: dict[str, Any] = data["organizations"]

    lacks_org = [
        cid
        for cid, rec in chats.items()
        if isinstance(rec, dict) and not rec.get("organization_id")
    ]
    # Только «чистый» legacy: были чаты, не было ни одной организации — создаём одну и вешаем все точки.
    # Если организации уже есть, новые чаты без organization_id ждут /link_org вручную.
    if lacks_org and not orgs:
        oid = new_org_id()
        orgs[oid] = {
            "name": "Организация (миграция)",
            "subscription": SUB_ACTIVE,
            "created_at": _now_iso(),
        }
        for cid in lacks_org:
            chats[cid]["organization_id"] = oid
        changed = True
    return changed


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def create_organization(data: dict[str, Any], name: str) -> str:
    oid = new_org_id()
    data.setdefault("organizations", {})[oid] = {
        "name": name.strip() or "Без названия",
        "subscription": SUB_ACTIVE,
        "created_at": _now_iso(),
    }
    return oid


def get_organization(data: dict[str, Any], org_id: str) -> dict[str, Any] | None:
    return data.get("organizations", {}).get(org_id)


def org_subscription(data: dict[str, Any], org_id: str) -> str:
    org = get_organization(data, org_id)
    if not org:
        return SUB_ACTIVE
    return org.get("subscription") or SUB_ACTIVE


def is_org_billing_blocked(data: dict[str, Any], org_id: str | None) -> bool:
    if not org_id:
        return False
    return org_subscription(data, org_id) == SUB_SUSPENDED


def chat_organization_id(data: dict[str, Any], chat_id: int) -> str | None:
    rec = data.get("chats", {}).get(str(chat_id))
    if not rec:
        return None
    oid = rec.get("organization_id")
    return str(oid) if oid else None


def list_chats_for_org(data: dict[str, Any], org_id: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for cid, rec in data.get("chats", {}).items():
        if not isinstance(rec, dict):
            continue
        if rec.get("organization_id") == org_id:
            out.append((cid, rec))
    return out


def manager_profiles(data: dict[str, Any], user_id: int) -> list[dict[str, Any]]:
    raw = data.get("managers", {}).get(str(user_id), [])
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict)]


def has_manager_access(data: dict[str, Any], user_id: int) -> bool:
    return bool(manager_profiles(data, user_id))


def allowed_chat_ids_for_manager(data: dict[str, Any], user_id: int) -> set[str]:
    """chat_id строками, по которым менеджеру можно смотреть аналитику."""
    ids: set[str] = set()
    for p in manager_profiles(data, user_id):
        oid = p.get("organization_id")
        if not oid:
            continue
        role = p.get("role")
        if role == ROLE_NETWORK_ADMIN:
            for cid, _ in list_chats_for_org(data, str(oid)):
                ids.add(cid)
        elif role == ROLE_LOCATION_ADMIN:
            for c in p.get("location_chat_ids") or []:
                ids.add(str(c))
    return ids


def set_manager_binding(
    data: dict[str, Any],
    user_id: int,
    org_id: str,
    role: str,
    location_chat_ids: list[str] | None,
) -> None:
    key = str(user_id)
    data.setdefault("managers", {})
    rest = [p for p in data["managers"].get(key, []) if not isinstance(p, dict) or p.get("organization_id") != org_id]
    if role == ROLE_NETWORK_ADMIN:
        locs: list[str] = []
    else:
        locs = [str(x) for x in (location_chat_ids or [])]
    rest.append({"organization_id": org_id, "role": role, "location_chat_ids": locs})
    data["managers"][key] = rest


def link_chat_to_organization(data: dict[str, Any], chat_id: int, org_id: str) -> bool:
    if org_id not in data.get("organizations", {}):
        return False
    cid = str(chat_id)
    chats = data.setdefault("chats", {})
    if cid not in chats:
        return False
    chats[cid]["organization_id"] = org_id
    return True


def text_subscription_status(data: dict[str, Any], user_id: int) -> str:
    profiles = manager_profiles(data, user_id)
    if not profiles:
        return "Для вас не настроена роль менеджера. Обратитесь к администратору Pulse."
    lines = ["<b>Подписка и организации</b>\n"]
    seen: set[str] = set()
    for p in profiles:
        oid = p.get("organization_id")
        if not oid or oid in seen:
            continue
        seen.add(str(oid))
        org = get_organization(data, str(oid))
        name = org.get("name", oid) if org else oid
        sub = org_subscription(data, str(oid))
        sub_ru = {"active": "активна", "grace": "льготный период", "suspended": "приостановлена"}.get(
            sub, sub
        )
        role = p.get("role")
        role_ru = "управляющий сетью" if role == ROLE_NETWORK_ADMIN else "менеджер точки"
        lines.append(
            f"• <b>{escape(str(name))}</b> (<code>{escape(str(oid))}</code>)\n"
            f"  подписка: <b>{escape(str(sub_ru))}</b> · ваша роль: {escape(role_ru)}\n"
        )
    return "\n".join(lines)


def text_report_stub() -> str:
    return (
        "<b>Отчёт</b>\n\n"
        "Здесь появится недельный и по запросу <b>ИИ-разбор</b>: динамика оценок, темы из комментариев, "
        "<b>алерты</b> и <b>рекомендации</b> по шагам улучшения (не только средняя оценка)."
    )


def text_support(support_username: str | None) -> str:
    su = (support_username or "").strip().lstrip("@")
    if su:
        return (
            "<b>Поддержка</b>\n\n"
            f'Напишите нам в Telegram: <a href="https://t.me/{escape(su)}">@{escape(su)}</a>\n\n'
            "Кратко укажите сеть, точку и суть вопроса — ответим как можно быстрее."
        )
    return (
        "<b>Поддержка</b>\n\n"
        "Контакт поддержки задаётся администратором бота (переменная <code>SUPPORT_USERNAME</code> в <code>.env</code>)."
    )


def text_connect_point(bot_username: str) -> str:
    un = bot_username.lstrip("@")
    un_e = escape(un)
    return (
        "<b>Как подключить точку</b>\n\n"
        "1. Создайте групповой чат официантов/смены (лучше супергруппа).\n"
        "2. Добавьте в чат бота.\n"
        "3. Администратор Pulse привяжет чат к вашей <b>организации</b> командой <code>/link_org …</code> в этом чате "
        "(или заранее через поддержку).\n"
        "4. В чате задайте время напоминания: <code>/settime 22:00</code> и при необходимости "
        "<code>/timezone Europe/Moscow</code>.\n\n"
        f'<a href="https://t.me/{un_e}?startgroup=open">добавить @{un_e} в группу</a>\n\n'
        "Оценки сотрудников идут <b>только в личку</b> по кнопке из группы — так ответ привязан к точке."
    )


def manager_menu_reply_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_SUBSCRIPTION)],
            [KeyboardButton(text=BTN_SUPPORT), KeyboardButton(text=BTN_CONNECT)],
        ],
        resize_keyboard=True,
    )


def remove_reply_markup():
    from aiogram.types import ReplyKeyboardRemove

    return ReplyKeyboardRemove()
