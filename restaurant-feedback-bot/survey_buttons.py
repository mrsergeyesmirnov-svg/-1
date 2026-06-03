"""
Быстрые кнопки «что повлияло» — настройка на точку (групповой чат).
Максимум MAX_ENABLED активных кнопок в опросе.
"""
from __future__ import annotations

import re
from html import escape
from typing import Any

MAX_ENABLED = 6
MAX_CUSTOM = 8

DEFAULT_BUTTONS: list[dict[str, Any]] = [
    {
        "code": "kitchen",
        "label": "🍽 Медленная кухня",
        "enabled": True,
        "builtin": True,
        "threshold": 3,
    },
    {
        "code": "conflict",
        "label": "😤 Конфликт / напряжение",
        "enabled": True,
        "builtin": True,
        "threshold": 3,
    },
    {
        "code": "staff",
        "label": "👥 Нехватка персонала",
        "enabled": True,
        "builtin": True,
        "threshold": 3,
    },
    {
        "code": "management",
        "label": "📋 Плохая организация",
        "enabled": True,
        "builtin": True,
        "threshold": 2,
    },
    {
        "code": "stress",
        "label": "😓 Сильная нагрузка",
        "enabled": True,
        "builtin": True,
        "threshold": 3,
    },
    {
        "code": "comment",
        "label": "💬 Свой комментарий",
        "enabled": False,
        "builtin": True,
        "threshold": 4,
    },
]


def _copy_defaults() -> list[dict[str, Any]]:
    return [dict(b) for b in DEFAULT_BUTTONS]


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
    raw = rec.get("problem_buttons")
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
                "threshold": int(item.get("threshold", 3)),
            }
        )
    return out if out else _copy_defaults()


def save_buttons(data: dict[str, Any], chat_id: int, buttons: list[dict[str, Any]]) -> None:
    rec = _chat_rec(data, chat_id)
    rec["problem_buttons"] = buttons


def enabled_buttons(buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in buttons if b.get("enabled")]


def count_enabled(buttons: list[dict[str, Any]]) -> int:
    return len(enabled_buttons(buttons))


def labels_map(data: dict[str, Any], chat_id: int) -> dict[str, str]:
    return {b["code"]: b["label"] for b in get_buttons(data, chat_id)}


def merge_labels_for_chats(data: dict[str, Any], chat_ids: list[int]) -> dict[str, str]:
    merged: dict[str, str] = {}
    import report_pulse

    merged.update(report_pulse.PROBLEM_LABELS)
    for cid in chat_ids:
        merged.update(labels_map(data, cid))
    return merged


def thresholds_map(data: dict[str, Any], chat_id: int) -> dict[str, int]:
    return {
        b["code"]: int(b.get("threshold", 3))
        for b in get_buttons(data, chat_id)
        if b.get("enabled")
    }


def titles_map(data: dict[str, Any], chat_id: int) -> dict[str, str]:
    """Короткое название для реестра проблем (без эмодзи по возможности)."""
    out: dict[str, str] = {}
    for b in get_buttons(data, chat_id):
        label = str(b["label"])
        plain = re.sub(r"[\U00010000-\U0010ffff]", "", label).strip()
        plain = re.sub(r"\s+", " ", plain).strip() or label
        out[b["code"]] = plain
    return out


def valid_codes(data: dict[str, Any], chat_id: int) -> set[str]:
    return {b["code"] for b in enabled_buttons(get_buttons(data, chat_id))}


def make_code_from_label(label: str, existing: set[str]) -> str:
    import hashlib

    ascii_part = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:30]
    if len(ascii_part.replace("_", "")) >= 2:
        base = ascii_part
    else:
        base = "c_" + hashlib.md5(label.encode("utf-8")).hexdigest()[:8]
    code = base
    n = 2
    while code in existing:
        code = f"{base}_{n}"
        n += 1
    return code


def build_problem_keyboard(data: dict[str, Any], chat_id: int):
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
                    callback_data=f"problem_{b['code']}"[:64],
                )
            ]
        )
    if not rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🍽 Медленная кухня",
                    callback_data="problem_kitchen",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="Пропустить", callback_data="problem_skip")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_config_message(data: dict[str, Any], chat_id: int, chat_title: str) -> str:
    n = count_enabled(get_buttons(data, chat_id))
    hint = (
        f"Лимит {MAX_ENABLED} активных — выключите лишнюю, чтобы включить другую."
        if n >= MAX_ENABLED
        else "Нажмите строку — вкл/выкл. Свои: ✏️ текст, 🗑 удалить."
    )
    return (
        f"⚙️ <b>Кнопки опроса</b> · {escape(chat_title)}\n"
        f"Активно <b>{n}/{MAX_ENABLED}</b>. {hint}"
    )


def config_keyboard(data: dict[str, Any], chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = get_buttons(data, chat_id)
    rows: list[list] = []

    for b in buttons[:14]:
        code = b["code"]
        mark = "✅" if b.get("enabled") else "⬜"
        short = b["label"][:28] + ("…" if len(b["label"]) > 28 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {short}",
                    callback_data=f"pb:t:{code}"[:64],
                )
            ]
        )
        if not b.get("builtin"):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✏️",
                        callback_data=f"pb:ed:{code}"[:64],
                    ),
                    InlineKeyboardButton(
                        text="🗑",
                        callback_data=f"pb:del:{code}"[:64],
                    ),
                ]
            )

    custom_count = sum(1 for b in buttons if not b.get("builtin"))
    if count_enabled(buttons) < MAX_ENABLED and custom_count < MAX_CUSTOM:
        rows.append(
            [InlineKeyboardButton(text="➕ Своя кнопка", callback_data="pb:add")]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="pr:l")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def toggle_enabled(data: dict[str, Any], chat_id: int, code: str) -> tuple[bool, str | None]:
    buttons = get_buttons(data, chat_id)
    target = None
    for b in buttons:
        if b["code"] == code:
            target = b
            break
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
    if count_enabled(buttons) < 1:
        for b in buttons:
            if b["code"] == "comment":
                b["enabled"] = True
                break
    save_buttons(data, chat_id, buttons)
    return True, None


def add_custom(
    data: dict[str, Any], chat_id: int, label: str
) -> tuple[bool, str | None]:
    label = label.strip()
    if len(label) < 2:
        return False, "Слишком короткий текст. Напишите 2–40 символов."
    if len(label) > 40:
        return False, "До 40 символов на кнопку."
    buttons = get_buttons(data, chat_id)
    if count_enabled(buttons) >= MAX_ENABLED:
        return False, f"Сначала выключите кнопку — активных уже {MAX_ENABLED}."
    if sum(1 for b in buttons if not b.get("builtin")) >= MAX_CUSTOM:
        return False, f"Своих кнопок не больше {MAX_CUSTOM}. Удалите лишнюю."
    codes = {b["code"] for b in buttons}
    code = make_code_from_label(label, codes)
    buttons.append(
        {
            "code": code,
            "label": label,
            "enabled": True,
            "builtin": False,
            "threshold": 3,
        }
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
