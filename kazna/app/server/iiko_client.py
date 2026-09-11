"""iikoCloud API client for Казна.

Auth: POST /api/1/access_token { apiLogin }
Docs: https://api-ru.iiko.services/
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import httpx

DEFAULT_BASE = "https://api-ru.iiko.services"


class IikoError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class IikoClient:
    def __init__(self, api_login: str, base_url: str = DEFAULT_BASE, timeout: float = 45.0):
        self.api_login = (api_login or "").strip()
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self._token: str | None = None

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise IikoError("Нет токена iiko — сначала auth()")
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def auth(self) -> str:
        if not self.api_login:
            raise IikoError("Пустой apiLogin")
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.base_url}/api/1/access_token",
                json={"apiLogin": self.api_login},
            )
        if r.status_code >= 400:
            raise IikoError(f"iiko auth {r.status_code}: {r.text[:300]}", r.status_code)
        data = r.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise IikoError("iiko не вернул token")
        self._token = token
        return token

    def _post(self, path: str, body: dict | None = None) -> Any:
        if not self._token:
            self.auth()
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(f"{self.base_url}{path}", headers=self._headers(), json=body or {})
            # token expired → once
            if r.status_code == 401:
                self.auth()
                r = client.post(f"{self.base_url}{path}", headers=self._headers(), json=body or {})
        if r.status_code >= 400:
            raise IikoError(f"iiko {path} → {r.status_code}: {r.text[:400]}", r.status_code)
        if not r.content:
            return None
        return r.json()

    def organizations(self) -> list[dict]:
        data = self._post("/api/1/organizations", {"organizationIds": None, "returnAdditionalInfo": False})
        orgs = (data or {}).get("organizations") or []
        return [
            {
                "id": o.get("id"),
                "name": o.get("name") or o.get("restaurantAddress") or o.get("id"),
                "code": o.get("code"),
            }
            for o in orgs
            if o.get("id")
        ]

    def counteragents(self, organization_id: str, types: list[str] | None = None) -> dict[str, str]:
        """Return map id → name."""
        mapping: dict[str, str] = {}
        offset = 0
        limit = 200
        while True:
            body = {
                "organizationId": organization_id,
                "limit": limit,
                "offset": offset,
                "type": types or ["supplier"],
            }
            try:
                data = self._post("/api/inventory/v1/counteragents", body)
            except IikoError:
                # Some API keys lack inventory scope — soft fail
                break
            rows = (data or {}).get("counteragents") or (data if isinstance(data, list) else [])
            if not rows:
                break
            for row in rows:
                cid = row.get("id") or row.get("counteragentId") or row.get("guid")
                name = row.get("name") or row.get("legalName") or row.get("displayName")
                if cid and name:
                    mapping[str(cid)] = str(name)
            if len(rows) < limit:
                break
            offset += limit
            if offset > 5000:
                break
        return mapping

    def incoming_invoices(self, organization_id: str, date_from: date, date_to: date) -> list[dict]:
        data = self._post(
            "/api/inventory/v1/incoming_invoice/list",
            {
                "organizationId": organization_id,
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
            },
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items") or data.get("documents") or data.get("incomingInvoices") or []
        return []


def parse_iiko_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    if not s:
        return None
    # 2026-07-21 or ISO datetime
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def invoice_amount(inv: dict) -> float:
    items = inv.get("items") or []
    total = 0.0
    for it in items:
        try:
            total += float(it.get("sum") if it.get("sum") is not None else 0)
        except (TypeError, ValueError):
            continue
    if total > 0:
        return abs(total)
    for key in ("sum", "totalSum", "amount", "incomingSum"):
        if inv.get(key) is not None:
            try:
                return abs(float(inv[key]))
            except (TypeError, ValueError):
                pass
    return 0.0


def invoice_to_payment_fields(
    inv: dict,
    *,
    org_name: str | None,
    counterparty_names: dict[str, str],
) -> dict | None:
    amount = invoice_amount(inv)
    if amount <= 0:
        return None
    if str(inv.get("status") or "").upper() == "DELETED":
        return None

    doc_id = inv.get("documentId") or inv.get("id") or inv.get("number")
    if not doc_id:
        return None

    ca_id = str(inv.get("counteragent") or "")
    counterparty = counterparty_names.get(ca_id) or (ca_id[:8] + "…" if ca_id else None)

    number = inv.get("number") or inv.get("invoice") or inv.get("incomingDocumentNumber") or ""
    comment = (inv.get("comment") or "").strip()
    purpose = comment or (f"Накладная {number}" if number else "Приходная накладная iiko")

    paid_on = parse_iiko_date(inv.get("paymentDate"))
    due = parse_iiko_date(inv.get("dueDate")) or parse_iiko_date(inv.get("incomingDate")) or parse_iiko_date(
        inv.get("date")
    )
    pay_date = paid_on or due
    status = "done" if paid_on else "plan"

    title = f"{counterparty} · {purpose}" if counterparty else purpose
    note_bits = [f"iiko накладная {number}".strip(), f"doc:{doc_id}"]
    if comment and comment != purpose:
        note_bits.insert(0, comment)

    return {
        "title": title[:512],
        "counterparty": (counterparty[:512] if counterparty else None),
        "amount": amount,
        "pay_date": pay_date,
        "account_name": org_name,
        "status": status,
        "source": "iiko",
        "external_id": f"iiko:invoice:{doc_id}",
        "note": " · ".join(note_bits)[:1000],
    }


def default_period(days_back: int = 30) -> tuple[date, date]:
    today = date.today()
    return today - timedelta(days=max(1, min(days_back, 366))), today
