"""
Встроенные HTML-ролики онбординга PulseTeam.
Открываются из «📚 Материалы» у менеджера — бот шлёт файл, на телефоне открыть в браузере.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

REELS_DIR = Path(__file__).resolve().parent / "onboarding_reels"

# short_id -> (title, filename, blurb)
CATALOG: list[dict[str, str]] = [
    {
        "id": "menu",
        "title": "01 · Карта меню",
        "file": "demo-manager-menu.html",
        "blurb": "Аналитика · Смена · Ещё",
    },
    {
        "id": "mats",
        "title": "02 · Материалы",
        "file": "demo-manager-materials.html",
        "blurb": "Где лежит шпаргалка в боте",
    },
    {
        "id": "report",
        "title": "03 · Отчёт",
        "file": "demo-manager-report.html",
        "blurb": "Точка → отдел → период → PDF",
    },
    {
        "id": "signals",
        "title": "04 · Горящие вопросы",
        "file": "demo-manager-signals.html",
        "blurb": "Статусы и комментарий команде",
    },
    {
        "id": "alert",
        "title": "05 · Push-алерт",
        "file": "demo-manager-alert.html",
        "blurb": "Бот сам пишет, когда смена красная",
    },
    {
        "id": "day",
        "title": "06 · День · план",
        "file": "demo-manager-day.html",
        "blurb": "Чек-листы и план в группу",
    },
    {
        "id": "bcast",
        "title": "07 · Шефу и менеджерам",
        "file": "demo-manager-broadcast.html",
        "blurb": "Оперсвязь без чата линии",
    },
    {
        "id": "close",
        "title": "08 · Закрытие дня",
        "file": "demo-manager-close.html",
        "blurb": "Вечерний чек-лист и дисциплина",
    },
    {
        "id": "stop",
        "title": "09 · Стоп-лист",
        "file": "demo-manager-stop.html",
        "blurb": "Публикация в группу смены",
    },
    {
        "id": "kitchen",
        "title": "10 · Кухня",
        "file": "demo-manager-kitchen.html",
        "blurb": "Оценка смены кухни",
    },
    {
        "id": "plan",
        "title": "11 · План · задания",
        "file": "demo-manager-plan.html",
        "blurb": "Задания, кадры, закрытие за дату",
    },
    {
        "id": "access",
        "title": "12 · Доступ",
        "file": "demo-manager-access.html",
        "blurb": "Подключить точку и роли",
    },
    {
        "id": "waiter",
        "title": "13 · Линия · 10 сек",
        "file": "demo-waiter-checkin.html",
        "blurb": "Как отвечает официант",
    },
]

_BY_ID = {r["id"]: r for r in CATALOG}


def reel_path(reel_id: str) -> Path | None:
    row = _BY_ID.get(reel_id)
    if not row:
        return None
    path = REELS_DIR / row["file"]
    return path if path.is_file() else None


def format_onboarding_menu() -> str:
    lines = [
        "<b>🎬 Онбординг Pulse</b>",
        "",
        "Короткие ролики по меню менеджера. Нажмите пункт — пришлю HTML.",
        "На телефоне: откройте файл <b>в браузере</b> — ролик играет сам.",
        "",
        "<i>Это встроенный пакет Pulse, не ваши папки обучения.</i>",
    ]
    return "\n".join(lines)


def onboarding_list_keyboard(chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    for r in CATALOG:
        # callback max 64 bytes: tr:ob:s:{cid}:{id}
        rows.append(
            [
                InlineKeyboardButton(
                    text=r["title"][:36],
                    callback_data=f"tr:ob:s:{cid}:{r['id']}"[:64],
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="← К материалам", callback_data=f"tr:mm:{cid}"[:64])]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def caption_for(reel_id: str) -> str:
    row = _BY_ID.get(reel_id) or {}
    title = row.get("title", "Ролик")
    blurb = row.get("blurb", "")
    return (
        f"🎬 {title}\n{blurb}\n\n"
        "Откройте файл в браузере (Chrome / Safari) — автопросмотр."
    )


def patch_manager_menu_keyboard(markup, chat_id: int):
    """Добавляет кнопку онбординга в клавиатуру материалов менеджера."""
    from aiogram.types import InlineKeyboardButton

    cid = str(chat_id)
    rows = list(markup.inline_keyboard) if markup and markup.inline_keyboard else []
    rows.insert(
        0,
        [
            InlineKeyboardButton(
                text="🎬 Онбординг Pulse",
                callback_data=f"tr:ob:list:{cid}"[:64],
            )
        ],
    )
    markup.inline_keyboard = rows
    return markup


def enrich_manager_menu_text(text: str) -> str:
    return (
        text
        + "\n\n🎬 <b>Онбординг Pulse</b> — встроенные ролики по меню "
        "(кнопка выше). Свои файлы сети — папками ниже."
    )
