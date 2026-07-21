"""
Встроенные видео-ролики онбординга PulseTeam (mute MP4).
Открываются из «📚 Материалы» → «📖 Инструкция по пользованию».

Нативный сценарий: сразу первый ролик в чат, под ним «Далее» —
как просмотр пачки, без меню из 13 кнопок. Отправка через sendVideo.
"""
from __future__ import annotations

from pathlib import Path

REELS_DIR = Path(__file__).resolve().parent / "onboarding_reels" / "mp4"
BTN_GUIDE = "📖 Инструкция по пользованию"

# short_id -> title, filename, blurb
CATALOG: list[dict[str, str]] = [
    {
        "id": "menu",
        "title": "Карта меню",
        "file": "menu.mp4",
        "blurb": "Аналитика · Смена · Ещё",
    },
    {
        "id": "mats",
        "title": "Материалы",
        "file": "mats.mp4",
        "blurb": "Где лежит шпаргалка в боте",
    },
    {
        "id": "report",
        "title": "Отчёт",
        "file": "report.mp4",
        "blurb": "Точка → отдел → период → PDF",
    },
    {
        "id": "signals",
        "title": "Горящие вопросы",
        "file": "signals.mp4",
        "blurb": "Статусы и комментарий команде",
    },
    {
        "id": "alert",
        "title": "Push-алерт",
        "file": "alert.mp4",
        "blurb": "Бот сам пишет, когда смена красная",
    },
    {
        "id": "day",
        "title": "День · план",
        "file": "day.mp4",
        "blurb": "Чек-листы и план в группу",
    },
    {
        "id": "bcast",
        "title": "Шефу и менеджерам",
        "file": "bcast.mp4",
        "blurb": "Оперсвязь без чата линии",
    },
    {
        "id": "close",
        "title": "Закрытие дня",
        "file": "close.mp4",
        "blurb": "Вечерний чек-лист и дисциплина",
    },
    {
        "id": "stop",
        "title": "Стоп-лист",
        "file": "stop.mp4",
        "blurb": "Публикация в группу смены",
    },
    {
        "id": "kitchen",
        "title": "Кухня",
        "file": "kitchen.mp4",
        "blurb": "Оценка смены кухни",
    },
    {
        "id": "plan",
        "title": "План · задания",
        "file": "plan.mp4",
        "blurb": "Задания, кадры, закрытие за дату",
    },
    {
        "id": "access",
        "title": "Доступ",
        "file": "access.mp4",
        "blurb": "Подключить точку и роли",
    },
    {
        "id": "waiter",
        "title": "Линия · 10 сек",
        "file": "waiter.mp4",
        "blurb": "Как отвечает официант",
    },
]

_BY_ID = {r["id"]: r for r in CATALOG}


def catalog_len() -> int:
    return len(CATALOG)


def reel_at(index: int) -> dict[str, str] | None:
    if index < 0 or index >= len(CATALOG):
        return None
    return CATALOG[index]


def reel_index(reel_id: str) -> int | None:
    for i, row in enumerate(CATALOG):
        if row["id"] == reel_id:
            return i
    return None


def reel_path(reel_id: str) -> Path | None:
    row = _BY_ID.get(reel_id)
    if not row:
        return None
    path = REELS_DIR / row["file"]
    return path if path.is_file() else None


def reel_path_at(index: int) -> Path | None:
    row = reel_at(index)
    if not row:
        return None
    return reel_path(row["id"])


def caption_for(reel_id: str) -> str:
    idx = reel_index(reel_id)
    if idx is None:
        return "🎬 Ролик"
    return caption_at(idx)


def caption_at(index: int) -> str:
    row = reel_at(index)
    if not row:
        return "🎬 Ролик"
    n = index + 1
    total = len(CATALOG)
    return f"🎬 {row['title']} · {n}/{total}\n{row['blurb']}"


def nav_keyboard(chat_id: int, index: int):
    """Клавиатура под роликом: назад / прогресс / далее + выход."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    total = len(CATALOG)
    n = index + 1
    rows: list[list] = []

    nav: list = []
    if index > 0:
        nav.append(
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"tr:ob:g:{cid}:{index - 1}"[:64],
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{n}/{total}",
            callback_data=f"tr:ob:noop:{cid}"[:64],
        )
    )
    if index < total - 1:
        nav.append(
            InlineKeyboardButton(
                text="Далее →",
                callback_data=f"tr:ob:g:{cid}:{index + 1}"[:64],
            )
        )
    else:
        nav.append(
            InlineKeyboardButton(
                text="Готово ✓",
                callback_data=f"tr:mm:{cid}"[:64],
            )
        )
    rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text="☰ Темы",
                callback_data=f"tr:ob:topics:{cid}:{index}"[:64],
            ),
            InlineKeyboardButton(
                text="← К материалам",
                callback_data=f"tr:mm:{cid}"[:64],
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topics_keyboard(chat_id: int, current_index: int = 0):
    """Компактный список тем — только если нужно перепрыгнуть."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    for i, r in enumerate(CATALOG):
        mark = "· " if i != current_index else "▶ "
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{i + 1}. {r['title']}"[:36],
                    callback_data=f"tr:ob:g:{cid}:{i}"[:64],
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="← К ролику",
                callback_data=f"tr:ob:g:{cid}:{current_index}"[:64],
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_topics_menu(current_index: int = 0) -> str:
    row = reel_at(current_index)
    title = row["title"] if row else "ролик"
    return (
        f"<b>{BTN_GUIDE}</b> · темы\n\n"
        f"Сейчас: <b>{title}</b> ({current_index + 1}/{len(CATALOG)}).\n"
        "Выберите тему или вернитесь к ролику."
    )


def format_onboarding_menu() -> str:
    """Устарело для списка; оставлено для совместимости."""
    return (
        f"<b>{BTN_GUIDE}</b>\n\n"
        f"{len(CATALOG)} коротких роликов подряд.\n"
        "Смотрите как истории — кнопка <b>Далее</b> под видео."
    )


def onboarding_list_keyboard(chat_id: int):
    """Совместимость: больше не показываем длинный список как главный экран."""
    return topics_keyboard(chat_id, 0)


def patch_manager_menu_keyboard(markup, chat_id: int):
    from aiogram.types import InlineKeyboardButton

    cid = str(chat_id)
    rows = list(markup.inline_keyboard) if markup and markup.inline_keyboard else []
    rows.insert(
        0,
        [
            InlineKeyboardButton(
                text=BTN_GUIDE,
                callback_data=f"tr:ob:start:{cid}"[:64],
            )
        ],
    )
    markup.inline_keyboard = rows
    return markup


def enrich_manager_menu_text(text: str) -> str:
    return (
        text
        + f"\n\n📖 <b>Инструкция по пользованию</b> — {len(CATALOG)} коротких "
        "роликов подряд (как истории). Свои файлы сети — папками ниже."
    )
