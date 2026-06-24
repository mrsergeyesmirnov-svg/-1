"""
Опрос шефа в личке: оценка смены кухни (отдельно от опроса зала).
"""
from __future__ import annotations

import re
from html import escape
from typing import Any

MAX_ENABLED = 5
MAX_CUSTOM = 6

DEPARTMENT_KITCHEN = "kitchen"
DEPARTMENT_FLOOR = "floor"
DEPARTMENT_ALL = "all"

CHEF_SURVEY_LABELS: dict[str, str] = {
    "pickup_slow": "Долго забирали еду",
    "dishes_slow": "Долго несли посуду",
    "communication": "Сложности в коммуникации",
    "comment": "Свой комментарий",
}

DEFAULT_CHEF_BUTTONS: list[dict[str, Any]] = [
    {
        "code": "pickup_slow",
        "label": "🍽 Долго забирали еду",
        "enabled": True,
        "builtin": True,
    },
    {
        "code": "dishes_slow",
        "label": "🧺 Долго несли посуду",
        "enabled": True,
        "builtin": True,
    },
    {
        "code": "communication",
        "label": "💬 Сложности в коммуникации",
        "enabled": True,
        "builtin": True,
    },
    {
        "code": "comment",
        "label": "✍️ Свой комментарий",
        "enabled": True,
        "builtin": True,
    },
]


def _copy_defaults() -> list[dict[str, Any]]:
    return [dict(b) for b in DEFAULT_CHEF_BUTTONS]


def _chat_rec(data: dict[str, Any], chat_id: int) -> dict[str, Any]:
    chats = data.setdefault("chats", {})
    cid = str(chat_id)
    if cid not in chats:
        chats[cid] = {}
    return chats[cid]


def get_buttons(data: dict[str, Any], chat_id: int) -> list[dict[str, Any]]:
    rec = data.get("chats", {}).get(str(chat_id))
    if not isinstance(rec, dict):
        return _copy_defaults()
    raw = rec.get("chef_survey_buttons")
    if not isinstance(raw, list) or not raw:
        return _copy_defaults()
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        out.append(
            {
                "code": str(item["code"]),
                "label": str(item.get("label") or item["code"]),
                "enabled": bool(item.get("enabled", True)),
                "builtin": bool(item.get("builtin", False)),
            }
        )
    return out if out else _copy_defaults()


def save_buttons(data: dict[str, Any], chat_id: int, buttons: list[dict[str, Any]]) -> None:
    rec = _chat_rec(data, chat_id)
    rec["chef_survey_buttons"] = buttons


def enabled_buttons(buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in buttons if b.get("enabled")]


def count_enabled(buttons: list[dict[str, Any]]) -> int:
    return len(enabled_buttons(buttons))


def labels_map(data: dict[str, Any], chat_id: int) -> dict[str, str]:
    base = dict(CHEF_SURVEY_LABELS)
    base.update({b["code"]: b["label"] for b in get_buttons(data, chat_id)})
    return base


def merge_labels_for_chats(data: dict[str, Any], chat_ids: list[int]) -> dict[str, str]:
    merged = dict(CHEF_SURVEY_LABELS)
    for cid in chat_ids:
        merged.update(labels_map(data, cid))
    return merged


def valid_codes(data: dict[str, Any], chat_id: int) -> set[str]:
    return {b["code"] for b in enabled_buttons(get_buttons(data, chat_id))}


def build_survey_keyboard(data: dict[str, Any], chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list] = []
    for b in enabled_buttons(get_buttons(data, chat_id)):
        label = b["label"]
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"chef_sv:{b['code']}"[:64],
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Пропустить", callback_data="chef_sv:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def survey_prompt_text(chat_title: str) -> str:
    return (
        f"<b>Как прошла смена?</b> · {escape(chat_title)}\n\n"
        "Выберите, что было сложным на стыке кухни и зала, или нажмите «Пропустить»."
    )


def format_config_message(data: dict[str, Any], chat_id: int, chat_title: str) -> str:
    n = count_enabled(get_buttons(data, chat_id))
    return (
        f"👨‍🍳 <b>Кнопки опроса кухни</b> · {escape(chat_title)}\n"
        f"Активно <b>{n}/{MAX_ENABLED}</b>. Шеф видит их при оценке смены в личке.\n\n"
        "<i>Нажмите строку — вкл/выкл. Свои: ✏️ текст, 🗑 удалить.</i>"
    )


def config_keyboard(data: dict[str, Any], chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    buttons = get_buttons(data, chat_id)
    rows: list[list] = []
    for b in buttons[:12]:
        code = b["code"]
        mark = "✅" if b.get("enabled") else "⬜"
        short = b["label"][:28] + ("…" if len(b["label"]) > 28 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {short}",
                    callback_data=f"cb:t:{cid}:{code}"[:64],
                )
            ]
        )
        if not b.get("builtin"):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✏️", callback_data=f"cb:ed:{cid}:{code}"[:64]
                    ),
                    InlineKeyboardButton(
                        text="🗑", callback_data=f"cb:del:{cid}:{code}"[:64]
                    ),
                ]
            )
    custom_count = sum(1 for b in buttons if not b.get("builtin"))
    if count_enabled(buttons) < MAX_ENABLED and custom_count < MAX_CUSTOM:
        rows.append(
            [InlineKeyboardButton(text="➕ Своя кнопка", callback_data=f"cb:add:{cid}")]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"pr:l:{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def toggle_enabled(data: dict[str, Any], chat_id: int, code: str) -> tuple[bool, str | None]:
    buttons = get_buttons(data, chat_id)
    target = next((b for b in buttons if b["code"] == code), None)
    if not target:
        return False, "Кнопка не найдена."
    if target.get("enabled"):
        target["enabled"] = False
        save_buttons(data, chat_id, buttons)
        return True, None
    if count_enabled(buttons) >= MAX_ENABLED:
        return False, f"Можно включить не больше {MAX_ENABLED} кнопок."
    target["enabled"] = True
    save_buttons(data, chat_id, buttons)
    return True, None


def delete_custom(data: dict[str, Any], chat_id: int, code: str) -> tuple[bool, str | None]:
    buttons = get_buttons(data, chat_id)
    item = next((b for b in buttons if b["code"] == code), None)
    if not item:
        return False, "Кнопка не найдена."
    if item.get("builtin"):
        return False, "Стандартную кнопку нельзя удалить — только выключить."
    buttons = [b for b in buttons if b["code"] != code]
    save_buttons(data, chat_id, buttons)
    return True, None


def add_custom(data: dict[str, Any], chat_id: int, label: str) -> tuple[bool, str | None]:
    import survey_buttons as sb

    label = label.strip()
    if len(label) < 2 or len(label) > 40:
        return False, "Текст: от 2 до 40 символов."
    buttons = get_buttons(data, chat_id)
    if count_enabled(buttons) >= MAX_ENABLED:
        return False, f"Сначала выключите кнопку — активных уже {MAX_ENABLED}."
    if sum(1 for b in buttons if not b.get("builtin")) >= MAX_CUSTOM:
        return False, f"Своих кнопок не больше {MAX_CUSTOM}."
    codes = {b["code"] for b in buttons}
    code = sb.make_code_from_label(label, codes)
    buttons.append(
        {"code": code, "label": label, "enabled": True, "builtin": False}
    )
    save_buttons(data, chat_id, buttons)
    return True, None


def update_label(
    data: dict[str, Any], chat_id: int, code: str, new_label: str
) -> tuple[bool, str | None]:
    new_label = new_label.strip()
    if len(new_label) < 2 or len(new_label) > 40:
        return False, "Текст кнопки: от 2 до 40 символов."
    buttons = get_buttons(data, chat_id)
    item = next((b for b in buttons if b["code"] == code), None)
    if not item:
        return False, "Кнопка не найдена."
    item["label"] = new_label
    save_buttons(data, chat_id, buttons)
    return True, None


def department_title(dept: str) -> str:
    if dept == DEPARTMENT_KITCHEN:
        return "Кухня"
    if dept == DEPARTMENT_ALL:
        return "Весь ресторан"
    return "Зал"
