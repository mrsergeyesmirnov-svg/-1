from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from io import BytesIO
from typing import Any

from openpyxl import load_workbook

from .models import Payment

AMOUNT_KEYS = (
    "сумма к оплате",
    "сумма платежа",
    "сумма оплаты",
    "сумма документа",
    "сумма с ндс",
    "сумма без ндс",
    "сумма руб",
    "сумма, руб",
    "сумма в руб",
    "сумма",
    "к оплате",
    "итого к оплате",
    "итого",
    "всего",
    "amount",
    "summa",
    "sum",
    "value",
    "оплата",
    "платёж",
    "платеж",
    "стоимость",
    "оборот",
    "расход",
    "списание",
    "руб",
    "rur",
    "rub",
)

# Outgoing money columns in bank / 1C statements
DEBIT_KEYS = ("дебет", "дебит", "списано", "расход", "outcome", "debit", "out")
CREDIT_KEYS = ("кредит", "зачислено", "приход", "income", "credit", "in")

DATE_KEYS = (
    "дата оплаты",
    "дата платежа",
    "дата документа",
    "дата операции",
    "дата проводки",
    "дата",
    "срок оплаты",
    "срок",
    "день",
    "date",
    "pay date",
    "due",
    "operation date",
)

TITLE_KEYS = (
    "назначение платежа",
    "назначение",
    "описание",
    "статья",
    "основание",
    "комментарий",
    "title",
    "payment purpose",
    "purpose",
    "платёжка",
    "платежка",
    "содержание",
    "операция",
)

COUNTERPARTY_KEYS = (
    "контрагент",
    "поставщик",
    "получатель",
    "плательщик",
    "vendor",
    "partner",
    "организация контрагент",
    "корреспондент",
    "имя",
)

ACCOUNT_KEYS = (
    "расчётный счёт",
    "расчетный счет",
    "р/с",
    "счёт",
    "счет",
    "account",
    "организация",
    "юрлицо",
    "юр. лицо",
    "компания",
    "точка",
)

STATUS_KEYS = ("статус", "status", "состояние", "оплачено", "признак")


def _norm(v: Any) -> str:
    s = str(v or "").replace("\n", " ").replace("\r", " ")
    s = s.replace("ё", "е")
    return re.sub(r"\s+", " ", s).strip().lower()


def _find_col(headers: list[str], keys: tuple[str, ...]) -> int | None:
    norms = [_norm(h) for h in headers]
    for key in sorted(keys, key=len, reverse=True):
        kn = _norm(key)
        for i, hn in enumerate(norms):
            if not hn:
                continue
            if hn == kn or kn in hn or hn in kn:
                return i
    return None


def _parse_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):
        try:
            from openpyxl.utils.datetime import from_excel

            return from_excel(v).date()
        except Exception:
            return None
    s = str(v).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y", "%Y.%m.%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    # 15.07.2026 12:30
    m = re.match(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def _parse_amount(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    s = s.replace("\u00a0", " ").replace(" ", "")
    s = s.replace("₽", "").replace("руб.", "").replace("руб", "").replace("RUB", "").replace("rub", "")
    if s.count(",") == 1 and s.count(".") >= 1:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(",") == 1 and s.count(".") == 0:
        s = s.replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in {".", "-", "-."}:
        return None
    try:
        return abs(float(s))
    except ValueError:
        return None


def _score_amount_col(rows: list[tuple], col: int, start: int) -> int:
    ok = 0
    for row in rows[start : start + 80]:
        if col >= len(row):
            continue
        if _parse_amount(row[col]) is not None:
            ok += 1
    return ok


def _detect_header(rows: list[tuple]) -> tuple[int, list[str]]:
    best = (0, [str(c or f"col{i}") for i, c in enumerate(rows[0] or ())])
    best_score = -1
    for i, row in enumerate(rows[:30]):
        if not row:
            continue
        cells = [str(c or "") for c in row]
        norms = [_norm(c) for c in cells]
        joined = " ".join(norms)
        score = 0
        for token in (
            "сумма",
            "amount",
            "дата",
            "date",
            "контрагент",
            "поставщик",
            "назначение",
            "оплат",
            "счет",
            "счёт",
            "дебет",
            "кредит",
            "расход",
            "приход",
        ):
            if token in joined:
                score += 3
        # Prefer rows that look like headers (short text cells, few numbers)
        numericish = sum(1 for c in cells if _parse_amount(c) is not None and not re.search(r"[a-zа-я]", _norm(c)))
        if numericish >= max(2, len(cells) // 2):
            score -= 4
        if score:
            for j, _ in enumerate(cells):
                score += min(4, _score_amount_col(rows, j, i + 1) // 4)
        if score > best_score:
            best_score = score
            best = (i, cells)
    return best


def _resolve_amount_cols(
    headers: list[str], rows: list[tuple], header_idx: int
) -> tuple[int | None, int | None, int | None]:
    """Return (amount_col, debit_col, credit_col)."""
    amount = _find_col(headers, AMOUNT_KEYS)
    debit = _find_col(headers, DEBIT_KEYS)
    credit = _find_col(headers, CREDIT_KEYS)

    # Avoid treating "сумма" as both when debit/credit exist
    if amount is not None and debit is not None and amount == debit:
        amount = None
    if amount is not None and credit is not None and amount == credit:
        amount = None

    if amount is None and debit is None and credit is None:
        width = max((len(r) for r in rows[header_idx + 1 : header_idx + 40]), default=0) or len(headers)
        scored = [(_score_amount_col(rows, c, header_idx + 1), c) for c in range(width)]
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 3:
            amount = scored[0][1]

    return amount, debit, credit


def _rows_from_xlsx(content: bytes) -> list[tuple[str, list[tuple]]]:
    wb = load_workbook(BytesIO(content), data_only=True)
    out = []
    for name in wb.sheetnames:
        ws = wb[name]
        rows = list(ws.iter_rows(values_only=True))
        if rows:
            out.append((name, rows))
    return out


def _rows_from_csv(content: bytes) -> list[tuple[str, list[tuple]]]:
    text = content.decode("utf-8-sig", errors="replace")
    # iiko / 1C often use windows-1251
    if text.count("�") > 20:
        text = content.decode("cp1251", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=";,\t")
    except Exception:
        dialect = csv.excel
        dialect.delimiter = ";" if text.count(";") > text.count(",") else ","
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [tuple(r) for r in reader]
    return [("csv", rows)] if rows else []


def inspect_payments_file(content: bytes, filename: str = "") -> dict:
    """Debug helper: which headers/columns we detected."""
    name = (filename or "").lower()
    if name.endswith(".csv") or (
        not name.endswith((".xlsx", ".xlsm")) and (b"," in content[:200] or b";" in content[:200])
    ):
        sheets = _rows_from_csv(content)
    else:
        sheets = _rows_from_xlsx(content)

    result = {"sheets": []}
    for sheet_name, rows in sheets:
        if not rows:
            continue
        header_idx, headers = _detect_header(rows)
        amount, debit, credit = _resolve_amount_cols(headers, rows, header_idx)
        result["sheets"].append(
            {
                "name": sheet_name,
                "headerRow": header_idx + 1,
                "headers": [h for h in headers if str(h).strip()],
                "amountCol": headers[amount] if amount is not None and amount < len(headers) else None,
                "debitCol": headers[debit] if debit is not None and debit < len(headers) else None,
                "creditCol": headers[credit] if credit is not None and credit < len(headers) else None,
                "dateCol": (headers[_find_col(headers, DATE_KEYS)] if _find_col(headers, DATE_KEYS) is not None else None),
            }
        )
    return result


def parse_payments_file(content: bytes, filename: str = "", source: str = "excel") -> list[Payment]:
    name = (filename or "").lower()
    if name.endswith(".csv") or (
        not name.endswith((".xlsx", ".xlsm")) and (b"," in content[:200] or b";" in content[:200])
    ):
        sheets = _rows_from_csv(content)
    else:
        sheets = _rows_from_xlsx(content)

    errors = []
    for sheet_name, rows in sheets:
        try:
            payments = _parse_sheet(rows, source=source)
            if payments:
                return payments
            errors.append(f"лист «{sheet_name}»: строк с суммой не найдено")
        except ValueError as e:
            errors.append(f"лист «{sheet_name}»: {e}")

    raise ValueError(" | ".join(errors) if errors else "Файл пустой")


def parse_payments_xlsx(content: bytes, source: str = "excel") -> list[Payment]:
    return parse_payments_file(content, filename="file.xlsx", source=source)


def _cell(row: list, idx: int | None) -> Any:
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _parse_sheet(rows: list[tuple], source: str) -> list[Payment]:
    if not rows:
        return []

    header_idx, headers = _detect_header(rows)
    max_w = max(len(headers), max((len(r) for r in rows[:50]), default=0))
    while len(headers) < max_w:
        headers.append(f"col{len(headers)}")

    amount_col, debit_col, credit_col = _resolve_amount_cols(headers, rows, header_idx)
    cols = {
        "date": _find_col(headers, DATE_KEYS),
        "title": _find_col(headers, TITLE_KEYS),
        "counterparty": _find_col(headers, COUNTERPARTY_KEYS),
        "account": _find_col(headers, ACCOUNT_KEYS),
        "status": _find_col(headers, STATUS_KEYS),
        "note": _find_col(headers, ("примечание", "note", "коммент")),
    }

    if amount_col is None and debit_col is None and credit_col is None:
        shown = ", ".join(h for h in headers if str(h).strip())[:160]
        raise ValueError(
            f"Не нашёл колонку суммы. Заголовки в файле: [{shown}]. "
            f"Переименуй колонку в «Сумма» / «Сумма к оплате» или используй «Дебет»/«Кредит»."
        )

    out: list[Payment] = []
    for row in rows[header_idx + 1 :]:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue
        row = list(row)
        while len(row) < max_w:
            row.append(None)

        amount = None
        if amount_col is not None:
            amount = _parse_amount(row[amount_col])
        if amount is None and debit_col is not None:
            amount = _parse_amount(row[debit_col])
        if amount is None and credit_col is not None:
            # For payments calendar we usually want outgoing; keep credit as optional inflow marked note
            amount = _parse_amount(row[credit_col])
        if amount is None or amount == 0:
            continue

        # Skip obvious total rows
        title_probe = " ".join(
            str(_cell(row, cols[k]) or "") for k in ("title", "counterparty") if cols[k] is not None
        )
        if _norm(title_probe) in {"итого", "всего", "total", "сумма"}:
            continue

        title = ""
        if cols["title"] is not None and row[cols["title"]] is not None:
            title = str(row[cols["title"]]).strip()
        counterparty = None
        if cols["counterparty"] is not None and row[cols["counterparty"]] is not None:
            counterparty = str(row[cols["counterparty"]]).strip() or None
        if not title:
            title = counterparty or "Платёж"
        if counterparty and counterparty not in title:
            title = f"{counterparty} · {title}" if title != counterparty else counterparty

        account_name = None
        if cols["account"] is not None and row[cols["account"]] is not None:
            account_name = str(row[cols["account"]]).strip() or None

        status_raw = ""
        if cols["status"] is not None and row[cols["status"]] is not None:
            status_raw = _norm(row[cols["status"]])
        status = "plan"
        if any(x in status_raw for x in ("оплач", "paid", "done", "закрыт", "исполн", "да", "true")):
            status = "done"
        elif any(x in status_raw for x in ("нов", "new", "к оплате")):
            status = "new" if "нов" in status_raw or "new" in status_raw else "plan"
        elif any(x in status_raw for x in ("соглас", "утверж", "ок")):
            status = "ok"

        note = None
        if cols["note"] is not None and row[cols["note"]] is not None:
            note = str(row[cols["note"]]).strip() or None

        pay_date = None
        if cols["date"] is not None:
            pay_date = _parse_date(row[cols["date"]])

        out.append(
            Payment(
                title=title[:512],
                counterparty=(counterparty[:512] if counterparty else None),
                amount=amount,
                pay_date=pay_date,
                account_name=account_name,
                status=status,
                source=source,
                note=note,
            )
        )
    return out
