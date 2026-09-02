"""Чат зала / кухни: department + location pairing."""
from __future__ import annotations

import pulse_model as pm


def _base() -> dict:
    return {
        "organizations": {"org_x": {"name": "Test"}},
        "chats": {
            "-1001": {
                "title": "Nomad Зал",
                "organization_id": "org_x",
                "active": True,
            },
            "-1002": {
                "title": "Nomad Кухня",
                "organization_id": "org_x",
                "active": True,
            },
        },
    }


def test_parse_chat_department_aliases():
    assert pm.parse_chat_department("зал") == pm.CHAT_DEPT_FLOOR
    assert pm.parse_chat_department("floor") == pm.CHAT_DEPT_FLOOR
    assert pm.parse_chat_department("кухня") == pm.CHAT_DEPT_KITCHEN
    assert pm.parse_chat_department("kitchen") == pm.CHAT_DEPT_KITCHEN
    assert pm.parse_chat_department("nope") is None


def test_default_department_is_floor():
    data = _base()
    assert pm.chat_department(data, -1001) == pm.CHAT_DEPT_FLOOR


def test_set_and_read_department():
    data = _base()
    assert pm.set_chat_department(data, -1002, "kitchen")
    assert pm.chat_department(data, -1002) == pm.CHAT_DEPT_KITCHEN
    assert data["chats"]["-1002"]["department"] == "kitchen"


def test_pair_hall_kitchen_same_location():
    data = _base()
    pm.set_chat_department(data, -1001, "floor")
    pm.set_chat_department(data, -1002, "kitchen")
    lid = pm.pair_chats_same_location(data, -1002, -1001)
    assert lid
    assert data["chats"]["-1001"]["location_id"] == lid
    assert data["chats"]["-1002"]["location_id"] == lid
    sibs = pm.sibling_chat_ids_for_location(data, -1001)
    assert set(sibs) == {-1001, -1002}


def test_floor_chats_in_org():
    data = _base()
    pm.set_chat_department(data, -1001, "floor")
    pm.set_chat_department(data, -1002, "kitchen")
    floors = pm.floor_chats_in_org(data, "org_x")
    assert floors == [(-1001, "Nomad Зал")]


def test_link_chat_with_department():
    data = _base()
    assert pm.link_chat_to_organization(
        data, -1001, "org_x", department="kitchen"
    )
    assert pm.chat_department(data, -1001) == pm.CHAT_DEPT_KITCHEN


def test_manager_access_includes_sibling_chat():
    data = _base()
    pm.set_chat_department(data, -1001, "floor")
    pm.set_chat_department(data, -1002, "kitchen")
    pm.pair_chats_same_location(data, -1002, -1001)
    data["managers"] = {
        "42": [
            {
                "organization_id": "org_x",
                "role": pm.ROLE_LOCATION_ADMIN,
                "location_chat_ids": ["-1001"],
            }
        ]
    }
    allowed = pm.allowed_chat_ids_for_manager(data, 42)
    assert "-1001" in allowed
    assert "-1002" in allowed
