from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(64))  # fin_director | manager | accountant
    role_label: Mapped[str] = mapped_column(String(255), default="")
    site: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(128), default="р/с")
    balance: Mapped[float] = mapped_column(Float, default=0)
    org: Mapped[str | None] = mapped_column(String(255), nullable=True)


class UserAccount(Base):
    """Which settlement accounts / legal entities a user may work with."""

    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    counterparty: Mapped[str | None] = mapped_column(String(512), nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    pay_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="plan")  # plan|ok|done|new
    source: Mapped[str] = mapped_column(String(64), default="excel")  # excel|iiko|manual|request
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class IikoSettings(Base):
    __tablename__ = "iiko_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_login: Mapped[str] = mapped_column(String(512), default="")
    organization_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    organization_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    days_back: Mapped[int] = mapped_column(Integer, default=30)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_result: Mapped[str | None] = mapped_column(Text, nullable=True)


class RequestItem(Base):
    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    amount: Mapped[float] = mapped_column(Float)
    meta: Mapped[str | None] = mapped_column(String(512), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="new")
    # new | reviewing | approved | rejected | scheduled | paid
    priority: Mapped[str] = mapped_column(String(64), default="normal")
    # urgent | high | normal | low
    site: Mapped[str | None] = mapped_column(String(255), nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
