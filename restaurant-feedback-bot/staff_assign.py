"""
Назначение ролей: точка → роль → Telegram ID.
Иерархия:
- глобальный админ — везде;
- управляющий сети — все роли в своей организации;
- старший менеджер — менеджер/старший/шеф на своих точках;
- менеджер точки — шеф и менеджер на своих точках.
"""
from __future__ import annotations

from html import escape
from typing import Any

import pulse_model

ROLE_CODES: dict[str, str] = {
    "nw": pulse_model.ROLE_NETWORK_ADMIN,
    "loc": pulse_model.ROLE_LOCATION_ADMIN,
    "sn": pulse_model.ROLE_SENIOR_MANAGER,
    "chf": pulse_model.ROLE_CHEF,
}

CODE_FOR_ROLE = {v: k for k, v in ROLE_CODES.items()}


def can_manage_staff(data: dict[str, Any], user_id: int, *, is_global_admin: bool) -> bool:
    if is_global_admin:
        return True
    profiles = pulse_model.manager_profiles(data, user_id)
    return any(
        isinstance(p, dict)
        and p.get("role")
        in (
            pulse_model.ROLE_NETWORK_ADMIN,
            pulse_model.ROLE_SENIOR_MANAGER,
            pulse_model.ROLE_LOCATION_ADMIN,
        )
        for p in profiles
    )


def _network_admin_org_ids(data: dict[str, Any], user_id: int) -> set[str]:
    out: set[str] = set()
    for p in pulse_model.manager_profiles(data, user_id):
        if (
            isinstance(p, dict)
            and p.get("role") == pulse_model.ROLE_NETWORK_ADMIN
            and p.get("organization_id")
        ):
            out.add(str(p["organization_id"]))
    return out


def _location_ids_for_assigner(
    data: dict[str, Any], user_id: int, *, is_global_admin: bool
) -> set[str]:
    if is_global_admin:
        chats = data.get("chats", {})
        if not isinstance(chats, dict):
            return set()
        return {
            str(cid)
            for cid, rec in chats.items()
            if isinstance(rec, dict)
            and not rec.get("removed_at")
            and rec.get("active") is not False
            and rec.get("organization_id")
        }
    ids: set[str] = set()
    for p in pulse_model.manager_profiles(data, user_id):
        if not isinstance(p, dict):
            continue
        role = p.get("role")
        oid = p.get("organization_id")
        if role == pulse_model.ROLE_NETWORK_ADMIN and oid:
            for cid, _ in pulse_model.list_chats_for_org(data, str(oid)):
                ids.add(cid)
        elif role in (
            pulse_model.ROLE_LOCATION_ADMIN,
            pulse_model.ROLE_SENIOR_MANAGER,
        ):
            for c in p.get("location_chat_ids") or []:
                ids.add(str(c))
    return ids


def assignable_locations(
    data: dict[str, Any], user_id: int, *, is_global_admin: bool
) -> list[tuple[int, str, str]]:
    """(chat_id, title, org_id)"""
    allowed = _location_ids_for_assigner(data, user_id, is_global_admin=is_global_admin)
    chats = data.get("chats", {})
    out: list[tuple[int, str, str]] = []
    if not isinstance(chats, dict):
        return out
    for cid_s in sorted(allowed, key=lambda x: int(x) if str(x).lstrip("-").isdigit() else x):
        rec = chats.get(cid_s)
        if not isinstance(rec, dict):
            continue
        oid = rec.get("organization_id")
        if not oid:
            continue
        try:
            cid = int(cid_s)
        except ValueError:
            continue
        out.append((cid, str(rec.get("title", cid)), str(oid)))
    return out


def assignable_network_orgs(
    data: dict[str, Any], user_id: int, *, is_global_admin: bool
) -> list[tuple[str, str]]:
    """(org_id, name) — для роли управляющего сети."""
    if is_global_admin:
        orgs = data.get("organizations", {})
        if not isinstance(orgs, dict):
            return []
        return [(oid, str(org.get("name", oid))) for oid, org in sorted(orgs.items())]
    out: list[tuple[str, str]] = []
    orgs = data.get("organizations", {})
    for oid in sorted(_network_admin_org_ids(data, user_id)):
        org = orgs.get(oid, {}) if isinstance(orgs, dict) else {}
        out.append((oid, str(org.get("name", oid))))
    return out


def _assigner_roles(data: dict[str, Any], user_id: int, *, is_global_admin: bool) -> set[str]:
    if is_global_admin:
        return set(ROLE_CODES.values())
    roles: set[str] = set()
    for p in pulse_model.manager_profiles(data, user_id):
        if not isinstance(p, dict):
            continue
        r = p.get("role")
        if r == pulse_model.ROLE_NETWORK_ADMIN:
            roles.update(ROLE_CODES.values())
        elif r == pulse_model.ROLE_SENIOR_MANAGER:
            roles.update(
                {
                    pulse_model.ROLE_SENIOR_MANAGER,
                    pulse_model.ROLE_LOCATION_ADMIN,
                    pulse_model.ROLE_CHEF,
                }
            )
        elif r == pulse_model.ROLE_LOCATION_ADMIN:
            roles.update({pulse_model.ROLE_LOCATION_ADMIN, pulse_model.ROLE_CHEF})
    return roles


def assignable_role_options(
    data: dict[str, Any],
    user_id: int,
    *,
    is_global_admin: bool,
    for_network: bool = False,
) -> list[tuple[str, str]]:
    """(code, label)"""
    allowed = _assigner_roles(data, user_id, is_global_admin=is_global_admin)
    if for_network:
        if pulse_model.ROLE_NETWORK_ADMIN not in allowed:
            return []
        return [("nw", pulse_model.role_label_ru(pulse_model.ROLE_NETWORK_ADMIN))]
    order = [
        (pulse_model.ROLE_LOCATION_ADMIN, "loc"),
        (pulse_model.ROLE_SENIOR_MANAGER, "sn"),
        (pulse_model.ROLE_CHEF, "chf"),
    ]
    out: list[tuple[str, str]] = []
    for role, code in order:
        if role in allowed:
            out.append((code, pulse_model.role_label_ru(role)))
    return out


def can_assign_at_location(
    data: dict[str, Any],
    assigner_uid: int,
    chat_id: int,
    *,
    is_global_admin: bool,
) -> bool:
    return str(chat_id) in _location_ids_for_assigner(
        data, assigner_uid, is_global_admin=is_global_admin
    )


def can_assign_network_org(
    data: dict[str, Any],
    assigner_uid: int,
    org_id: str,
    *,
    is_global_admin: bool,
) -> bool:
    if is_global_admin:
        return org_id in (data.get("organizations") or {})
    return org_id in _network_admin_org_ids(data, assigner_uid)


def validate_assignment(
    data: dict[str, Any],
    assigner_uid: int,
    *,
    is_global_admin: bool,
    target_uid: int,
    org_id: str,
    role: str,
    chat_id: int | None,
) -> tuple[bool, str | None]:
    if target_uid <= 0:
        return False, "Некорректный Telegram ID."
    allowed_roles = _assigner_roles(data, assigner_uid, is_global_admin=is_global_admin)
    if role not in allowed_roles:
        return False, "Недостаточно прав для этой роли."
    if org_id not in (data.get("organizations") or {}):
        return False, "Организация не найдена."
    if role == pulse_model.ROLE_NETWORK_ADMIN:
        if not can_assign_network_org(
            data, assigner_uid, org_id, is_global_admin=is_global_admin
        ):
            return False, "Нет доступа к этой организации."
        return True, None
    if chat_id is None:
        return False, "Укажите точку."
    if not can_assign_at_location(
        data, assigner_uid, chat_id, is_global_admin=is_global_admin
    ):
        return False, "Нет доступа к этой точке."
    rec = data.get("chats", {}).get(str(chat_id))
    if not isinstance(rec, dict) or str(rec.get("organization_id")) != str(org_id):
        return False, "Точка не привязана к организации."
    return True, None


def apply_assignment(
    data: dict[str, Any],
    *,
    target_uid: int,
    org_id: str,
    role: str,
    chat_id: int | None,
) -> None:
    locs = None if role == pulse_model.ROLE_NETWORK_ADMIN else [str(chat_id)]
    pulse_model.set_manager_binding(data, target_uid, org_id, role, locs)


def locations_keyboard(
    locations: list[tuple[int, str, str]],
    *,
    network_orgs: list[tuple[str, str]] | None = None,
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list] = []
    if network_orgs:
        for oid, name in network_orgs[:8]:
            short = name[:24] + ("…" if len(name) > 24 else "")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🌐 {short}",
                        callback_data=f"sa:org:{oid}"[:64],
                    )
                ]
            )
    for cid, title, _oid in locations[:20]:
        short = title[:26] + ("…" if len(title) > 26 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📍 {short}",
                    callback_data=f"sa:loc:{cid}"[:64],
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="sa:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def roles_keyboard(
    roles: list[tuple[str, str]],
    *,
    chat_id: int | None = None,
    org_id: str | None = None,
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list] = []
    for code, label in roles:
        if chat_id is not None:
            cb = f"sa:role:{code}:{chat_id}:{org_id}"[:64]
        else:
            cb = f"sa:role:{code}:0:{org_id}"[:64]
        rows.append([InlineKeyboardButton(text=label, callback_data=cb)])
    back = "sa:menu"
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_start_message() -> str:
    return (
        "<b>Подключить доступ</b>\n\n"
        "1. Выберите <b>точку</b> (или «🌐» — управляющий всей сети)\n"
        "2. Выберите <b>роль</b>\n"
        "3. Отправьте <b>Telegram ID</b> человека\n\n"
        "<i>ID узнать: человек пишет боту </i><code>/myid</code>"
    )


def format_role_pick(chat_title: str) -> str:
    return f"<b>Роль</b> · {escape(chat_title)}\n\nКого подключаем?"


def format_network_role_pick(org_name: str) -> str:
    return (
        f"<b>Управляющий сети</b> · {escape(org_name)}\n\n"
        "Подтвердите роль или вернитесь назад."
    )


def format_await_uid(role_label: str, place_label: str) -> str:
    return (
        f"Роль: <b>{escape(role_label)}</b>\n"
        f"Точка: <b>{escape(place_label)}</b>\n\n"
        "Отправьте <b>числовой Telegram ID</b> (команда <code>/myid</code> у человека)."
    )


def format_success(target_uid: int, role_label: str, place_label: str) -> str:
    return (
        f"✅ Подключено: <code>{target_uid}</code>\n"
        f"<b>{escape(role_label)}</b> · {escape(place_label)}"
    )
