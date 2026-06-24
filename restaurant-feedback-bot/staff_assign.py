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
    mode: str = "assign",
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    loc_pfx = "rmloc" if mode == "remove" else "loc"
    org_pfx = "rmorg" if mode == "remove" else "org"
    cancel_cb = "sa:rmmenu" if mode == "remove" else "sa:cancel"
    rows: list[list] = []
    if network_orgs:
        for oid, name in network_orgs[:8]:
            short = name[:24] + ("…" if len(name) > 24 else "")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🌐 {short}",
                        callback_data=f"sa:{org_pfx}:{oid}"[:64],
                    )
                ]
            )
    for cid, title, _oid in locations[:20]:
        short = title[:26] + ("…" if len(title) > 26 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📍 {short}",
                    callback_data=f"sa:{loc_pfx}:{cid}"[:64],
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=cancel_cb)])
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


def can_remove_target(
    data: dict[str, Any],
    assigner_uid: int,
    *,
    is_global_admin: bool,
    target_uid: int,
    org_id: str,
    role: str,
    chat_id: int | None,
) -> bool:
    if target_uid == assigner_uid and not is_global_admin:
        return False
    ok, _ = validate_assignment(
        data,
        assigner_uid,
        is_global_admin=is_global_admin,
        target_uid=target_uid,
        org_id=org_id,
        role=role,
        chat_id=chat_id,
    )
    return ok


def list_staff_at_location(
    data: dict[str, Any], chat_id: int, org_id: str
) -> list[tuple[int, str, str]]:
    """(uid, role_label, role_code)"""
    out: list[tuple[int, str, str]] = []
    managers = data.get("managers", {})
    if not isinstance(managers, dict):
        return out
    cid_s = str(chat_id)
    for uid_s, profiles in managers.items():
        try:
            uid = int(uid_s)
        except (TypeError, ValueError):
            continue
        if not isinstance(profiles, list):
            continue
        for p in profiles:
            if not isinstance(p, dict):
                continue
            if str(p.get("organization_id")) != str(org_id):
                continue
            role = p.get("role")
            if role == pulse_model.ROLE_NETWORK_ADMIN:
                continue
            locs = [str(x) for x in (p.get("location_chat_ids") or [])]
            if cid_s not in locs:
                continue
            code = CODE_FOR_ROLE.get(role, role)
            out.append((uid, pulse_model.role_label_ru(role), code))
    return sorted(out, key=lambda x: (x[1], x[0]))


def list_network_admins(data: dict[str, Any], org_id: str) -> list[int]:
    out: list[int] = []
    managers = data.get("managers", {})
    if not isinstance(managers, dict):
        return out
    for uid_s, profiles in managers.items():
        try:
            uid = int(uid_s)
        except (TypeError, ValueError):
            continue
        if not isinstance(profiles, list):
            continue
        for p in profiles:
            if (
                isinstance(p, dict)
                and str(p.get("organization_id")) == str(org_id)
                and p.get("role") == pulse_model.ROLE_NETWORK_ADMIN
            ):
                out.append(uid)
                break
    return sorted(set(out))


def apply_removal(
    data: dict[str, Any],
    *,
    target_uid: int,
    org_id: str,
    role: str,
    chat_id: int | None,
) -> bool:
    loc = None if role == pulse_model.ROLE_NETWORK_ADMIN else str(chat_id) if chat_id else None
    return pulse_model.remove_manager_binding(
        data,
        target_uid,
        org_id,
        role=role,
        location_chat_id=loc,
    )


def format_remove_start_message() -> str:
    return (
        "<b>Отозвать доступ</b>\n\n"
        "Выберите <b>точку</b> или «🌐» — управляющий сети.\n"
        "Затем выберите человека и подтвердите."
    )


def format_remove_pick(chat_title: str) -> str:
    return f"<b>Кого отключить</b> · {escape(chat_title)}"


def format_remove_network_pick(org_name: str) -> str:
    return f"<b>Управляющие сети</b> · {escape(org_name)}"


def format_remove_confirm(target_uid: int, role_label: str, place_label: str) -> str:
    return (
        f"Отозвать доступ у <code>{target_uid}</code>?\n"
        f"<b>{escape(role_label)}</b> · {escape(place_label)}"
    )


def staff_remove_keyboard(
    entries: list[tuple[int, str, str]],
    *,
    chat_id: int | None = None,
    org_id: str,
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list] = []
    for uid, label, code in entries[:25]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label} · {uid}",
                    callback_data=f"sa:rm:{uid}:{code}:{chat_id or 0}:{org_id}"[:64],
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="sa:rmmenu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def remove_confirm_keyboard(
    target_uid: int, code: str, chat_id: int | None, org_id: str
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = chat_id or 0
    cb = f"sa:rmok:{target_uid}:{code}:{cid}:{org_id}"[:64]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, отозвать", callback_data=cb)],
            [InlineKeyboardButton(text="Отмена", callback_data="sa:rmmenu")],
        ]
    )

