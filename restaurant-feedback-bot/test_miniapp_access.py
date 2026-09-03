"""QR / identity parsing for Mini App access."""
from __future__ import annotations

import miniapp_access as ma


def test_parse_user_id_plain():
    r = ma.parse_identity_payload("328270272")
    assert r["ok"] and r["kind"] == "user_id" and r["user_id"] == 328270272


def test_parse_tg_user_link():
    r = ma.parse_identity_payload("tg://user?id=123456789")
    assert r["ok"] and r["user_id"] == 123456789


def test_parse_tme_username():
    r = ma.parse_identity_payload("https://t.me/sergey_smirnov")
    assert r["ok"] and r["kind"] == "username" and r["username"] == "sergey_smirnov"


def test_parse_at_username():
    r = ma.parse_identity_payload("@chef_ivan")
    assert r["ok"] and r["username"] == "chef_ivan"


def test_parse_invite_startapp():
    r = ma.parse_identity_payload("https://t.me/mybot?startapp=inv_abc123XYZ")
    assert r["ok"] and r["kind"] == "invite" and r["token"].startswith("inv_")


def test_create_and_redeem_invite():
    import pulse_model
    import staff_assign

    data = pulse_model.default_data()
    data["organizations"]["org_1"] = {"name": "Net"}
    data["chats"]["-10"] = {
        "title": "Hall",
        "organization_id": "org_1",
        "active": True,
    }
    inv = ma.create_invite(
        data,
        created_by=1,
        org_id="org_1",
        role=pulse_model.ROLE_LOCATION_ADMIN,
        chat_id=-10,
        bot_username="smena_bot",
    )
    assert "startapp=" in inv["link"]
    ok, err, rec = ma.redeem_invite(data, token=inv["token"], user_id=99)
    assert ok and err is None
    assert rec["used_by"] == 99
    profiles = pulse_model.manager_profiles(data, 99)
    assert any(p.get("role") == pulse_model.ROLE_LOCATION_ADMIN for p in profiles)
