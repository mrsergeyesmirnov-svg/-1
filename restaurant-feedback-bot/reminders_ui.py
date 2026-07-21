"""
UI напоминаний опроса смены для менеджера (кнопки, не только /settime).
"""
from __future__ import annotations

from html import escape
from typing import Any

TIME_PRESETS = (
    "10:00",
    "14:00",
    "18:00",
    "21:00",
    "22:00",
    "22:30",
    "23:00",
    "23:30",
)

# short code -> IANA timezone
TZ_CHOICES: list[tuple[str, str, str]] = [
    ("msk", "Europe/Moscow", "Москва"),
    ("spb", "Europe/Moscow", "СПб (MSK)"),
    ("klg", "Europe/Kaliningrad", "Калининград"),
    ("sam", "Europe/Samara", "Самара"),
    ("yek", "Asia/Yekaterinburg", "Екатеринбург"),
    ("oms", "Asia/Omsk", "Омск"),
    ("kras", "Asia/Krasnoyarsk", "Красноярск"),
    ("irk", "Asia/Irkutsk", "Иркутск"),
    ("yak", "Asia/Yakutsk", "Якутск"),
    ("vlad", "Asia/Vladivostok", "Владивосток"),
]

_TZ_BY_CODE = {c: iana for c, iana, _ in TZ_CHOICES}


def tz_iana(code: str) -> str | None:
    return _TZ_BY_CODE.get(code)


def format_panel(rec: dict[str, Any], title: str) -> str:
    arr = list(rec.get("auto_times") or [])
    t1 = arr[0] if len(arr) > 0 and arr[0] else "—"
    t2 = arr[1] if len(arr) > 1 and arr[1] else "—"
    tz = str(rec.get("timezone") or "Europe/Moscow")
    return (
        f"<b>⏰ Напоминания</b> · {escape(title)}\n\n"
        f"1-е: <b>{escape(t1)}</b>\n"
        f"2-е: <b>{escape(t2)}</b>\n"
        f"Пояс: <code>{escape(tz)}</code>\n\n"
        "Бот в это время пишет в <b>группу смены</b> кнопку «отметить в личку».\n"
        "Выберите слот или пояс ниже."
    )


def panel_keyboard(chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1️⃣ Первое время",
                    callback_data=f"rm:s:{cid}:0"[:64],
                ),
                InlineKeyboardButton(
                    text="2️⃣ Второе время",
                    callback_data=f"rm:s:{cid}:1"[:64],
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Убрать 1-е",
                    callback_data=f"rm:d:{cid}:0"[:64],
                ),
                InlineKeyboardButton(
                    text="🗑 Убрать 2-е",
                    callback_data=f"rm:d:{cid}:1"[:64],
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌍 Часовой пояс",
                    callback_data=f"rm:tz:{cid}"[:64],
                )
            ],
        ]
    )


def slot_presets_keyboard(chat_id: int, slot: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    row: list = []
    for t in TIME_PRESETS:
        hhmm = t.replace(":", "")
        row.append(
            InlineKeyboardButton(
                text=t,
                callback_data=f"rm:t:{cid}:{slot}:{hhmm}"[:64],
            )
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Своё время",
                callback_data=f"rm:c:{cid}:{slot}"[:64],
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"rm:p:{cid}"[:64],
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def timezone_keyboard(chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    row: list = []
    for code, _iana, label in TZ_CHOICES:
        # skip duplicate MSK label for spb as separate? keep both for clarity
        if code == "spb":
            continue
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"rm:z:{cid}:{code}"[:64],
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"rm:p:{cid}"[:64])]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_preset_hhmm(raw: str) -> str | None:
    raw = (raw or "").strip()
    if len(raw) == 4 and raw.isdigit():
        return f"{raw[:2]}:{raw[2:]}"
    return None
