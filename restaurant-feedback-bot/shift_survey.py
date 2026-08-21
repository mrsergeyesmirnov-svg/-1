"""
Опрос смены: эмоция → что мешало → комментарий (текст или голос).

Эмоции пишутся в лог как rating 1–5 для отчётов и алертов.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# callback mood_<code> → число для аналитики
MOOD_TO_RATING: dict[str, int] = {
    "charged": 5,  # 🔥 Заряженная
    "normal": 4,  # 🙂 Нормальная
    "meh": 2,  # 😐 Так себе
    "heavy": 1,  # 🥶 Тяжёлая
}

MOOD_LABELS: dict[str, str] = {
    "charged": "🔥 Заряженная",
    "normal": "🙂 Нормальная",
    "meh": "😐 Так себе",
    "heavy": "🥶 Тяжёлая",
}

# Одно окно «Что мешало работать?»
BLOCKER_BUTTONS: list[tuple[str, str]] = [
    ("team", "👥 Команда"),
    ("kitchen", "👨‍🍳 Кухня"),
    ("guests", "🙋 Гости"),
    ("processes", "⚙️ Процессы"),
    ("self", "🧠 Моё состояние"),
    ("ok", "✨ Нигде — всё прошло хорошо"),
]

BLOCKER_LABELS: dict[str, str] = {code: label for code, label in BLOCKER_BUTTONS}

# Коды, которые считаются «проблемой» для сигналов (не ok)
BLOCKER_PROBLEM_CODES = frozenset(
    code for code, _ in BLOCKER_BUTTONS if code != "ok"
)


def mood_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=MOOD_LABELS["charged"], callback_data="mood_charged")],
            [InlineKeyboardButton(text=MOOD_LABELS["normal"], callback_data="mood_normal")],
            [InlineKeyboardButton(text=MOOD_LABELS["meh"], callback_data="mood_meh")],
            [InlineKeyboardButton(text=MOOD_LABELS["heavy"], callback_data="mood_heavy")],
        ]
    )


def blocker_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"blocker_{code}")]
            for code, label in BLOCKER_BUTTONS
        ]
    )


def comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="final_skip")],
        ]
    )


def rating_from_mood(code: str) -> int | None:
    return MOOD_TO_RATING.get(code)


def mood_prompt() -> str:
    return "Как прошла смена?"


def blocker_prompt() -> str:
    return "Что мешало работать?"


def comment_prompt() -> str:
    return (
        "Расскажите подробнее — текстом или <b>голосовым</b>.\n"
        "Или нажмите «Пропустить»."
    )
