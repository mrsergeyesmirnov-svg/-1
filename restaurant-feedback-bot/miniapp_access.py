"""
Доступы в Mini App: QR / @username / ID + одноразовые инвайт-ссылки.

QR личного профиля Telegram часто даёт t.me/username или tg://user?id=…
Надёжный путь без myid: управ создаёт инвайт-QR → сотрудник открывает Mini App.
"""
from __future__ import annotations

import re
import secrets
import time
from typing import Any

import pulse_model
import staff_assign

INVITE_TTL_SEC = 48 * 3600
INVITE_PREFIX = "inv_"


def parse_identity_payload(raw: str) -> dict[str, Any]:
    """
    Разбор QR / вставленного текста → user_id | username | invite.
    """
    text = (raw or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}

    # наш инвайт
    m_inv = re.search(r"(?:startapp=|start=)?(inv_[A-Za-z0-9_-]{6,40})", text)
    if m_inv:
        return {"ok": True, "kind": "invite", "token": m_inv.group(1)}

    m_id = re.search(
        r"(?:tg://user\?id=|(?:^|[?&])id=)(\d{5,15})\b", text, re.I
    )
    if m_id:
        return {"ok": True, "kind": "user_id", "user_id": int(m_id.group(1))}

    if re.fullmatch(r"\d{5,15}", text):
        return {"ok": True, "kind": "user_id", "user_id": int(text)}

    m_user = re.search(
        r"(?:https?://)?(?:t\.me/|telegram\.me/|tg://resolve\?domain=)"
        r"@?([A-Za-z0-9_]{4,32})\b",
        text,
        re.I,
    )
    if m_user:
        un = m_user.group(1)
        if un.lower() not in ("joinchat", "addstickers", "share", "proxy", "socks", "iv"):
            return {"ok": True, "kind": "username", "username": un}

    if text.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{4,32}", text):
        return {"ok": True, "kind": "username", "username": text[1:]}

    if re.fullmatch(r"[A-Za-z0-9_]{4,32}", text) and not text.isdigit():
        return {"ok": True, "kind": "username", "username": text}

    return {
        "ok": False,
        "error": "unparsed",
        "hint": (
            "Не распознали QR. Попросите человека открыть «Настройки → QR-код» "
            "или создайте инвайт-ссылку в приложении."
        ),
    }


def _ensure_invites(data: dict[str, Any]) -> dict[str, Any]:
    inv = data.get("access_invites")
    if not isinstance(inv, dict):
        inv = {}
        data["access_invites"] = inv
    return inv


def create_invite(
    data: dict[str, Any],
    *,
    created_by: int,
    org_id: str,
    role: str,
    chat_id: int | None,
    bot_username: str,
) -> dict[str, Any]:
    token = INVITE_PREFIX + secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")[:12]
    now = int(time.time())
    rec = {
        "token": token,
        "org_id": org_id,
        "role": role,
        "chat_id": chat_id,
        "created_by": created_by,
        "created_at": now,
        "expires_at": now + INVITE_TTL_SEC,
        "used_by": None,
        "used_at": None,
    }
    _ensure_invites(data)[token] = rec
    un = (bot_username or "").lstrip("@")
    link = f"https://t.me/{un}?startapp={token}"
    return {
        "token": token,
        "link": link,
        "expires_at": rec["expires_at"],
        "role": role,
        "role_label": pulse_model.role_label_ru(role),
        "org_id": org_id,
        "chat_id": chat_id,
    }


def redeem_invite(
    data: dict[str, Any],
    *,
    token: str,
    user_id: int,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    inv = _ensure_invites(data).get(token)
    if not isinstance(inv, dict):
        return False, "Инвайт не найден или уже недействителен.", None
    now = int(time.time())
    if inv.get("used_by"):
        if int(inv["used_by"]) == int(user_id):
            return True, None, inv
        return False, "Эту ссылку уже использовали.", None
    if int(inv.get("expires_at") or 0) < now:
        return False, "Срок инвайта истёк. Попросите новую ссылку.", None
    role = str(inv.get("role") or "")
    org_id = str(inv.get("org_id") or "")
    chat_id = inv.get("chat_id")
    if chat_id is not None:
        chat_id = int(chat_id)
    # Инвайт уже проверен создателем — применяем напрямую
    staff_assign.apply_assignment(
        data,
        target_uid=int(user_id),
        org_id=org_id,
        role=role,
        chat_id=chat_id,
    )
    inv["used_by"] = int(user_id)
    inv["used_at"] = now
    return True, None, inv


def access_options_payload(
    data: dict[str, Any],
    user_id: int,
    *,
    is_global_admin: bool,
) -> dict[str, Any]:
    if not staff_assign.can_manage_staff(data, user_id, is_global_admin=is_global_admin):
        return {"ok": False, "error": "forbidden", "can_manage": False}
    locs = staff_assign.assignable_locations(
        data, user_id, is_global_admin=is_global_admin
    )
    nets = staff_assign.assignable_network_orgs(
        data, user_id, is_global_admin=is_global_admin
    )
    roles_loc = staff_assign.assignable_role_options(
        data, user_id, is_global_admin=is_global_admin, for_network=False
    )
    roles_net = staff_assign.assignable_role_options(
        data, user_id, is_global_admin=is_global_admin, for_network=True
    )
    return {
        "ok": True,
        "can_manage": True,
        "locations": [
            {"id": str(cid), "title": title, "org_id": oid} for cid, title, oid in locs
        ],
        "network_orgs": [{"id": oid, "title": name} for oid, name in nets],
        "roles": [{"code": c, "role": staff_assign.ROLE_CODES[c], "label": lab} for c, lab in roles_loc],
        "network_roles": [
            {"code": c, "role": staff_assign.ROLE_CODES[c], "label": lab}
            for c, lab in roles_net
        ],
        "hint": (
            "Выберите роль и точку → отсканируйте QR человека в Telegram "
            "или создайте инвайт-QR: сотрудник открывает ссылку — доступ выдаётся сам."
        ),
    }


def grant_by_identity(
    data: dict[str, Any],
    assigner_uid: int,
    *,
    is_global_admin: bool,
    target_uid: int,
    org_id: str,
    role: str,
    chat_id: int | None,
) -> tuple[bool, str | None]:
    return staff_assign.validate_assignment(
        data,
        assigner_uid,
        is_global_admin=is_global_admin,
        target_uid=target_uid,
        org_id=org_id,
        role=role,
        chat_id=chat_id,
    )
