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
BTN_SIGNALS = "Горящие вопросы"
BTN_DAY_PLAN = "План дня"
BTN_DAY_CLOSE = "Закрытие дня"
BTN_STAFF_OUT = "Уход кадра"
BTN_TASKS = "Задания"
BTN_SUBSCRIPTION = "Подписка/статус"
BTN_SUPPORT = "Поддержка"
BTN_CONNECT = "Как подключить точку"
MANAGER_MENU_BUTTONS = frozenset(
    {
        BTN_REPORT,
        BTN_SIGNALS,
        BTN_DAY_PLAN,
        BTN_DAY_CLOSE,
        BTN_STAFF_OUT,
        BTN_TASKS,
        BTN_SUBSCRIPTION,
        BTN_SUPPORT,
        BTN_CONNECT,
    }
)

ROLE_NETWORK_ADMIN = "network_admin"
ROLE_LOCATION_ADMIN = "location_admin"

SUB_ACTIVE = "active"
SUB_GRACE = "grace"
SUB_SUSPENDED = "suspended"

# Тарифы по числу уникальных сотрудников на точке (как на лендинге)
TARIFF_T10 = "t10"
TARIFF_T25 = "t25"
TARIFF_T40 = "t40"
TARIFF_ENTERPRISE = "enterprise"
TARIFF_CUSTOM = "custom"

TARIFF_CODES = frozenset(
    {TARIFF_T10, TARIFF_T25, TARIFF_T40, TARIFF_ENTERPRISE, TARIFF_CUSTOM}
)

TARIFF_ALIASES: dict[str, str] = {
    "10": TARIFF_T10,
    "t10": TARIFF_T10,
    "4900": TARIFF_T10,
    "25": TARIFF_T25,
    "t25": TARIFF_T25,
    "9900": TARIFF_T25,
    "40": TARIFF_T40,
    "t40": TARIFF_T40,
    "12900": TARIFF_T40,
    "enterprise": TARIFF_ENTERPRISE,
    "ent": TARIFF_ENTERPRISE,
    "40+": TARIFF_ENTERPRISE,
    "custom": TARIFF_CUSTOM,
    "ind": TARIFF_CUSTOM,
}

TARIFF_INFO: dict[str, dict[str, Any]] = {
    TARIFF_T10: {"label": "до 10 чел", "price": 4900, "max_users": 10},
    TARIFF_T25: {"label": "11–25 чел", "price": 9900, "max_users": 25},
    TARIFF_T40: {"label": "26–40 чел", "price": 12900, "max_users": 40},
    TARIFF_ENTERPRISE: {"label": "40+ чел", "price": None, "max_users": None},
    TARIFF_CUSTOM: {"label": "индивидуально", "price": None, "max_users": None},
}


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


def parse_tariff(raw: str) -> str | None:
    key = (raw or "").strip().lower()
    if not key:
        return None
    code = TARIFF_ALIASES.get(key, key)
    return code if code in TARIFF_CODES else None


def tariff_label(plan: str | None) -> str:
    if not plan:
        return "не задан"
    info = TARIFF_INFO.get(plan)
    if not info:
        return plan
    return str(info["label"])


def tariff_price_rub(plan: str | None) -> int | None:
    if not plan:
        return None
    info = TARIFF_INFO.get(plan)
    if not info:
        return None
    price = info.get("price")
    return int(price) if price is not None else None


def tariff_max_users(plan: str | None) -> int | None:
    if not plan:
        return None
    info = TARIFF_INFO.get(plan)
    if not info:
        return None
    mx = info.get("max_users")
    return int(mx) if mx is not None else None


def tariff_display(plan: str | None) -> str:
    if not plan:
        return "—"
    label = tariff_label(plan)
    price = tariff_price_rub(plan)
    if price is not None:
        return f"{plan} · {label} · {price:,} ₽/мес".replace(",", " ")
    return f"{plan} · {label}"


def suggest_tariff_for_users(unique_users: int) -> str:
    if unique_users <= 10:
        return TARIFF_T10
    if unique_users <= 25:
        return TARIFF_T25
    if unique_users <= 40:
        return TARIFF_T40
    return TARIFF_ENTERPRISE


def chat_tariff(data: dict[str, Any], chat_id: int | str) -> str | None:
    rec = data.get("chats", {}).get(str(chat_id))
    if not isinstance(rec, dict):
        return None
    plan = rec.get("tariff_plan")
    return str(plan) if plan else None


def chat_tariff_history(data: dict[str, Any], chat_id: int | str) -> list[dict[str, Any]]:
    rec = data.get("chats", {}).get(str(chat_id))
    if not isinstance(rec, dict):
        return []
    raw = rec.get("tariff_history")
    if not isinstance(raw, list):
        return []
    return [h for h in raw if isinstance(h, dict)]


def set_chat_tariff(
    data: dict[str, Any],
    chat_id: int | str,
    plan: str,
    *,
    admin_id: int,
    unique_users: int | None = None,
    note: str | None = None,
) -> tuple[str | None, bool]:
    """Возвращает (старый тариф, изменился ли)."""
    cid = str(chat_id)
    chats = data.setdefault("chats", {})
    if cid not in chats or not isinstance(chats[cid], dict):
        return None, False
    rec = chats[cid]
    old = rec.get("tariff_plan")
    old_s = str(old) if old else None
    if old_s == plan:
        return old_s, False
    rec["tariff_plan"] = plan
    hist = rec.setdefault("tariff_history", [])
    if not isinstance(hist, list):
        hist = []
        rec["tariff_history"] = hist
    entry: dict[str, Any] = {
        "plan": plan,
        "at": _now_iso(),
        "by": admin_id,
    }
    if unique_users is not None:
        entry["unique_users"] = unique_users
    if note:
        entry["note"] = note.strip()
    if old_s:
        entry["from_plan"] = old_s
    hist.append(entry)
    return old_s, True


def tariff_over_limit(plan: str | None, unique_users: int) -> bool:
    mx = tariff_max_users(plan)
    if mx is None:
        return False
    return unique_users > mx


def get_tariff_over_alert(rec: dict[str, Any]) -> dict[str, Any] | None:
    raw = rec.get("tariff_over_alert")
    return raw if isinstance(raw, dict) else None


def clear_tariff_over_alert(rec: dict[str, Any]) -> None:
    rec.pop("tariff_over_alert", None)


def record_tariff_over_alert(rec: dict[str, Any], plan: str, unique_users: int) -> None:
    rec["tariff_over_alert"] = {
        "plan": plan,
        "users": int(unique_users),
        "sent_at": _now_iso(),
    }


def should_notify_tariff_over(rec: dict[str, Any], plan: str, unique_users: int) -> bool:
    if not tariff_over_limit(plan, unique_users):
        return False
    prev = get_tariff_over_alert(rec)
    if not prev:
        return True
    if str(prev.get("plan")) != plan:
        return True
    try:
        prev_users = int(prev.get("users", 0))
    except (TypeError, ValueError):
        prev_users = 0
    return unique_users > prev_users


def sync_tariff_alert_after_set(rec: dict[str, Any], plan: str, unique_users: int) -> None:
    """После ручной смены тарифа — не слать алерт повторно, пока не выросло число людей."""
    if tariff_over_limit(plan, unique_users):
        record_tariff_over_alert(rec, plan, unique_users)
    else:
        clear_tariff_over_alert(rec)


def format_tariff_history_line(entry: dict[str, Any]) -> str:
    plan = entry.get("plan", "?")
    at = entry.get("at", "")
    users = entry.get("unique_users")
    frm = entry.get("from_plan")
    parts = [f"<code>{escape(str(plan))}</code> ({escape(tariff_label(str(plan)))})"]
    if frm:
        parts.append(f"← {escape(str(frm))}")
    if users is not None:
        parts.append(f"· {users} чел.")
    if at:
        parts.append(f"· {escape(str(at))}")
    return " ".join(parts)


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
            "<b>Поддержка Pulse Team</b>\n\n"
            f'Напишите в Telegram: <a href="https://t.me/{escape(su)}">@{escape(su)}</a>\n\n'
            "Укажите сеть, точку и вопрос — ответим в рабочее время."
        )
    return (
        "<b>Поддержка Pulse Team</b>\n\n"
        "Контакт пока не настроен. Администратор бота может задать "
        "<code>SUPPORT_USERNAME</code> в <code>.env</code> (без @)."
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
            [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_SIGNALS)],
            [KeyboardButton(text=BTN_DAY_PLAN), KeyboardButton(text=BTN_DAY_CLOSE)],
            [KeyboardButton(text=BTN_STAFF_OUT), KeyboardButton(text=BTN_TASKS)],
            [KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_SUPPORT)],
            [KeyboardButton(text=BTN_CONNECT)],
        ],
        resize_keyboard=True,
    )


def support_only_reply_markup():
    """Reply-клавиатура только с кнопкой «Поддержка» (для всех пользователей)."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SUPPORT)]],
        resize_keyboard=True,
    )


def remove_reply_markup():
    from aiogram.types import ReplyKeyboardRemove

    return ReplyKeyboardRemove()
