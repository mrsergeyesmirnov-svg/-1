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
BTN_FOLDER_ANALYTICS = "📊 Аналитика"
BTN_FOLDER_SHIFT = "📋 Смена"
BTN_FOLDER_SHIFT_DAY = "☀️ День"
BTN_FOLDER_SHIFT_STOP = "🛑 Стоп"
BTN_FOLDER_SHIFT_KITCHEN = "👨‍🍳 Кухня"
BTN_FOLDER_SHIFT_PLAN = "📅 План"
BTN_SHIFT_BACK = "← Смена"
BTN_CHEF_BACK = "← Меню"
BTN_FOLDER_INBOX = "📁 Сводки"
BTN_COMMANDS = "📖 Команды"
BTN_FOLDER_MORE = "⚙️ Ещё"
BTN_MONTH_PLAN = "План на месяц"
BTN_MENU_HOME = "← Главное меню"

BTN_REPORT = "Отчёт"
BTN_SIGNALS = "Горящие вопросы"
BTN_DAY_PLAN = "План дня"
BTN_DAY_CLOSE = "Закрытие дня"
BTN_STOP_LIST = "Стоп-лист"
BTN_STOP_ADD = "➕ В стоп-лист"
BTN_STOP_CURRENT = "Актуальный стоп"
BTN_RATE_SHIFT = "Оценить смену"
BTN_OPS_BROADCAST = "📣 Шефу и менеджерам"
BTN_CHECKLISTS = "Чек-листы смены"
BTN_STAFF_OUT = "Уход кадра"
BTN_TASKS = "Задания"
BTN_SUBSCRIPTION = "Подписка/статус"
BTN_SUPPORT = "Поддержка"
BTN_CONNECT = "Как подключить точку"
BTN_STAFF_ASSIGN = "Подключить доступ"
BTN_STAFF_REMOVE = "Отозвать доступ"
BTN_TRAINING_MGR = "📚 Материалы"
BTN_TRAINING = "📚 Обучение"
BTN_DAY_CLOSE_PAST = "Закрыть за дату"
BTN_REMINDERS = "⏰ Напоминания"
BTN_AI_AUDIT = "🧠 ИИ-аудит"
BTN_AUDIT_FINISH = "✅ Завершить анализ"
BTN_AUDIT_CANCEL = "❌ Отменить аудит"

MANAGER_ROOT_BUTTONS = frozenset(
    {
        BTN_FOLDER_ANALYTICS,
        BTN_FOLDER_SHIFT,
        BTN_FOLDER_INBOX,
        BTN_FOLDER_MORE,
        BTN_AI_AUDIT,
    }
)
SHIFT_FOLDER_BUTTONS = frozenset(
    {
        BTN_FOLDER_SHIFT_DAY,
        BTN_FOLDER_SHIFT_STOP,
        BTN_FOLDER_SHIFT_KITCHEN,
        BTN_FOLDER_SHIFT_PLAN,
        BTN_SHIFT_BACK,
    }
)
MANAGER_ACTION_BUTTONS = frozenset(
    {
        BTN_REPORT,
        BTN_SIGNALS,
        BTN_DAY_PLAN,
        BTN_DAY_CLOSE,
        BTN_STOP_LIST,
        BTN_STOP_ADD,
        BTN_STOP_CURRENT,
        BTN_RATE_SHIFT,
        BTN_OPS_BROADCAST,
        BTN_CHECKLISTS,
        BTN_MONTH_PLAN,
        BTN_STAFF_OUT,
        BTN_TASKS,
        BTN_SUBSCRIPTION,
        BTN_SUPPORT,
        BTN_CONNECT,
        BTN_STAFF_ASSIGN,
        BTN_STAFF_REMOVE,
        BTN_TRAINING_MGR,
        BTN_DAY_CLOSE_PAST,
        BTN_REMINDERS,
        BTN_AI_AUDIT,
        BTN_AUDIT_FINISH,
        BTN_AUDIT_CANCEL,
        BTN_MENU_HOME,
    }
) | SHIFT_FOLDER_BUTTONS
MANAGER_MENU_BUTTONS = MANAGER_ROOT_BUTTONS | MANAGER_ACTION_BUTTONS

CHEF_MENU_BUTTONS = frozenset(
    {
        BTN_RATE_SHIFT,
        BTN_STOP_LIST,
        BTN_STOP_ADD,
        BTN_STOP_CURRENT,
        BTN_FOLDER_SHIFT_STOP,
        BTN_CHEF_BACK,
        BTN_SUPPORT,
    }
)

ROLE_NETWORK_ADMIN = "network_admin"
ROLE_LOCATION_ADMIN = "location_admin"
ROLE_SENIOR_MANAGER = "senior_manager"
ROLE_CHEF = "chef"
ROLE_HAPPINESS_MANAGER = "happiness_manager"

ROLE_LABELS_RU: dict[str, str] = {
    ROLE_NETWORK_ADMIN: "Управляющий сети",
    ROLE_LOCATION_ADMIN: "Менеджер точки",
    ROLE_SENIOR_MANAGER: "Старший менеджер",
    ROLE_CHEF: "Шеф",
    ROLE_HAPPINESS_MANAGER: "Менеджер по счастью",
}

MANAGER_ROLES = frozenset(
    {
        ROLE_NETWORK_ADMIN,
        ROLE_LOCATION_ADMIN,
        ROLE_SENIOR_MANAGER,
        ROLE_HAPPINESS_MANAGER,
    }
)

# Роли с доступом к ИИ-аудитору (глобальный админ — отдельно в bot.py)
AI_AUDITOR_ROLES = frozenset({ROLE_HAPPINESS_MANAGER, ROLE_NETWORK_ADMIN})

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
    "2990": TARIFF_T10,
    "25": TARIFF_T25,
    "t25": TARIFF_T25,
    "9900": TARIFF_T25,
    "6990": TARIFF_T25,
    "40": TARIFF_T40,
    "t40": TARIFF_T40,
    "12900": TARIFF_T40,
    "8990": TARIFF_T40,
    "enterprise": TARIFF_ENTERPRISE,
    "ent": TARIFF_ENTERPRISE,
    "40+": TARIFF_ENTERPRISE,
    "custom": TARIFF_CUSTOM,
    "ind": TARIFF_CUSTOM,
}

TARIFF_INFO: dict[str, dict[str, Any]] = {
    TARIFF_T10: {"label": "до 10 чел", "price": 2990, "max_users": 10},
    TARIFF_T25: {"label": "11–25 чел", "price": 6990, "max_users": 25},
    TARIFF_T40: {"label": "26–40 чел", "price": 8990, "max_users": 40},
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
    if "iiko_staff" not in data or not isinstance(data.get("iiko_staff"), list):
        data["iiko_staff"] = []
        changed = True
    if "iiko_permits" not in data or not isinstance(data.get("iiko_permits"), dict):
        data["iiko_permits"] = {}
        changed = True
    if "iiko_points" not in data or not isinstance(data.get("iiko_points"), dict):
        data["iiko_points"] = {}
        changed = True
    if "iiko_nudge_sent" not in data or not isinstance(data.get("iiko_nudge_sent"), dict):
        data["iiko_nudge_sent"] = {}
        changed = True
    if "iiko_surveys" not in data or not isinstance(data.get("iiko_surveys"), list):
        data["iiko_surveys"] = []
        changed = True
    if "iiko_out_tokens" not in data or not isinstance(data.get("iiko_out_tokens"), dict):
        data["iiko_out_tokens"] = {}
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
    profiles = manager_profiles(data, user_id)
    return any(
        isinstance(p, dict) and p.get("role") in MANAGER_ROLES
        for p in profiles
    )


def has_senior_or_network_access(data: dict[str, Any], user_id: int) -> bool:
    profiles = manager_profiles(data, user_id)
    return any(
        isinstance(p, dict)
        and p.get("role") in (ROLE_NETWORK_ADMIN, ROLE_SENIOR_MANAGER)
        for p in profiles
    )


def can_assign_tasks_role(data: dict[str, Any], user_id: int) -> bool:
    """Старший менеджер, управляющий сети — назначают задания."""
    return has_senior_or_network_access(data, user_id)


def role_label_ru(role: str | None) -> str:
    if not role:
        return "—"
    return ROLE_LABELS_RU.get(str(role), str(role))


def network_admin_ids_for_org(data: dict[str, Any], org_id: str | None) -> list[int]:
    if not org_id:
        return []
    out: set[int] = set()
    for uid_s, profiles in data.get("managers", {}).items():
        try:
            uid = int(uid_s)
        except ValueError:
            continue
        for p in profiles if isinstance(profiles, list) else []:
            if not isinstance(p, dict):
                continue
            if (
                p.get("role") == ROLE_NETWORK_ADMIN
                and str(p.get("organization_id")) == str(org_id)
            ):
                out.add(uid)
    return list(out)


def has_chef_access(data: dict[str, Any], user_id: int) -> bool:
    return any(
        isinstance(p, dict) and p.get("role") == ROLE_CHEF
        for p in manager_profiles(data, user_id)
    )


def has_happiness_manager_access(data: dict[str, Any], user_id: int) -> bool:
    return any(
        isinstance(p, dict) and p.get("role") == ROLE_HAPPINESS_MANAGER
        for p in manager_profiles(data, user_id)
    )


def has_ai_auditor_access(data: dict[str, Any], user_id: int) -> bool:
    return any(
        isinstance(p, dict) and p.get("role") in AI_AUDITOR_ROLES
        for p in manager_profiles(data, user_id)
    )


def audit_orgs_for_user(
    data: dict[str, Any], user_id: int, *, is_global_admin: bool
) -> list[tuple[str, str]]:
    """(org_id, name) — для ИИ-аудита без привязки группы точки."""
    orgs: dict[str, Any] = data.get("organizations") or {}
    if not isinstance(orgs, dict):
        return []
    if is_global_admin:
        return [
            (oid, str((org or {}).get("name", oid)))
            for oid, org in sorted(orgs.items())
            if isinstance(org, dict)
        ]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for p in manager_profiles(data, user_id):
        if not isinstance(p, dict):
            continue
        if p.get("role") not in AI_AUDITOR_ROLES:
            continue
        oid = p.get("organization_id")
        if not oid or str(oid) in seen:
            continue
        seen.add(str(oid))
        org = orgs.get(str(oid), {})
        name = str(org.get("name", oid)) if isinstance(org, dict) else str(oid)
        out.append((str(oid), name))
    return sorted(out, key=lambda x: x[1].lower())


def is_happiness_manager_only(data: dict[str, Any], user_id: int) -> bool:
    """Только менеджер по счастью — без опер. ролей точки/сети."""
    profiles = manager_profiles(data, user_id)
    roles = {p.get("role") for p in profiles if isinstance(p, dict)}
    if ROLE_HAPPINESS_MANAGER not in roles:
        return False
    ops = roles & {
        ROLE_NETWORK_ADMIN,
        ROLE_LOCATION_ADMIN,
        ROLE_SENIOR_MANAGER,
    }
    return not ops


def has_ops_staff_access(data: dict[str, Any], user_id: int) -> bool:
    return has_manager_access(data, user_id) or has_chef_access(data, user_id)


_LOCATION_BOUND_ROLES = frozenset(
    {
        ROLE_LOCATION_ADMIN,
        ROLE_SENIOR_MANAGER,
        ROLE_CHEF,
        ROLE_HAPPINESS_MANAGER,
    }
)


def _location_ids_from_profiles(
    data: dict[str, Any], profiles: list[dict[str, Any]], *, roles: frozenset[str]
) -> set[str]:
    ids: set[str] = set()
    for p in profiles:
        if not isinstance(p, dict):
            continue
        role = p.get("role")
        oid = p.get("organization_id")
        if role == ROLE_NETWORK_ADMIN and role in roles and oid:
            for cid, _ in list_chats_for_org(data, str(oid)):
                ids.add(cid)
        elif role in _LOCATION_BOUND_ROLES and role in roles:
            for c in p.get("location_chat_ids") or []:
                ids.add(str(c))
    return ids


def allowed_chat_ids_for_manager(data: dict[str, Any], user_id: int) -> set[str]:
    """chat_id строками, по которым менеджеру можно смотреть аналитику."""
    return _location_ids_from_profiles(
        data,
        manager_profiles(data, user_id),
        roles=frozenset(
            {
                ROLE_NETWORK_ADMIN,
                ROLE_LOCATION_ADMIN,
                ROLE_SENIOR_MANAGER,
                ROLE_HAPPINESS_MANAGER,
            }
        ),
    )


def allowed_chat_ids_for_chef(data: dict[str, Any], user_id: int) -> set[str]:
    return _location_ids_from_profiles(
        data,
        manager_profiles(data, user_id),
        roles=frozenset({ROLE_CHEF}),
    )


def allowed_chat_ids_for_ops_user(data: dict[str, Any], user_id: int) -> set[str]:
    return allowed_chat_ids_for_manager(data, user_id) | allowed_chat_ids_for_chef(
        data, user_id
    )


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
    elif role in _LOCATION_BOUND_ROLES:
        locs = [str(x) for x in (location_chat_ids or [])]
    else:
        locs = [str(x) for x in (location_chat_ids or [])]
    rest.append({"organization_id": org_id, "role": role, "location_chat_ids": locs})
    data["managers"][key] = rest


def remove_manager_binding(
    data: dict[str, Any],
    user_id: int,
    org_id: str,
    *,
    role: str | None = None,
    location_chat_id: str | None = None,
) -> bool:
    """Снять роль. Если location_chat_id — убрать только эту точку у location-ролей."""
    key = str(user_id)
    profiles = manager_profiles(data, user_id)
    if not profiles:
        return False
    new_profiles: list[dict[str, Any]] = []
    removed = False
    for p in profiles:
        if not isinstance(p, dict):
            continue
        if str(p.get("organization_id")) != str(org_id):
            new_profiles.append(p)
            continue
        if role and p.get("role") != role:
            new_profiles.append(p)
            continue
        if location_chat_id and p.get("role") in _LOCATION_BOUND_ROLES:
            locs = [str(x) for x in (p.get("location_chat_ids") or [])]
            locs = [x for x in locs if x != str(location_chat_id)]
            if locs:
                copy = dict(p)
                copy["location_chat_ids"] = locs
                new_profiles.append(copy)
            else:
                removed = True
            continue
        removed = True
    if new_profiles:
        data.setdefault("managers", {})[key] = new_profiles
    else:
        data.get("managers", {}).pop(key, None)
    return removed


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
        if role == ROLE_NETWORK_ADMIN:
            role_ru = ROLE_LABELS_RU[ROLE_NETWORK_ADMIN]
        elif role == ROLE_SENIOR_MANAGER:
            role_ru = ROLE_LABELS_RU[ROLE_SENIOR_MANAGER]
        elif role == ROLE_CHEF:
            role_ru = ROLE_LABELS_RU[ROLE_CHEF]
        elif role == ROLE_HAPPINESS_MANAGER:
            role_ru = ROLE_LABELS_RU[ROLE_HAPPINESS_MANAGER]
        else:
            role_ru = ROLE_LABELS_RU[ROLE_LOCATION_ADMIN]
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
        "4. В чате или в личке: «⏰ Напоминания» — время опроса смены; "
        "либо <code>/settime 22:00</code> и при необходимости "
        "<code>/timezone Europe/Moscow</code>.\n\n"
        f'<a href="https://t.me/{un_e}?startgroup=open">добавить @{un_e} в группу</a>\n\n'
        "Оценки сотрудников идут <b>только в личку</b> по кнопке из группы — так ответ привязан к точке."
    )


def manager_menu_root_markup(
    *,
    show_inbox: bool = False,
    show_commands: bool = False,
    show_ai_audit: bool = False,
    happiness_only: bool = False,
):
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    if happiness_only:
        rows: list[list] = [
            [
                KeyboardButton(text=BTN_FOLDER_ANALYTICS),
                KeyboardButton(text=BTN_AI_AUDIT),
            ],
            [KeyboardButton(text=BTN_TRAINING_MGR)],
            [KeyboardButton(text=BTN_FOLDER_MORE)],
        ]
        return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

    rows = [
        [
            KeyboardButton(text=BTN_FOLDER_ANALYTICS),
            KeyboardButton(text=BTN_FOLDER_SHIFT),
        ],
    ]
    if show_ai_audit:
        rows.append([KeyboardButton(text=BTN_AI_AUDIT), KeyboardButton(text=BTN_TRAINING_MGR)])
    else:
        rows.append([KeyboardButton(text=BTN_TRAINING_MGR)])
    admin_row: list = []
    if show_inbox:
        admin_row.append(KeyboardButton(text=BTN_FOLDER_INBOX))
    if show_commands:
        admin_row.append(KeyboardButton(text=BTN_COMMANDS))
    if admin_row:
        rows.append(admin_row)
    rows.append([KeyboardButton(text=BTN_FOLDER_MORE)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def manager_menu_analytics_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_SIGNALS)],
            [KeyboardButton(text=BTN_MENU_HOME)],
        ],
        resize_keyboard=True,
    )


def audit_session_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_AUDIT_FINISH)],
            [KeyboardButton(text=BTN_AUDIT_CANCEL), KeyboardButton(text=BTN_MENU_HOME)],
        ],
        resize_keyboard=True,
    )


def manager_menu_shift_root_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_FOLDER_SHIFT_DAY),
                KeyboardButton(text=BTN_FOLDER_SHIFT_STOP),
            ],
            [
                KeyboardButton(text=BTN_FOLDER_SHIFT_KITCHEN),
                KeyboardButton(text=BTN_FOLDER_SHIFT_PLAN),
            ],
            [KeyboardButton(text=BTN_MENU_HOME)],
        ],
        resize_keyboard=True,
    )


def manager_menu_shift_day_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DAY_PLAN), KeyboardButton(text=BTN_DAY_CLOSE)],
            [
                KeyboardButton(text=BTN_CHECKLISTS),
                KeyboardButton(text=BTN_OPS_BROADCAST),
            ],
            [KeyboardButton(text=BTN_SHIFT_BACK)],
        ],
        resize_keyboard=True,
    )


def manager_menu_shift_stop_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_STOP_LIST), KeyboardButton(text=BTN_STOP_ADD)],
            [KeyboardButton(text=BTN_STOP_CURRENT)],
            [KeyboardButton(text=BTN_SHIFT_BACK)],
        ],
        resize_keyboard=True,
    )


def manager_menu_shift_kitchen_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_RATE_SHIFT)],
            [KeyboardButton(text=BTN_SHIFT_BACK)],
        ],
        resize_keyboard=True,
    )


def manager_menu_shift_plan_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MONTH_PLAN), KeyboardButton(text=BTN_STAFF_OUT)],
            [KeyboardButton(text=BTN_TASKS)],
            [KeyboardButton(text=BTN_DAY_CLOSE_PAST)],
            [KeyboardButton(text=BTN_SHIFT_BACK)],
        ],
        resize_keyboard=True,
    )


def manager_menu_shift_markup():
    """Корень папки «Смена» (подпапки)."""
    return manager_menu_shift_root_markup()


def chef_menu_reply_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_RATE_SHIFT)],
            [KeyboardButton(text=BTN_FOLDER_SHIFT_STOP)],
            [KeyboardButton(text=BTN_SUPPORT)],
        ],
        resize_keyboard=True,
    )


def chef_menu_stop_markup():
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_STOP_LIST), KeyboardButton(text=BTN_STOP_ADD)],
            [KeyboardButton(text=BTN_STOP_CURRENT)],
            [KeyboardButton(text=BTN_CHEF_BACK)],
        ],
        resize_keyboard=True,
    )


def manager_menu_more_markup(
    *, show_staff_assign: bool = False, happiness_only: bool = False
):
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    rows: list[list] = []
    if show_staff_assign and not happiness_only:
        rows.append(
            [
                KeyboardButton(text=BTN_STAFF_ASSIGN),
                KeyboardButton(text=BTN_STAFF_REMOVE),
            ]
        )
    if happiness_only:
        rows.extend(
            [
                [KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_SUPPORT)],
                [KeyboardButton(text=BTN_MENU_HOME)],
            ]
        )
    else:
        rows.extend(
            [
                [KeyboardButton(text=BTN_REMINDERS)],
                [KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_SUPPORT)],
                [KeyboardButton(text=BTN_CONNECT)],
                [KeyboardButton(text=BTN_MENU_HOME)],
            ]
        )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def manager_menu_reply_markup(
    *,
    show_inbox: bool = False,
    show_commands: bool = False,
    show_ai_audit: bool = False,
    happiness_only: bool = False,
):
    """Корневое меню (папки; «Сводки» и «Команды» — только глобальный админ)."""
    return manager_menu_root_markup(
        show_inbox=show_inbox,
        show_commands=show_commands,
        show_ai_audit=show_ai_audit,
        happiness_only=happiness_only,
    )


def admin_commands_reference_chunks() -> list[str]:
    """Справочник команд и меню — несколько сообщений под лимит Telegram."""
    return [
        (
            "<b>📖 Справочник Pulse Team</b>\n"
            "<i>Глобальный администратор · личка с ботом</i>\n\n"
            "<b>🔧 Организации и доступ</b>\n"
            "<code>/create_org Название</code> — новая организация\n"
            "<code>/orgs</code> — список организаций\n"
            "<code>/del_org org_id</code> — удалить организацию\n"
            "<code>/admin</code> — все чаты, тарифы, расписание\n"
            "<code>/managers</code> — кто привязан (менеджеры, шефы)\n"
            "<code>/myid</code> — ваш Telegram ID\n\n"
            "<b>Привязка людей</b>\n"
            f"Кнопка «{BTN_STAFF_ASSIGN}» в «{BTN_FOLDER_MORE}» — точка → роль → Telegram ID\n"
            "<code>/link_manager ID org_id network</code> — управляющий сети (все точки org)\n"
            "<code>/link_manager ID org_id location CHAT_ID</code> — менеджер точки\n"
            "<code>/link_manager ID org_id senior CHAT_ID</code> — старший менеджер (задания + точка)\n"
            "<code>/link_manager ID org_id chef CHAT_ID</code> — шеф\n"
            "<code>/link_manager ID org_id happiness CHAT_ID</code> — менеджер по счастью\n\n"
            "<b>Роли</b>\n"
            "· <b>Менеджер точки</b> — план, закрытие, отчёты, стоп\n"
            "· <b>Старший менеджер</b> — то же + назначает задания\n"
            "· <b>Управляющий сети</b> — все точки организации\n"
            "· <b>Менеджер по счастью</b> — аналитика + ИИ-аудит здоровья точки\n"
            "· <b>Шеф</b> — стоп-лист и оценка смены кухни в личке\n\n"
            "<b>В группе точки</b>\n"
            "<code>/link_org org_id</code> — привязать чат к организации"
        ),
        (
            "<b>💳 Тарифы и подписка</b>\n"
            "<code>/set_subscription org_id active|grace|suspended</code>\n"
            "<code>/set_tariff CHAT_ID t10|t25|t40|enterprise</code> — в личке\n"
            "<code>/set_tariff t25</code> — в группе (для этого чата)\n"
            "<code>/metrics</code> — уникальные сотрудники и тарифы\n"
            "<code>/tariff_history CHAT_ID</code> — история смен тарифа\n\n"
            "<b>📣 Рассылка</b>\n"
            "<code>/broadcast текст</code> — всем менеджерам\n"
            "или ответьте <code>/broadcast</code> на сообщение\n\n"
            "<b>📁 Сводки (ваше меню)</b>\n"
            f"Кнопка «{BTN_FOLDER_INBOX}» — отчёты по точкам <b>по запросу</b>, "
            "без автоспама в личку."
        ),
        (
            "<b>⏰ В группе ресторана — опрос сотрудников</b>\n"
            "<code>/settime 22:00</code> — 1-е напоминание в личку\n"
            "<code>/settime2 14:00</code> — 2-е напоминание\n"
            "<code>/times</code> — текущее расписание\n"
            "<code>/deltime</code> / <code>/deltime2</code> — убрать время\n"
            "<code>/timezone Europe/Moscow</code> — часовой пояс\n"
            "<code>/send</code> или <code>/send_now</code> — напоминание сейчас\n"
            "<code>/smena_help</code> — краткая справка в группе\n\n"
            "<b>📋 Операционный день</b>\n"
            "<code>/set_ops morning 11:30 chef_stop 11:45 evening 00:00</code>\n"
            "· план менеджера в чат\n"
            "· дедлайн стоп-листа шефа\n"
            "· автозакрытие дня"
        ),
        (
            "<b>📊 Меню менеджера (кнопки)</b>\n"
            f"<b>{BTN_FOLDER_ANALYTICS}</b> — {BTN_REPORT}, {BTN_SIGNALS}\n"
            f"<b>{BTN_AI_AUDIT}</b> — голос/файлы → индекс здоровья + PDF\n"
            f"<b>{BTN_FOLDER_SHIFT}</b> — подпапки:\n"
            f"· {BTN_FOLDER_SHIFT_DAY} — {BTN_DAY_PLAN}, {BTN_DAY_CLOSE}, {BTN_CHECKLISTS}, {BTN_OPS_BROADCAST}\n"
            f"· {BTN_FOLDER_SHIFT_STOP} — {BTN_STOP_LIST}, {BTN_STOP_ADD}, {BTN_STOP_CURRENT}\n"
            f"· {BTN_FOLDER_SHIFT_KITCHEN} — {BTN_RATE_SHIFT}\n"
            f"· {BTN_FOLDER_SHIFT_PLAN} — {BTN_MONTH_PLAN}, {BTN_STAFF_OUT}, {BTN_TASKS}\n"
            f"<b>{BTN_TRAINING_MGR}</b> — инструкция и файлы точки (главный экран)\n"
            f"<b>{BTN_FOLDER_MORE}</b> — {BTN_STAFF_ASSIGN}, {BTN_REMINDERS}, "
            f"{BTN_SUBSCRIPTION}, {BTN_SUPPORT}, {BTN_CONNECT}\n\n"
            "<b>Отчёты</b> — смена / неделя / 3 недели / "
            "<b>месяц (календарь)</b> с планом и эффективностью\n\n"
            "<b>👨‍🍳 Меню шефа (всё в личке)</b>\n"
            f"{BTN_RATE_SHIFT} — оценка смены кухни (утро/вечер — напоминание в личку)\n"
            f"{BTN_STOP_LIST} — задать или заменить стоп целиком (публикует в группу)\n"
            f"{BTN_STOP_ADD} — дописать позиции к текущему стопу\n"
            f"{BTN_STOP_CURRENT} — только посмотреть, без редактирования\n"
            f"{BTN_RATE_SHIFT} — оценка смены кухни\n"
            f"{BTN_OPS_BROADCAST} — личное сообщение шефу и менеджерам точки\n"
            f"{BTN_CHECKLISTS} — пункты открытия/закрытия смены\n"
            f"{BTN_SUPPORT}\n"
            "В «Горящих вопросах» менеджер настраивает <b>кнопки опроса кухни</b>\n\n"
            "<b>Горящие вопросы</b>\n"
            "<code>/signals</code> или <code>/problems</code> — в личке\n\n"
            "<i>Подсказка: /smena_help в личке — краткий список.</i>"
        ),
    ]


def support_only_reply_markup(*, show_training: bool = False):
    """Reply-клавиатура только с кнопкой «Поддержка» (для всех пользователей)."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    row = [KeyboardButton(text=BTN_SUPPORT)]
    if show_training:
        row.insert(0, KeyboardButton(text=BTN_TRAINING))
    return ReplyKeyboardMarkup(keyboard=[row], resize_keyboard=True)


def remove_reply_markup():
    from aiogram.types import ReplyKeyboardRemove

    return ReplyKeyboardRemove()
