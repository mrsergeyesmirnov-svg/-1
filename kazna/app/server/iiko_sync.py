from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .iiko_client import (
    IikoClient,
    IikoError,
    default_period,
    invoice_to_payment_fields,
)
from .models import Account, IikoSettings, Payment


def get_or_create_settings(db: Session) -> IikoSettings:
    row = db.scalar(select(IikoSettings).order_by(IikoSettings.id).limit(1))
    if row:
        return row
    row = IikoSettings(api_login="", days_back=30, enabled=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def settings_public(row: IikoSettings) -> dict:
    login = row.api_login or ""
    masked = ""
    if login:
        masked = login[:3] + "…" + login[-2:] if len(login) > 6 else "***"
    return {
        "configured": bool(login),
        "apiLoginMasked": masked,
        "organizationId": row.organization_id,
        "organizationName": row.organization_name,
        "daysBack": row.days_back or 30,
        "note": row.note,
        "enabled": bool(row.enabled),
        "lastSyncAt": row.last_sync_at.isoformat() if row.last_sync_at else None,
        "lastError": row.last_error,
        "lastResult": row.last_result,
    }


def _ensure_org_account(db: Session, org_name: str | None) -> None:
    if not org_name:
        return
    existing = db.scalar(select(Account).where(Account.name == org_name))
    if existing:
        return
    kind = "ИП" if org_name.upper().startswith("ИП") else "ООО / юрлицо"
    db.add(Account(name=org_name, kind=kind, balance=0, org=org_name))


def sync_iiko_invoices(
    db: Session,
    *,
    api_login: str | None = None,
    organization_id: str | None = None,
    days_back: int | None = None,
    replace: bool = False,
) -> dict:
    settings = get_or_create_settings(db)
    login = (api_login or settings.api_login or os.environ.get("IIKO_API_LOGIN") or "").strip()
    if not login:
        raise IikoError("Сначала сохраните apiLogin iiko")

    org_id = organization_id or settings.organization_id
    days = days_back if days_back is not None else (settings.days_back or 30)
    date_from, date_to = default_period(days)

    client = IikoClient(login)
    client.auth()
    orgs = client.organizations()
    if not orgs:
        raise IikoError("У apiLogin нет доступных организаций")

    if org_id:
        selected = [o for o in orgs if o["id"] == org_id]
        if not selected:
            raise IikoError(f"Организация {org_id} недоступна этому apiLogin")
    else:
        selected = orgs

    if replace:
        for p in db.scalars(select(Payment).where(Payment.source == "iiko")).all():
            db.delete(p)
        db.flush()

    imported = 0
    updated = 0
    closed = 0
    skipped = 0
    per_org = []

    for org in selected:
        names = client.counteragents(org["id"])
        try:
            invoices = client.incoming_invoices(org["id"], date_from, date_to)
        except IikoError as e:
            per_org.append({"org": org["name"], "error": str(e), "imported": 0})
            continue

        org_count = 0
        for inv in invoices:
            fields = invoice_to_payment_fields(
                inv, org_name=org["name"], counterparty_names=names
            )
            if not fields:
                skipped += 1
                continue

            existing = db.scalar(
                select(Payment).where(Payment.external_id == fields["external_id"])
            )
            if existing:
                was_done = existing.status == "done"
                existing.title = fields["title"]
                existing.counterparty = fields["counterparty"]
                existing.amount = fields["amount"]
                existing.pay_date = fields["pay_date"]
                existing.account_name = fields["account_name"]
                existing.note = fields["note"]
                existing.source = "iiko"
                if fields["status"] == "done":
                    existing.status = "done"
                    if not was_done:
                        closed += 1
                updated += 1
            else:
                db.add(Payment(**fields))
                imported += 1
                if fields["status"] == "done":
                    closed += 1
            org_count += 1

        _ensure_org_account(db, org["name"])
        per_org.append(
            {
                "org": org["name"],
                "orgId": org["id"],
                "invoices": len(invoices),
                "imported": org_count,
            }
        )

    settings.last_sync_at = datetime.utcnow()
    settings.last_error = None
    settings.last_result = (
        f"+{imported} новых, {updated} обновлено, закрыто оплат: {closed}, "
        f"период {date_from}—{date_to}"
    )
    if organization_id:
        settings.organization_id = organization_id
        match = next((o for o in orgs if o["id"] == organization_id), None)
        if match:
            settings.organization_name = match["name"]
    db.commit()

    return {
        "ok": True,
        "imported": imported,
        "updated": updated,
        "closed": closed,
        "skipped": skipped,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "organizations": per_org,
        "message": settings.last_result,
    }
