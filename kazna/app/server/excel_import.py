from __future__ import annotations

import re
from datetime import date, datetime
from io import BytesIO
from typing import Any

from openpyxl import load_workbook

from .models import Payment


HEADER_MAP = {
    "date": ("дата", "date", "срок", "день", "pay date", "дата оплаты", "дата платежа"),
    "amount": ("сумма", "amount", "к оплате", "платёж", "платеж", "sum"),
    "title": ("назначение", "описание", "статья", "title", "payment", "платёжка", "платежка", "комментарий"),
    "counterparty": ("контрагент", "поставщик", "получатель", "контрагент / поставщик", "vendor", "partner"),
    "account": ("счёт", "счет", "р/с", "расчётный счёт", "расчетный счет", "account", "организация"),
    "status": ("статус", "status", "состояние"),
    "note": ("примечание", "note", "коммент", "комментарий"),
}


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip().lower())


def _find_col(headers: list[str], keys: tuple[str, ...]) -> int | None:
    for i, h in enumerate(headers):
        hn = _norm(h)
        for key in keys:
            if key == hn or key in hn:
                return i
    return None


def _parse_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace("\u00a0", "").replace("₽", "").replace("руб.", "").replace("руб", "")
    s = s.replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in {".", "-", "-."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_payments_xlsx(content: bytes, source: str = "excel") -> list[Payment]:
    wb = load_workbook(BytesIO(content), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # find header row in first 15 rows
    header_idx = 0
    headers: list[str] = []
    for i, row in enumerate(rows[:15]):
        cells = [str(c or "") for c in row]
        joined = " ".join(_norm(c) for c in cells)
        if any(k in joined for k in ("сумма", "amount", "дата", "контрагент", "поставщик", "назначение")):
            header_idx = i
            headers = cells
            break
    if not headers:
        headers = [str(c or f"col{i}") for i, c in enumerate(rows[0])]
        header_idx = 0

    cols = {k: _find_col(headers, v) for k, v in HEADER_MAP.items()}
    if cols["amount"] is None:
        raise ValueError(
            "Не нашёл колонку суммы. Нужны заголовки вроде: Дата, Сумма, Контрагент, Назначение."
        )

    out: list[Payment] = []
    for row in rows[header_idx + 1 :]:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue
        amount = _parse_amount(row[cols["amount"]] if cols["amount"] is not None else None)
        if amount is None:
            continue

        title = ""
        if cols["title"] is not None and row[cols["title"]] is not None:
            title = str(row[cols["title"]]).strip()
        counterparty = None
        if cols["counterparty"] is not None and row[cols["counterparty"]] is not None:
            counterparty = str(row[cols["counterparty"]]).strip()
        if not title:
            title = counterparty or "Платёж"
        if counterparty and counterparty not in title:
            title = f"{counterparty} · {title}" if title != counterparty else counterparty

        account_name = None
        if cols["account"] is not None and row[cols["account"]] is not None:
            account_name = str(row[cols["account"]]).strip()

        status_raw = ""
        if cols["status"] is not None and row[cols["status"]] is not None:
            status_raw = _norm(row[cols["status"]])
        status = "plan"
        if any(x in status_raw for x in ("оплач", "paid", "done", "закрыт")):
            status = "done"
        elif any(x in status_raw for x in ("нов", "new")):
            status = "new"
        elif any(x in status_raw for x in ("ок", "соглас")):
            status = "ok"

        note = None
        if cols["note"] is not None and row[cols["note"]] is not None:
            note = str(row[cols["note"]]).strip()

        out.append(
            Payment(
                title=title[:512],
                counterparty=(counterparty[:512] if counterparty else None),
                amount=amount,
                pay_date=_parse_date(row[cols["date"]] if cols["date"] is not None else None),
                account_name=account_name,
                status=status,
                source=source,
                note=note,
            )
        )
    return out
