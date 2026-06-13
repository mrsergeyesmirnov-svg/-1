"""
Чек-листы открытия и закрытия смены.
Шаблон пунктов — на точке; отметки — по дням в ops_days.
"""
from __future__ import annotations

import secrets
from datetime import datetime
from html import escape
from typing import Any, Literal

import ops_day

CheckKind = Literal["opening", "closing"]

OPENING_KEY = "opening_checklist"
CLOSING_KEY = "closing_checklist"


def _items_key(kind: CheckKind) -> str:
    return OPENING_KEY if kind == "opening" else CLOSING_KEY


def _checks_key(kind: CheckKind) -> str:
    return "opening_checks" if kind == "opening" else "closing_checks"


def _opened_key(kind: CheckKind) -> str:
    return "shift_opened_at" if kind == "opening" else "shift_closed_at"


def get_template_items(rec: dict[str, Any], kind: CheckKind) -> list[dict[str, Any]]:
    raw = rec.get(_items_key(kind))
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        out.append(
            {
                "id": str(item["id"]),
                "text": str(item.get("text") or ""),
                "order": int(item.get("order", i)),
            }
        )
    return sorted(out, key=lambda x: x["order"])


def save_template_items(
    rec: dict[str, Any], kind: CheckKind, items: list[dict[str, Any]]
) -> None:
    rec[_items_key(kind)] = items


def add_template_item(rec: dict[str, Any], kind: CheckKind, text: str) -> tuple[bool, str | None]:
    text = text.strip()
    if len(text) < 2 or len(text) > 120:
        return False, "Текст пункта: от 2 до 120 символов."
    items = get_template_items(rec, kind)
    if len(items) >= 20:
        return False, "Не больше 20 пунктов в чек-листе."
    item_id = "c_" + secrets.token_hex(3)
    items.append({"id": item_id, "text": text, "order": len(items)})
    save_template_items(rec, kind, items)
    return True, None


def delete_template_item(
    rec: dict[str, Any], kind: CheckKind, item_id: str
) -> tuple[bool, str | None]:
    items = get_template_items(rec, kind)
    if not any(i["id"] == item_id for i in items):
        return False, "Пункт не найден."
    items = [i for i in items if i["id"] != item_id]
    for i, item in enumerate(items):
        item["order"] = i
    save_template_items(rec, kind, items)
    return True, None


def get_checks(rec: dict[str, Any], day: str, kind: CheckKind) -> dict[str, Any]:
    bucket = ops_day.get_day_bucket(rec, day)
    raw = bucket.get(_checks_key(kind))
    return raw if isinstance(raw, dict) else {}


def toggle_check(
    rec: dict[str, Any], day: str, kind: CheckKind, item_id: str, uid: int
) -> bool:
    """True если пункт отмечен после toggle."""
    bucket = ops_day.get_day_bucket(rec, day)
    checks = bucket.setdefault(_checks_key(kind), {})
    if not isinstance(checks, dict):
        checks = {}
        bucket[_checks_key(kind)] = checks
    if item_id in checks:
        checks.pop(item_id, None)
        bucket.pop(_opened_key(kind), None)
        return False
    checks[item_id] = {
        "by_uid": uid,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if all_checked(rec, day, kind):
        bucket[_opened_key(kind)] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
    return True


def all_checked(rec: dict[str, Any], day: str, kind: CheckKind) -> bool:
    items = get_template_items(rec, kind)
    if not items:
        return True
    checks = get_checks(rec, day, kind)
    return all(i["id"] in checks for i in items)


def shift_gate_complete(rec: dict[str, Any], day: str, kind: CheckKind) -> bool:
    """Открытие/закрытие смены по чек-листу (если пункты заданы)."""
    items = get_template_items(rec, kind)
    if not items:
        return True
    bucket = ops_day.get_day_bucket(rec, day)
    return bool(bucket.get(_opened_key(kind))) or all_checked(rec, day, kind)


def kind_title(kind: CheckKind) -> str:
    return "Открытие смены" if kind == "opening" else "Закрытие смены"


def format_config_message(
    rec: dict[str, Any], chat_title: str, kind: CheckKind
) -> str:
    items = get_template_items(rec, kind)
    title = kind_title(kind)
    lines = [
        f"📋 <b>{title}</b> · {escape(chat_title)}",
        f"Пунктов: <b>{len(items)}</b>",
        "",
        "<i>Менеджер отмечает галочки перед планом дня / закрытием.</i>",
        "",
    ]
    if items:
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. {escape(item['text'])}")
    else:
        lines.append("<i>Пунктов пока нет — добавьте ниже.</i>")
    return "\n".join(lines)


def config_keyboard(chat_id: int, kind: CheckKind):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    k = "o" if kind == "opening" else "c"
    rows: list[list] = [
        [
            InlineKeyboardButton(
                text="➕ Добавить пункт",
                callback_data=f"cl:add:{k}:{cid}"[:64],
            )
        ],
    ]
    items = []  # filled by caller if needed — use dynamic in bot
    return rows, k


def build_config_keyboard(rec: dict[str, Any], chat_id: int, kind: CheckKind):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    k = "o" if kind == "opening" else "c"
    rows: list[list] = []
    for item in get_template_items(rec, kind)[:15]:
        short = item["text"][:30] + ("…" if len(item["text"]) > 30 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 {short}",
                    callback_data=f"cl:del:{k}:{cid}:{item['id']}"[:64],
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➕ Добавить пункт", callback_data=f"cl:add:{k}:{cid}")]
    )
    other = "closing" if kind == "opening" else "opening"
    ok = "c" if other == "closing" else "o"
    rows.append(
        [
            InlineKeyboardButton(
                text="← Открытие" if kind == "closing" else "Закрытие →",
                callback_data=f"cl:cfg:{ok}:{cid}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"cl:menu:{cid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def checklist_menu_keyboard(chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="☀️ Открытие",
                    callback_data=f"cl:cfg:o:{cid}",
                ),
                InlineKeyboardButton(
                    text="🌙 Закрытие",
                    callback_data=f"cl:cfg:c:{cid}",
                ),
            ],
        ]
    )


def format_checklist_prompt(
    rec: dict[str, Any], chat_title: str, day: str, kind: CheckKind
) -> str:
    items = get_template_items(rec, kind)
    checks = get_checks(rec, day, kind)
    done = sum(1 for i in items if i["id"] in checks)
    title = kind_title(kind)
    lines = [
        f"<b>{title}</b> · {escape(chat_title)}",
        f"Отмечено: <b>{done}/{len(items)}</b>",
        "",
    ]
    for item in items:
        mark = "✅" if item["id"] in checks else "⬜"
        lines.append(f"{mark} {escape(item['text'])}")
    if done < len(items):
        lines.append("\n<i>Отметьте все пункты, чтобы продолжить.</i>")
    return "\n".join(lines)


def run_checklist_keyboard(rec: dict[str, Any], chat_id: int, day: str, kind: CheckKind):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    k = "o" if kind == "opening" else "c"
    checks = get_checks(rec, day, kind)
    rows: list[list] = []
    for item in get_template_items(rec, kind):
        mark = "✅" if item["id"] in checks else "⬜"
        short = item["text"][:28] + ("…" if len(item["text"]) > 28 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {short}",
                    callback_data=f"cl:chk:{k}:{cid}:{day}:{item['id']}"[:64],
                )
            ]
        )
    if shift_gate_complete(rec, day, kind):
        cont = "ops:mcont" if kind == "opening" else "ops:econt"
        rows.append(
            [
                InlineKeyboardButton(
                    text="✔️ Продолжить",
                    callback_data=f"{cont}:{cid}:{day}"[:64],
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
