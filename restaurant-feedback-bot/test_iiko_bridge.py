"""Шаг 1 iiko bridge: карта людей и permit после оценки."""
from __future__ import annotations

import iiko_bridge as b


def test_upsert_and_permit():
    data: dict = {}
    b.upsert_staff(
        data,
        telegram_id=111,
        iiko_employee_id="emp-a",
        chat_id=-1001,
        role="floor",
    )
    assert b.staff_by_telegram(data, 111)["iiko_employee_id"] == "emp-a"
    rec = b.grant_permit(data, telegram_id=111, day="2026-08-18")
    assert rec and rec["ok"] is True
    ok = b.clockout_permit(data, "emp-a", "2026-08-18")
    assert ok["ok"] is True
    no = b.clockout_permit(data, "emp-a", "2026-08-17")
    assert no["ok"] is False


def test_unknown_user_no_permit():
    data: dict = {}
    assert b.grant_permit(data, telegram_id=999) is None


def test_out_token():
    data: dict = {}
    b.upsert_staff(data, telegram_id=5, iiko_employee_id="e1", chat_id=-2)
    tok = b.issue_out_token(data, "e1")
    row = b.consume_out_token(data, tok)
    assert row["telegram_id"] == 5
    assert b.consume_out_token(data, tok) is None


def test_api_key():
    assert b.api_key_ok("abc", "abc")
    assert not b.api_key_ok("abc", "abd")
    assert not b.api_key_ok(None, "abc")
