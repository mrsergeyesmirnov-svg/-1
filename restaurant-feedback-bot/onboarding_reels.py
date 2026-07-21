"""
Встроенные видео-ролики онбординга PulseTeam (mute MP4).
Открываются из «📚 Материалы» → «🎬 Онбординг Pulse».
Бот шлёт animation (Telegram крутит как гифку, без браузера).
"""
from __future__ import annotations

from pathlib import Path

REELS_DIR = Path(__file__).resolve().parent / "onboarding_reels" / "mp4"

# short_id -> title, filename, blurb
CATALOG: list[dict[str, str]] = [
    {
        "id": "menu",
        "title": "01 · Карта меню",
        "file": "menu.mp4",
        "blurb": "Аналитика · Смена · Ещё",
    },
    {
        "id": "mats",
        "title": "02 · Материалы",
        "file": "mats.mp4",
        "blurb": "Где лежит шпаргалка в боте",
    },
    {
        "id": "report",
        "title": "03 · Отчёт",
        "file": "report.mp4",
        "blurb": "Точка → отдел → период → PDF",
    },
    {
        "id": "signals",
        "title": "04 · Горящие вопросы",
        "file": "signals.mp4",
        "blurb": "Статусы и комментарий команде",
    },
    {
        "id": "alert",
        "title": "05 · Push-алерт",
        "file": "alert.mp4",
        "blurb": "Бот сам пишет, когда смена красная",
    },
    {
        "id": "day",
        "title": "06 · День · план",
        "file": "day.mp4",
        "blurb": "Чек-листы и план в группу",
    },
    {
        "id": "bcast",
        "title": "07 · Шефу и менеджерам",
        "file": "bcast.mp4",
        "blurb": "Оперсвязь без чата линии",
    },
    {
        "id": "close",
        "title": "08 · Закрытие дня",
        "file": "close.mp4",
        "blurb": "Вечерний чек-лист и дисциплина",
    },
    {
        "id": "stop",
        "title": "09 · Стоп-лист",
        "file": "stop.mp4",
        "blurb": "Публикация в группу смены",
    },
    {
        "id": "kitchen",
        "title": "10 · Кухня",
        "file": "kitchen.mp4",
        "blurb": "Оценка смены кухни",
    },
    {
        "id": "plan",
        "title": "11 · План · задания",
        "file": "plan.mp4",
        "blurb": "Задания, кадры, закрытие за дату",
    },
    {
        "id": "access",
        "title": "12 · Доступ",
        "file": "access.mp4",
        "blurb": "Подключить точку и роли",
    },
    {
        "id": "waiter",
        "title": "13 · Линия · 10 сек",
        "file": "waiter.mp4",
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
    return (
        "<b>🎬 Онбординг Pulse</b>\n\n"
        "Короткие ролики по меню менеджера.\n"
        "Нажмите пункт — пришлю <b>видео</b> прямо в чат "
        "(крутится само, как гифка).\n\n"
        "<i>Встроенный пакет Pulse, не ваши папки обучения.</i>"
    )


def onboarding_list_keyboard(chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    for r in CATALOG:
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
    return f"🎬 {title}\n{blurb}"


def patch_manager_menu_keyboard(markup, chat_id: int):
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
        + "\n\n🎬 <b>Онбординг Pulse</b> — видео-ролики по меню "
        "(кнопка выше). Свои файлы сети — папками ниже."
    )
