"""ИИ-аудит для Mini App: сессия, чанки, PDF."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import ai_auditor
import pulse_model


def can_run_audit(data: dict[str, Any], user_id: int, *, is_global_admin: bool) -> bool:
    return is_global_admin or pulse_model.has_ai_auditor_access(data, user_id)


def orgs_payload(
    data: dict[str, Any], user_id: int, *, is_global_admin: bool
) -> list[dict[str, str]]:
    rows = pulse_model.audit_orgs_for_user(
        data, user_id, is_global_admin=is_global_admin
    )
    return [{"id": oid, "title": name} for oid, name in rows]


def session_public(sess: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(sess, dict):
        return None
    chunks = sess.get("chunks") or []
    return {
        "restaurant_id": sess.get("restaurant_id"),
        "restaurant_title": sess.get("restaurant_title"),
        "organization_id": sess.get("organization_id"),
        "started_at": sess.get("started_at"),
        "chunk_count": len(chunks) if isinstance(chunks, list) else 0,
    }


def record_public(record: dict[str, Any]) -> dict[str, Any]:
    analysis = record.get("analysis") if isinstance(record.get("analysis"), dict) else {}
    pdf_name = ""
    pdf_path = str(record.get("pdf_path") or "")
    if pdf_path:
        pdf_name = Path(pdf_path).name
    return {
        "id": record.get("id"),
        "restaurant_title": record.get("restaurant_title"),
        "completed_at": record.get("completed_at"),
        "chunk_count": record.get("chunk_count"),
        "analysis": analysis,
        "pdf_name": pdf_name,
        "overall_index": analysis.get("overall_index"),
        "summary": analysis.get("summary"),
        "index_label": ai_auditor.index_label(int(analysis.get("overall_index") or 0)),
    }
