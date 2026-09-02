"""Tests for AI auditor: sessions, normalize, PDF."""
from __future__ import annotations

import json
from pathlib import Path

import ai_auditor
import audit_pdf
import pulse_model
import staff_assign


def test_happiness_role_access(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path))
    d = pulse_model.default_data()
    d["organizations"]["org_x"] = {"name": "Сеть"}
    d["chats"]["-42"] = {
        "title": "Зал",
        "organization_id": "org_x",
        "active": True,
    }
    pulse_model.set_manager_binding(
        d, 7, "org_x", pulse_model.ROLE_HAPPINESS_MANAGER, ["-42"]
    )
    assert pulse_model.has_manager_access(d, 7)
    assert pulse_model.has_happiness_manager_access(d, 7)
    assert pulse_model.has_ai_auditor_access(d, 7)
    assert pulse_model.is_happiness_manager_only(d, 7)
    assert "-42" in pulse_model.allowed_chat_ids_for_manager(d, 7)

    pulse_model.set_manager_binding(
        d, 8, "org_x", pulse_model.ROLE_NETWORK_ADMIN, None
    )
    assert pulse_model.has_ai_auditor_access(d, 8)
    assert not pulse_model.is_happiness_manager_only(d, 8)


def test_audit_orgs_for_user():
    d = pulse_model.default_data()
    d["organizations"]["org_nomad"] = {"name": "Nomad"}
    d["organizations"]["org_other"] = {"name": "Other"}
    all_orgs = pulse_model.audit_orgs_for_user(d, 1, is_global_admin=True)
    assert ("org_nomad", "Nomad") in all_orgs
    pulse_model.set_manager_binding(
        d, 9, "org_nomad", pulse_model.ROLE_NETWORK_ADMIN, None
    )
    net = pulse_model.audit_orgs_for_user(d, 9, is_global_admin=False)
    assert net == [("org_nomad", "Nomad")]


def test_assignable_happiness_role():
    d = pulse_model.default_data()
    opts = staff_assign.assignable_role_options(d, 1, is_global_admin=True)
    codes = [c for c, _ in opts]
    assert "hm" in codes
    assert staff_assign.ROLE_CODES["hm"] == pulse_model.ROLE_HAPPINESS_MANAGER


def test_normalize_analysis_fills_blocks():
    raw = {
        "overall_index": 91,
        "summary": "Сильная точка",
        "blocks": {"people": {"score": 95, "findings": ["команда спокойная"]}},
        "top_priorities": ["держать ритм"],
        "quotes": ["нам ок"],
    }
    a = ai_auditor.normalize_analysis(raw)
    assert a["overall_index"] == 91
    assert a["blocks"]["people"]["score"] == 95
    assert a["blocks"]["processes"]["score"] == 50
    assert "мало данных" in a["blocks"]["guest"]["findings"][0].lower()
    assert a["top_priorities"] == ["держать ритм"]


def test_session_chunks_and_text(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path))
    ai_auditor.start_session(11, restaurant_id="-1", restaurant_title="Test")
    sess, err = ai_auditor.add_chunk(
        11,
        kind="voice",
        file_id="fid1",
        filename="voice.ogg",
        size=1000,
    )
    assert err is None
    assert len(sess["chunks"]) == 1
    store = ai_auditor.load_store()
    active = store["active"]["11"]
    active["chunks"].append({"kind": "text", "text": "кухня тормозит на пике"})
    store["active"]["11"] = active
    ai_auditor.save_store(store)
    assert ai_auditor.get_active(11) is not None
    assert ai_auditor.cancel_session(11) is True
    assert ai_auditor.get_active(11) is None


def test_audit_pdf_bytes():
    a = ai_auditor.normalize_analysis(
        {
            "overall_index": 64,
            "summary": "Средний уровень, есть риски на отдаче.",
            "blocks": {
                "people": {
                    "score": 70,
                    "findings": ["текучка"],
                    "risks": ["выгорание"],
                    "quick_wins": ["1:1"],
                },
                "processes": {"score": 55, "findings": ["стоп рваный"]},
                "guest": {"score": 68, "findings": ["сервис держится"]},
                "finance_ops": {"score": 60, "findings": ["фудкост не ясен"]},
            },
            "top_priorities": ["починить отдачу", "прозрачный фудкост"],
            "quotes": ["на пике все бегают"],
        }
    )
    pdf = audit_pdf.build_audit_pdf_bytes(
        a,
        location="Тестовая точка",
        audit_id="aud_unit",
        completed_at="2026-08-26T12:00:00+00:00",
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_tg_summary_html():
    rec = {
        "id": "aud_x",
        "restaurant_title": "Кафе",
        "analysis": {
            "overall_index": 40,
            "summary": "Слабо",
            "blocks": {
                k: {"score": 40} for k in ai_auditor.BLOCK_KEYS
            },
            "top_priorities": ["A"],
        },
    }
    html = ai_auditor.tg_summary_html(rec)
    assert "40/100" in html
    assert "Кафе" in html
