"""Tests for Mini App ops helpers: orgs, problems payload, staff list."""
from __future__ import annotations

import miniapp_ops
import problems_pulse
import pulse_model
import staff_assign


def test_manager_can_link_own_org_not_others():
    d = pulse_model.default_data()
    d["organizations"]["org_a"] = {"name": "A"}
    d["organizations"]["org_b"] = {"name": "B"}
    d["chats"]["-1"] = {"title": "Free", "active": True}
    pulse_model.set_manager_binding(
        d, 9, "org_a", pulse_model.ROLE_LOCATION_ADMIN, ["-10"]
    )
    d["chats"]["-10"] = {"title": "Hall", "organization_id": "org_a", "active": True}
    assert miniapp_ops.can_link_org_chats(d, 9, is_global_admin=False, org_id="org_a")
    assert not miniapp_ops.can_link_org_chats(d, 9, is_global_admin=False, org_id="org_b")
    linked = miniapp_ops.link_existing_chat(
        d, org_id="org_a", chat_id=-1, department="kitchen"
    )
    assert linked["ok"] is True
    assert linked["department"] == "kitchen"
    guide = miniapp_ops.connect_guide(d, 9, is_global_admin=False, bot_username="bot")
    assert guide["can_link"] is True
    assert any(c["org_id"] == "org_a" for c in guide["commands"])


def test_create_org_and_link_chat():
    d = pulse_model.default_data()
    d["chats"]["-1001"] = {"title": "Nomad Зал", "active": True}
    created = miniapp_ops.create_org_and_maybe_link(
        d, name="Nomad", chat_id=-1001, department="floor"
    )
    assert created["ok"] is True
    assert created["linked"] is True
    oid = created["org_id"]
    assert d["chats"]["-1001"]["organization_id"] == oid
    orgs = miniapp_ops.orgs_list(d, is_global_admin=True, user_id=1)
    assert any(o["id"] == oid and o["chats"] for o in orgs)


def test_staff_at_chat_lists_existing():
    d = pulse_model.default_data()
    d["organizations"]["org_x"] = {"name": "X"}
    d["chats"]["-10"] = {"title": "Hall", "organization_id": "org_x", "active": True}
    pulse_model.set_manager_binding(
        d, 42, "org_x", pulse_model.ROLE_LOCATION_ADMIN, ["-10"]
    )
    payload = miniapp_ops.staff_at_chat(d, -10)
    assert payload["ok"] is True
    assert any(s["user_id"] == 42 for s in payload["staff"])
    ok = staff_assign.apply_removal(
        d,
        target_uid=42,
        org_id="org_x",
        role=pulse_model.ROLE_LOCATION_ADMIN,
        chat_id=-10,
    )
    assert ok
    assert miniapp_ops.staff_at_chat(d, -10)["staff"] == []


def test_problem_public_shape():
    p = problems_pulse.ProblemRow(
        id="p1",
        organization_id="org_x",
        restaurant_chat_id=-10,
        problem_key="kitchen",
        title="Кухня",
        source_type="button",
        mentions_count=4,
        status=problems_pulse.STATUS_NEW,
        manager_comment=None,
        first_detected_at=None,
        last_detected_at=None,
        resolved_at=None,
    )
    pub = miniapp_ops.problem_public(p)
    assert pub["id"] == "p1"
    assert pub["status_ru"] == "Новая"
    assert "card_text" in pub
