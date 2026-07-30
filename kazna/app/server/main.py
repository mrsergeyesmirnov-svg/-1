from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .database import UPLOADS, Base, SessionLocal, engine, get_db
from .excel_import import inspect_payments_file, parse_payments_file
from .models import Account, Payment, RequestItem, User, UserAccount

ROOT = Path(__file__).resolve().parents[1]
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

ROLE_LABELS = {
    "fin_director": "Финансовый директор",
    "manager": "Управляющий",
    "accountant": "Бухгалтер",
}
ALLOWED_ROLES = set(ROLE_LABELS)

app = FastAPI(title="Казна")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("KAZNA_SECRET", secrets.token_hex(32)),
    session_cookie="kazna_session",
    same_site="lax",
    https_only=os.environ.get("KAZNA_HTTPS", "").lower() in {"1", "true", "yes"},
)


def _migrate_schema() -> None:
    """Add new columns on existing DBs (create_all alone won't alter)."""
    stmts = [
        "ALTER TABLE requests ADD COLUMN comment TEXT",
        "ALTER TABLE requests ADD COLUMN due_date DATE",
        "ALTER TABLE requests ADD COLUMN decided_by INTEGER",
        "ALTER TABLE requests ADD COLUMN payment_id INTEGER",
        "ALTER TABLE requests ADD COLUMN created_at TIMESTAMP",
        "ALTER TABLE requests ADD COLUMN updated_at TIMESTAMP",
    ]
    with engine.begin() as conn:
        for sql in stmts:
            try:
                conn.execute(text(sql))
            except Exception:
                pass


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_schema()
    db = SessionLocal()
    try:
        if db.scalar(select(func.count()).select_from(User)) == 0:
            db.add_all(
                [
                    User(
                        email="fin@kazna.local",
                        password_hash=pwd.hash("kazna2026"),
                        name="Анна Финдир",
                        role="fin_director",
                        role_label="Финансовый директор",
                    ),
                    User(
                        email="manager@kazna.local",
                        password_hash=pwd.hash("kazna2026"),
                        name="Игорь Управляющий",
                        role="manager",
                        role_label="Управляющий · Невский",
                        site="Невский",
                    ),
                ]
            )
            db.commit()
    finally:
        db.close()


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def current_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.get(User, uid)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if not user or not user.active:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


def require_fin(user: User = Depends(require_user)) -> User:
    if user.role != "fin_director":
        raise HTTPException(status_code=403, detail="Только финдир")
    return user


def require_office(user: User = Depends(require_user)) -> User:
    """Финдир или бухгалтер — кабинет сети, не точка управляющего."""
    if user.role not in {"fin_director", "accountant"}:
        raise HTTPException(status_code=403, detail="Нет доступа")
    return user


class LoginIn(BaseModel):
    email: str
    password: str


class RequestCreate(BaseModel):
    title: str = Field(min_length=2, max_length=512)
    amount: float = Field(gt=0)
    due_date: str | None = None
    comment: str | None = None
    meta: str | None = None
    priority: str = "normal"


class RequestPatch(BaseModel):
    status: str | None = None
    priority: str | None = None
    comment: str | None = None
    due_date: str | None = None
    to_calendar: bool = False


class PersonCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=128)
    role: str = "manager"
    site: str | None = None
    account_ids: list[int] = Field(default_factory=list)
    active: bool = True


class PersonPatch(BaseModel):
    name: str | None = None
    email: str | None = None
    password: str | None = None
    role: str | None = None
    site: str | None = None
    account_ids: list[int] | None = None
    active: bool | None = None


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: str = "юрлицо / р/с"
    org: str | None = None
    balance: float = 0


class AccountPatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    org: str | None = None
    balance: float | None = None


class PaymentPatch(BaseModel):
    title: str | None = None
    counterparty: str | None = None
    amount: float | None = None
    pay_date: str | None = None  # ISO or empty to clear
    clear_date: bool = False
    account_name: str | None = None
    status: str | None = None
    note: str | None = None


class PaymentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    counterparty: str | None = None
    amount: float = Field(gt=0)
    pay_date: str | None = None
    account_name: str | None = None
    status: str = "plan"
    note: str | None = None


def fmt_money(n: float) -> str:
    n = float(n or 0)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}".replace(".", ",") + " млн"
    if abs(n) >= 1_000:
        return f"{round(n / 1000):,}".replace(",", " ") + " тыс."
    return f"{round(n):,}".replace(",", " ") + " ₽"


def parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        for fmt in ("%d.%m.%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(s[:10], fmt).date()
            except ValueError:
                continue
    return None


def request_dict(r: RequestItem, creator: User | None = None) -> dict:
    return {
        "id": r.id,
        "title": r.title,
        "amount": r.amount,
        "amountLabel": fmt_money(r.amount),
        "meta": r.meta,
        "comment": r.comment,
        "status": r.status,
        "priority": r.priority,
        "site": r.site,
        "dueDate": r.due_date.isoformat() if r.due_date else None,
        "dueDateLabel": r.due_date.strftime("%d.%m.%Y") if r.due_date else "без срока",
        "createdBy": r.created_by,
        "creatorName": creator.name if creator else None,
        "paymentId": r.payment_id,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


def payment_dict(p: Payment) -> dict:
    # title often "контрагент · назначение" — split for UI
    purpose = p.title or ""
    counterparty = p.counterparty
    if counterparty and purpose.startswith(counterparty):
        rest = purpose[len(counterparty) :].lstrip(" ·")
        if rest:
            purpose = rest
    return {
        "id": p.id,
        "title": p.title,
        "counterparty": counterparty,
        "purpose": purpose if purpose != (counterparty or "") else None,
        "amount": p.amount,
        "amountLabel": fmt_money(p.amount),
        "date": p.pay_date.isoformat() if p.pay_date else None,
        "dateLabel": p.pay_date.strftime("%d.%m.%Y") if p.pay_date else "без даты",
        "status": p.status,
        "source": p.source,
        "account": p.account_name,
        "note": p.note,
    }


@app.get("/api/health")
def health():
    return {"ok": True, "service": "kazna"}


@app.post("/api/login")
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = str(payload.email).strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if not user or not pwd.verify(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Неверный логин или пароль")
    request.session["uid"] = user.id
    return {
        "ok": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
            "roleLabel": user.role_label,
            "site": user.site,
        },
    }


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    aids = _account_ids_for_user(db, user.id)
    accounts = []
    if aids:
        accounts = [
            {"id": a.id, "name": a.name, "org": a.org}
            for a in db.scalars(select(Account).where(Account.id.in_(aids)).order_by(Account.id)).all()
        ]
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "roleLabel": user.role_label,
        "site": user.site,
        "accountIds": aids,
        "accounts": accounts,
    }


@app.get("/api/overview")
def overview(user: User = Depends(require_office), db: Session = Depends(get_db)):
    today = date.today()
    accounts = db.scalars(select(Account).order_by(Account.id)).all()
    accounts_total = sum(a.balance for a in accounts) if accounts else 0.0

    pays_today = db.scalars(
        select(Payment).where(Payment.pay_date == today).where(Payment.status != "done")
    ).all()
    if not pays_today:
        pays_today = db.scalars(
            select(Payment)
            .where(Payment.status != "done")
            .where(Payment.pay_date.is_not(None))
            .where(Payment.pay_date >= today)
            .where(Payment.pay_date <= today + timedelta(days=7))
            .order_by(Payment.pay_date, Payment.id)
            .limit(20)
        ).all()

    pay_today_sum = sum(p.amount for p in pays_today)
    after = accounts_total - pay_today_sum if accounts else None

    recent_pays = db.scalars(
        select(Payment).order_by(Payment.pay_date.is_(None), Payment.pay_date, Payment.id).limit(30)
    ).all()
    requests = db.scalars(select(RequestItem).order_by(RequestItem.id.desc()).limit(20)).all()
    pending = db.scalar(
        select(func.count())
        .select_from(RequestItem)
        .where(RequestItem.status.in_(("new", "reviewing")))
    )

    return {
        "accountsTotal": accounts_total,
        "accountsTotalLabel": fmt_money(accounts_total) if accounts else "нет данных",
        "payToday": pay_today_sum,
        "payTodayLabel": fmt_money(pay_today_sum),
        "afterPay": after,
        "afterPayLabel": fmt_money(after) if after is not None else "—",
        "hasAccounts": bool(accounts),
        "hasPayments": db.scalar(select(func.count()).select_from(Payment)) > 0,
        "pendingRequests": int(pending or 0),
        "accounts": [
            {
                "id": a.id,
                "name": a.name,
                "kind": a.kind,
                "balance": a.balance,
                "balanceLabel": fmt_money(a.balance),
                "org": a.org,
            }
            for a in accounts
        ],
        "payments": [payment_dict(p) for p in recent_pays],
        "requests": [request_dict(r) for r in requests],
    }


@app.get("/api/payments")
def list_payments(user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Payment).order_by(Payment.pay_date.is_(None), Payment.pay_date, Payment.id)).all()
    return [payment_dict(p) for p in rows]


@app.post("/api/payments")
def create_payment(payload: PaymentCreate, user: User = Depends(require_office), db: Session = Depends(get_db)):
    status = payload.status if payload.status in {"new", "plan", "ok", "done"} else "plan"
    title = payload.title.strip()
    counterparty = (payload.counterparty or "").strip() or None
    if counterparty and counterparty not in title:
        title = f"{counterparty} · {title}"
    pay = Payment(
        title=title[:512],
        counterparty=counterparty,
        amount=float(payload.amount),
        pay_date=parse_iso_date(payload.pay_date),
        account_name=(payload.account_name or "").strip() or None,
        status=status,
        source="manual",
        note=(payload.note or "").strip() or None,
    )
    db.add(pay)
    db.commit()
    db.refresh(pay)
    return payment_dict(pay)


@app.patch("/api/payments/{payment_id}")
def patch_payment(
    payment_id: int,
    payload: PaymentPatch,
    user: User = Depends(require_office),
    db: Session = Depends(get_db),
):
    pay = db.get(Payment, payment_id)
    if not pay:
        raise HTTPException(404, "Платёж не найден")

    if payload.title is not None:
        pay.title = payload.title.strip()[:512] or pay.title
    if payload.counterparty is not None:
        pay.counterparty = payload.counterparty.strip() or None
    if payload.amount is not None:
        if payload.amount <= 0:
            raise HTTPException(400, "Сумма должна быть больше 0")
        pay.amount = float(payload.amount)
    if payload.clear_date:
        pay.pay_date = None
    elif payload.pay_date is not None:
        pay.pay_date = parse_iso_date(payload.pay_date) if payload.pay_date.strip() else None
    if payload.account_name is not None:
        pay.account_name = payload.account_name.strip() or None
    if payload.status is not None:
        if payload.status not in {"new", "plan", "ok", "done"}:
            raise HTTPException(400, "Неверный статус")
        pay.status = payload.status
    if payload.note is not None:
        pay.note = payload.note.strip() or None

    db.commit()
    db.refresh(pay)
    return payment_dict(pay)


def _import_payments(
    content: bytes,
    filename: str,
    source: str,
    replace: bool,
    db: Session,
) -> dict:
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (filename or "upload.xlsx"))
    dest = UPLOADS / f"{stamp}_{safe}"
    dest.write_bytes(content)

    try:
        payments = parse_payments_file(content, filename=filename, source=source)
    except Exception as e:
        raise HTTPException(400, f"Не разобрал файл: {e}") from e

    if not payments:
        raise HTTPException(400, "В файле не нашёл ни одной строки с суммой")

    if replace:
        db.execute(delete(Payment).where(Payment.source == source))

    db.add_all(payments)
    db.commit()

    # Accounts / юрлица from column «организация / р/с»
    org_names = sorted({p.account_name for p in payments if p.account_name})
    if org_names:
        existing = {a.name for a in db.scalars(select(Account)).all()}
        for name in org_names:
            if name not in existing:
                db.add(Account(name=name, kind="юрлицо / р/с", balance=0, org=name))
        db.commit()
    elif db.scalar(select(func.count()).select_from(Account)) == 0:
        db.add(Account(name="Не указано", kind="юрлицо / р/с", balance=0, org=None))
        db.commit()

    no_date = sum(1 for p in payments if not p.pay_date)
    no_who = sum(1 for p in payments if not p.counterparty)
    no_org = sum(1 for p in payments if not p.account_name)
    warnings = []
    if no_who > len(payments) // 2:
        warnings.append("у многих строк не найден контрагент (кому)")
    if no_date > len(payments) // 2:
        warnings.append("у многих строк нет даты/срока")
    if no_org > len(payments) // 2:
        warnings.append("не нашли колонку юрлица/р/с — укажите «Организация» или «Плательщик»")

    sample = [payment_dict(p) for p in payments[:3]]
    msg = f"Загружено платежей: {len(payments)} ({source})"
    if warnings:
        msg += ". Внимание: " + "; ".join(warnings) + ". Нажмите «Показать заголовки»."

    return {
        "ok": True,
        "imported": len(payments),
        "file": dest.name,
        "source": source,
        "message": msg,
        "warnings": warnings,
        "sample": sample,
    }


@app.post("/api/import/excel")
async def import_excel(
    file: UploadFile = File(...),
    replace: bool = True,
    user: User = Depends(require_fin),
    db: Session = Depends(get_db),
):
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xlsm", ".csv")):
        raise HTTPException(400, "Нужен файл .xlsx или .csv")
    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 15 МБ")
    return _import_payments(content, file.filename or "payments.xlsx", "excel", replace, db)


@app.post("/api/import/iiko")
async def import_iiko(
    file: UploadFile = File(...),
    replace: bool = True,
    user: User = Depends(require_fin),
    db: Session = Depends(get_db),
):
    """Import iiko export (xlsx/csv). Live API credentials come next."""
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xlsm", ".csv")):
        raise HTTPException(400, "Нужен выгрузка iiko: .xlsx или .csv")
    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 15 МБ")
    return _import_payments(content, file.filename or "iiko.xlsx", "iiko", replace, db)


@app.post("/api/import/inspect")
async def import_inspect(
    file: UploadFile = File(...),
    user: User = Depends(require_fin),
):
    content = await file.read()
    try:
        info = inspect_payments_file(content, filename=file.filename or "")
    except Exception as e:
        raise HTTPException(400, f"Не открыл файл: {e}") from e
    return {"ok": True, "filename": file.filename, **info}


class IikoConfigIn(BaseModel):
    api_login: str | None = None
    org_id: str | None = None
    note: str | None = None


@app.post("/api/import/iiko/config")
def iiko_config_stub(payload: IikoConfigIn, user: User = Depends(require_fin)):
    """Store intent for live iiko API — credentials kept for next iteration."""
    return {
        "ok": True,
        "message": "Конфиг принят. Живой API iiko подключим следующим шагом; сейчас работает выгрузка файлом.",
        "saved": {
            "apiLogin": bool(payload.api_login),
            "orgId": payload.org_id,
            "note": payload.note,
        },
    }


@app.post("/api/accounts/set-balances")
async def set_balances(
    file: UploadFile = File(...),
    user: User = Depends(require_fin),
    db: Session = Depends(get_db),
):
    content = await file.read()
    try:
        wb_payments = parse_balance_sheet(content)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Не разобрал остатки: {e}") from e
    db.execute(delete(Account))
    for row in wb_payments:
        db.add(Account(name=row["name"], kind=row.get("kind") or "р/с", balance=row["balance"], org=row.get("org")))
    db.commit()
    return {"ok": True, "accounts": len(wb_payments)}


def parse_balance_sheet(content: bytes) -> list[dict]:
    from io import BytesIO

    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(content), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(c or "") for c in rows[0]]
    name_i = None
    bal_i = None
    for i, h in enumerate(headers):
        hn = h.strip().lower().replace("ё", "е")
        if name_i is None and any(x in hn for x in ("счёт", "счет", "организ", "account", "название")):
            name_i = i
        if bal_i is None and any(x in hn for x in ("остаток", "баланс", "balance", "сумма")):
            bal_i = i
    if name_i is None or bal_i is None:
        raise HTTPException(400, "Нужны колонки: Счёт/Организация и Остаток")
    out = []
    for row in rows[1:]:
        if not row or row[name_i] is None:
            continue
        try:
            bal = float(str(row[bal_i]).replace(" ", "").replace(",", ".").replace("\u00a0", ""))
        except Exception:
            continue
        out.append({"name": str(row[name_i]).strip(), "balance": bal, "kind": "р/с", "org": str(row[name_i]).strip()})
    if not out:
        raise HTTPException(400, "Пустой файл остатков")
    return out


def _account_ids_for_user(db: Session, user_id: int) -> list[int]:
    return list(
        db.scalars(select(UserAccount.account_id).where(UserAccount.user_id == user_id)).all()
    )


def _set_user_accounts(db: Session, user_id: int, account_ids: list[int]) -> None:
    db.execute(delete(UserAccount).where(UserAccount.user_id == user_id))
    if not account_ids:
        return
    valid = set(db.scalars(select(Account.id).where(Account.id.in_(account_ids))).all())
    for aid in account_ids:
        if aid in valid:
            db.add(UserAccount(user_id=user_id, account_id=aid))


def _role_label(role: str, site: str | None = None) -> str:
    base = ROLE_LABELS.get(role, role)
    if role == "manager" and site:
        return f"{base} · {site}"
    return base


def person_dict(u: User, db: Session) -> dict:
    aids = _account_ids_for_user(db, u.id)
    accounts = []
    if aids:
        accounts = [
            {"id": a.id, "name": a.name, "org": a.org}
            for a in db.scalars(select(Account).where(Account.id.in_(aids)).order_by(Account.id)).all()
        ]
    return {
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "role": u.role,
        "roleLabel": u.role_label or _role_label(u.role, u.site),
        "site": u.site,
        "siteLabel": u.site or ("все точки" if u.role == "fin_director" else "не назначена"),
        "active": u.active,
        "accountIds": aids,
        "accounts": accounts,
    }


def account_dict(a: Account) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "kind": a.kind,
        "balance": a.balance,
        "balanceLabel": fmt_money(a.balance),
        "org": a.org,
    }


@app.get("/api/roles")
def list_roles(user: User = Depends(require_fin)):
    return [{"id": k, "label": v} for k, v in ROLE_LABELS.items()]


@app.get("/api/accounts")
def list_accounts(user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Account).order_by(Account.id)).all()
    if user.role == "fin_director":
        return [account_dict(a) for a in rows]
    allowed = set(_account_ids_for_user(db, user.id))
    if not allowed:
        return []
    return [account_dict(a) for a in rows if a.id in allowed]


@app.post("/api/accounts")
def create_account(payload: AccountCreate, user: User = Depends(require_fin), db: Session = Depends(get_db)):
    acc = Account(
        name=payload.name.strip(),
        kind=(payload.kind or "юрлицо / р/с").strip(),
        org=(payload.org or payload.name).strip() if payload.org or payload.name else None,
        balance=float(payload.balance or 0),
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return account_dict(acc)


@app.patch("/api/accounts/{account_id}")
def patch_account(
    account_id: int,
    payload: AccountPatch,
    user: User = Depends(require_fin),
    db: Session = Depends(get_db),
):
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "Счёт не найден")
    if payload.name is not None:
        acc.name = payload.name.strip()
    if payload.kind is not None:
        acc.kind = payload.kind.strip()
    if payload.org is not None:
        acc.org = payload.org.strip() or None
    if payload.balance is not None:
        acc.balance = float(payload.balance)
    db.commit()
    db.refresh(acc)
    return account_dict(acc)


@app.get("/api/people")
def people(user: User = Depends(require_fin), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).order_by(User.id)).all()
    return [person_dict(u, db) for u in rows]


@app.post("/api/people")
def create_person(payload: PersonCreate, user: User = Depends(require_fin), db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(400, "Такой email уже есть")
    role = payload.role if payload.role in ALLOWED_ROLES else "manager"
    site = (payload.site or "").strip() or None
    person = User(
        email=email,
        password_hash=pwd.hash(payload.password),
        name=payload.name.strip(),
        role=role,
        role_label=_role_label(role, site),
        site=site,
        active=bool(payload.active),
    )
    db.add(person)
    db.flush()
    _set_user_accounts(db, person.id, payload.account_ids)
    db.commit()
    db.refresh(person)
    return person_dict(person, db)


@app.patch("/api/people/{person_id}")
def patch_person(
    person_id: int,
    payload: PersonPatch,
    user: User = Depends(require_fin),
    db: Session = Depends(get_db),
):
    person = db.get(User, person_id)
    if not person:
        raise HTTPException(404, "Пользователь не найден")

    if payload.email is not None:
        email = payload.email.strip().lower()
        other = db.scalar(select(User).where(User.email == email, User.id != person.id))
        if other:
            raise HTTPException(400, "Такой email уже есть")
        person.email = email
    if payload.name is not None:
        person.name = payload.name.strip()
    if payload.password:
        if len(payload.password) < 6:
            raise HTTPException(400, "Пароль минимум 6 символов")
        person.password_hash = pwd.hash(payload.password)
    if payload.role is not None:
        if payload.role not in ALLOWED_ROLES:
            raise HTTPException(400, "Неизвестная роль")
        # Don't lock yourself out of being the last fin director
        if person.id == user.id and payload.role != "fin_director":
            others = db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role == "fin_director")
                .where(User.active.is_(True))
                .where(User.id != person.id)
            )
            if not others:
                raise HTTPException(400, "Нужен хотя бы один активный финдир")
        person.role = payload.role
    if payload.site is not None:
        person.site = payload.site.strip() or None
    if payload.active is not None:
        if person.id == user.id and not payload.active:
            raise HTTPException(400, "Нельзя выключить самого себя")
        person.active = bool(payload.active)
    person.role_label = _role_label(person.role, person.site)
    if payload.account_ids is not None:
        _set_user_accounts(db, person.id, payload.account_ids)
    db.commit()
    db.refresh(person)
    return person_dict(person, db)


# ——— Requests: manager ↔ fin director ———


@app.get("/api/requests")
def list_requests(user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = select(RequestItem).order_by(RequestItem.id.desc())
    if user.role == "manager":
        q = q.where(RequestItem.created_by == user.id)
    rows = db.scalars(q).all()
    creators = {u.id: u for u in db.scalars(select(User)).all()}
    return [request_dict(r, creators.get(r.created_by)) for r in rows]


@app.post("/api/requests")
def create_request(payload: RequestCreate, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if user.role not in {"manager", "fin_director"}:
        raise HTTPException(403, "Нет доступа")
    priority = payload.priority if payload.priority in {"urgent", "high", "normal", "low"} else "normal"
    item = RequestItem(
        title=payload.title.strip()[:512],
        amount=float(payload.amount),
        meta=(payload.meta or "").strip()[:512] or None,
        comment=(payload.comment or "").strip() or None,
        status="new",
        priority=priority,
        site=user.site,
        due_date=parse_iso_date(payload.due_date),
        created_by=user.id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return request_dict(item, user)


@app.patch("/api/requests/{request_id}")
def patch_request(
    request_id: int,
    payload: RequestPatch,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    item = db.get(RequestItem, request_id)
    if not item:
        raise HTTPException(404, "Заявка не найдена")

    is_fin = user.role == "fin_director"
    is_owner = item.created_by == user.id

    if not is_fin and not is_owner:
        raise HTTPException(403, "Нет доступа")

    if payload.priority and is_fin:
        if payload.priority not in {"urgent", "high", "normal", "low"}:
            raise HTTPException(400, "Неверный приоритет")
        item.priority = payload.priority

    if payload.comment is not None and (is_fin or is_owner):
        item.comment = payload.comment.strip() or None

    if payload.due_date is not None and (is_fin or (is_owner and item.status == "new")):
        item.due_date = parse_iso_date(payload.due_date)

    if payload.status:
        if not is_fin and not (is_owner and payload.status == "new" and item.status == "new"):
            # manager can only cancel own new requests
            if is_owner and payload.status == "rejected" and item.status in {"new", "reviewing"}:
                item.status = "rejected"
            elif not is_fin:
                raise HTTPException(403, "Статус меняет только финдир")
        if is_fin:
            allowed = {"new", "reviewing", "approved", "rejected", "scheduled", "paid"}
            if payload.status not in allowed:
                raise HTTPException(400, "Неверный статус")
            item.status = payload.status
            item.decided_by = user.id

    # Approve / to calendar → create Payment linked to request
    if is_fin and (payload.to_calendar or payload.status in {"approved", "scheduled"}):
        if item.payment_id is None:
            pay = Payment(
                title=f"{item.site + ' · ' if item.site else ''}{item.title}",
                counterparty=item.site,
                amount=item.amount,
                pay_date=item.due_date or date.today(),
                account_name=None,
                status="plan",
                source="request",
                note=item.comment,
            )
            db.add(pay)
            db.flush()
            item.payment_id = pay.id
        if item.status not in {"paid", "rejected"}:
            item.status = "scheduled" if payload.to_calendar or payload.status == "scheduled" else "approved"
        item.decided_by = user.id

    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    creator = db.get(User, item.created_by) if item.created_by else None
    return request_dict(item, creator)


# Static frontend
app.mount("/css", StaticFiles(directory=ROOT / "css"), name="css")
app.mount("/js", StaticFiles(directory=ROOT / "js"), name="js")


@app.get("/")
def root():
    return FileResponse(ROOT / "index.html")


@app.get("/{page_name}.html")
def html_page(page_name: str):
    path = ROOT / f"{page_name}.html"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)
