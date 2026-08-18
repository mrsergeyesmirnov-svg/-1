"""Терминальный опрос iiko: permit, CSV, анонимный дожим."""
from __future__ import annotations

import iiko_bridge as b


def test_csv_import_and_survey():
    data: dict = {}
    b.bind_point(data, iiko_org_id="org-1", chat_id=-100)
    n = b.import_employees_csv(
        data,
        "id;фио;должность\n"
        "aaa-1;Иванов;официант\n"
        "bbb-2;Петров;повар\n",
        default_chat_id=-100,
    )
    assert n == 2
    rec = b.submit_shift_survey(
        data,
        employee_id="aaa-1",
        rating=4,
        blocker="ok",
        iiko_org_id="org-1",
    )
    assert rec["ok"] is True
    assert rec["department"] == "hall"
    assert b.clockout_permit(data, "aaa-1")["ok"] is True
    assert b.clockout_permit(data, "bbb-2")["ok"] is False


def test_csv_uuid_braces_and_guid_header():
    data: dict = {}
    n = b.import_employees_csv(
        data,
        "guid,имя,роль\n"
        "{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee},Иванов,waiter\n",
    )
    assert n == 1
    assert data["iiko_staff"][0]["iiko_employee_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert data["iiko_staff"][0]["telegram_id"] is None


def test_survey_without_staff_directory():
    data: dict = {}
    b.bind_point(data, iiko_org_id="org-1", chat_id=-100)
    rec = b.submit_shift_survey(
        data,
        employee_id="till-session-user",
        rating=5,
        blocker="ok",
        iiko_org_id="org-1",
        department="kitchen",
    )
    assert rec["department"] == "kitchen"
    assert b.clockout_permit(data, "till-session-user")["ok"] is True


def test_nudge_after_three_bad():
    data: dict = {}
    b.bind_point(data, iiko_org_id="org-1", chat_id=-50, kitchen_chat_id=-51)
    for i, emp in enumerate(("e1", "e2", "e3")):
        b.submit_shift_survey(
            data,
            employee_id=emp,
            rating=1,
            blocker="stress",
            iiko_org_id="org-1",
            department="kitchen",
            day="2026-08-18",
        )
        nudge = b.take_nudge(
            data, chat_id=-50, department="kitchen", day="2026-08-18"
        )
        if i < 2:
            assert nudge is None
        else:
            assert nudge is not None
            assert nudge["chat_id"] == -51
            assert "без имён" in nudge["text"]
            assert "касс" in nudge["text"]
    assert b.take_nudge(data, chat_id=-50, department="kitchen", day="2026-08-18") is None


def test_survey_unknown_org():
    data: dict = {}
    try:
        b.submit_shift_survey(
            data, employee_id="x", rating=5, blocker="ok", iiko_org_id="nope"
        )
        assert False
    except KeyError:
        pass
