"""Tests for Telegram Mini App role routing and initData helpers."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import miniapp_api
import pulse_model


def _sign_init_data(bot_token: str, fields: dict[str, str]) -> str:
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields = dict(fields)
    fields["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_validate_webapp_init_data_ok():
    token = "123456:ABC"
    user = json.dumps({"id": 42, "first_name": "Ann"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    init = _sign_init_data(token, fields)
    parsed = miniapp_api.validate_webapp_init_data(init, bot_token=token)
    assert parsed is not None
    assert miniapp_api.parse_user(parsed)["id"] == 42


def test_validate_webapp_init_data_bad_hash():
    token = "123456:ABC"
    init = "auth_date=1&user=%7B%22id%22%3A1%7D&hash=deadbeef"
    assert miniapp_api.validate_webapp_init_data(init, bot_token=token) is None


def test_resolve_roles():
    d = pulse_model.default_data()
    d["organizations"]["org_1"] = {"name": "Net"}
    d["chats"]["-10"] = {
        "title": "Hall",
        "organization_id": "org_1",
        "active": True,
    }
    pulse_model.set_manager_binding(
        d, 7, "org_1", pulse_model.ROLE_HAPPINESS_MANAGER, ["-10"]
    )
    p = miniapp_api.resolve_miniapp_role(d, 7, is_global_admin=False)
    assert p["role"] == "happiness"
    assert any(s["id"] == "ai_audit" and s["status"] == "ready" for s in p["screens"])
    assert not any(s["id"] == "consulting" for s in p["screens"])
    assert p["feedback_in_bot"] is True

    pulse_model.set_manager_binding(
        d, 8, "org_1", pulse_model.ROLE_LOCATION_ADMIN, ["-10"]
    )
    m = miniapp_api.resolve_miniapp_role(d, 8, is_global_admin=False)
    assert m["role"] == "manager"
    ids = {s["id"] for s in m["screens"]}
    assert {"home", "reviews", "engagement", "signals", "ai"} <= ids
    assert "ai_audit" not in ids
    assert "consulting" not in ids
    assert m.get("app_mode") == "app"
    assert any(s["id"] == "access" and s["status"] == "ready" for s in m["screens"])

    staff = miniapp_api.resolve_miniapp_role(d, 99, is_global_admin=False)
    assert staff["role"] == "staff"
    assert any(s["id"] == "feedback_bot" for s in staff["screens"])

    owner = miniapp_api.resolve_miniapp_role(d, 1, is_global_admin=True)
    assert owner["role"] == "owner"
    assert any(s["id"] == "ai_audit" and s["status"] == "ready" for s in owner["screens"])
    assert any(s["id"] == "consulting" and s["status"] == "ready" for s in owner["screens"])
    assert any(s["id"] == "billing" for s in owner["screens"])


def test_bot_description_mentions_miniapp_and_bot_feedback():
    import bot as bot_mod

    assert "Mini App" in bot_mod.BOT_DESCRIPTION
    assert "боте" in bot_mod.BOT_SHORT_DESCRIPTION.lower() or "бот" in bot_mod.BOT_SHORT_DESCRIPTION.lower()
    assert "отзыв" in bot_mod.PRIVATE_WELCOME.lower() or "отзыв" in bot_mod.BOT_DESCRIPTION.lower()
