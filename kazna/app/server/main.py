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
from .models import Account, Payment, RequestItem, User

ROOT = Path(__file__).resolve().parents[1]
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

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
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "roleLabel": user.role_label,
        "site": user.site,
    }


@app.get("/api/overview")
def overview(user: User = Depends(require_fin), db: Session = Depends(get_db)):
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
        "payments": [
            {
                "id": p.id,
                "title": p.title,
                "amount": p.amount,
                "amountLabel": fmt_money(p.amount),
                "date": p.pay_date.isoformat() if p.pay_date else None,
                "dateLabel": p.pay_date.strftime("%d.%m.%Y") if p.pay_date else "без даты",
                "status": p.status,
                "source": p.source,
                "account": p.account_name,
                "note": p.note,
            }
            for p in recent_pays
        ],
        "requests": [request_dict(r) for r in requests],
    }


@app.get("/api/payments")
def list_payments(user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Payment).order_by(Payment.pay_date.is_(None), Payment.pay_date, Payment.id)).all()
    return [
        {
            "id": p.id,
            "title": p.title,
            "amount": p.amount,
            "amountLabel": fmt_money(p.amount),
            "date": p.pay_date.isoformat() if p.pay_date else None,
            "dateLabel": p.pay_date.strftime("%d.%m.%Y") if p.pay_date else "без даты",
            "status": p.status,
            "source": p.source,
            "account": p.account_name,
        }
        for p in rows
    ]


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

    if db.scalar(select(func.count()).select_from(Account)) == 0:
        names = sorted({p.account_name for p in payments if p.account_name})
        for name in names or ["Основной р/с"]:
            due = sum(
                p.amount
                for p in payments
                if (p.account_name or "Основной р/с") == name and p.status != "done"
            )
            db.add(Account(name=name, kind="р/с", balance=max(due * 1.15, due), org=name))
        db.commit()

    return {
        "ok": True,
        "imported": len(payments),
        "file": dest.name,
        "source": source,
        "message": f"Загружено платежей: {len(payments)} ({source})",
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


@app.get("/api/people")
def people(user: User = Depends(require_fin), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).order_by(User.id)).all()
    return [
        {
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "roleLabel": u.role_label,
            "site": u.site or "все точки",
            "active": u.active,
        }
        for u in rows
    ]


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
