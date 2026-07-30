from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .database import UPLOADS, Base, SessionLocal, engine, get_db
from .excel_import import parse_payments_xlsx
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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
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


def fmt_money(n: float) -> str:
    n = float(n or 0)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}".replace(".", ",") + " млн"
    if abs(n) >= 1_000:
        return f"{round(n / 1000):,}".replace(",", " ") + " тыс."
    return f"{round(n):,}".replace(",", " ") + " ₽"


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
    # if no exact today matches, show nearest upcoming unpaid in next 7 days as "к оплате сейчас"
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

    return {
        "accountsTotal": accounts_total,
        "accountsTotalLabel": fmt_money(accounts_total) if accounts else "нет данных",
        "payToday": pay_today_sum,
        "payTodayLabel": fmt_money(pay_today_sum),
        "afterPay": after,
        "afterPayLabel": fmt_money(after) if after is not None else "—",
        "hasAccounts": bool(accounts),
        "hasPayments": db.scalar(select(func.count()).select_from(Payment)) > 0,
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
        "requests": [
            {
                "id": r.id,
                "title": r.title,
                "amount": r.amount,
                "amountLabel": fmt_money(r.amount),
                "meta": r.meta,
                "status": r.status,
                "priority": r.priority,
            }
            for r in requests
        ],
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


@app.post("/api/import/excel")
async def import_excel(
    file: UploadFile = File(...),
    replace: bool = True,
    user: User = Depends(require_fin),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Нужен файл .xlsx")
    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 15 МБ")

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest = UPLOADS / f"{stamp}_{file.filename}"
    dest.write_bytes(content)

    try:
        payments = parse_payments_xlsx(content, source="excel")
    except Exception as e:
        raise HTTPException(400, f"Не разобрал Excel: {e}") from e

    if not payments:
        raise HTTPException(400, "В файле не нашёл ни одной строки с суммой")

    if replace:
        db.execute(delete(Payment).where(Payment.source == "excel"))

    db.add_all(payments)
    db.commit()

    # If no accounts yet — create from account column (balances still better via separate file)
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
        "message": f"Загружено платежей: {len(payments)}",
    }


@app.post("/api/accounts/set-balances")
async def set_balances(
    file: UploadFile = File(...),
    user: User = Depends(require_fin),
    db: Session = Depends(get_db),
):
    """Optional second Excel: columns Организация/Счёт + Остаток."""
    content = await file.read()
    wb_payments = parse_balance_sheet(content)
    db.execute(delete(Account))
    for row in wb_payments:
        db.add(Account(name=row["name"], kind=row.get("kind") or "р/с", balance=row["balance"], org=row.get("org")))
    db.commit()
    return {"ok": True, "accounts": len(wb_payments)}


def parse_balance_sheet(content: bytes) -> list[dict]:
    from openpyxl import load_workbook
    from io import BytesIO

    wb = load_workbook(BytesIO(content), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(c or "") for c in rows[0]]
    name_i = None
    bal_i = None
    for i, h in enumerate(headers):
        hn = h.strip().lower()
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
            bal = float(str(row[bal_i]).replace(" ", "").replace(",", "."))
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
