"""Private trainee mode and menu-update nudges.

Preferences are keyed only by the same pseudonymous learner hash as menu_training.
Managers cannot query or view who enabled trainee mode.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

import db_pulse
import menu_training
import training_materials

BOT: Any = None
LOAD_DATA: Callable[[], Awaitable[dict[str, Any]]] | None = None
_SCHEMA_LOCK = asyncio.Lock()
_SCHEMA_READY = False

TZ_NAME = os.getenv("MENU_TRAINING_TIMEZONE", os.getenv("BOT_TIMEZONE", "Europe/Moscow"))
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:
    TZ = ZoneInfo("UTC")

NUDGE_HOURS = {
    int(x) for x in os.getenv("MENU_TRAINING_NUDGE_HOURS", "12,18").split(",")
    if x.strip().isdigit() and 0 <= int(x) <= 23
}

DDL = """
CREATE TABLE IF NOT EXISTS menu_training_preferences (
    learner_hash TEXT NOT NULL,
    restaurant_chat_id BIGINT NOT NULL,
    trainee_mode BOOLEAN NOT NULL DEFAULT false,
    last_nudge_slot TEXT,
    last_version_notified INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (learner_hash, restaurant_chat_id)
)
"""


def configure(bot: Any, load_data) -> None:
    global BOT, LOAD_DATA
    BOT = bot
    LOAD_DATA = load_data


async def _ensure_schema() -> bool:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return True
    pool = db_pulse.pool()
    if pool is None:
        return False
    async with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return True
        pool = db_pulse.pool()
        if pool is None:
            return False
        async with pool.acquire() as conn:
            await conn.execute(DDL)
        _SCHEMA_READY = True
        return True


async def _get_pref(learner: str, chat_id: int):
    if not await _ensure_schema():
        return None
    pool = db_pulse.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO menu_training_preferences (learner_hash,restaurant_chat_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            learner,
            chat_id,
        )
        return await conn.fetchrow(
            "SELECT * FROM menu_training_preferences WHERE learner_hash=$1 AND restaurant_chat_id=$2",
            learner,
            chat_id,
        )


def register(dp: Any) -> None:
    @dp.callback_query(F.data.startswith("mtn:t:"))
    async def toggle_trainee(callback: CallbackQuery) -> None:
        try:
            chat_id = int(callback.data.split(":", 2)[2])
        except Exception:
            await callback.answer("Ошибка", show_alert=True)
            return
        data = await LOAD_DATA()
        linked = training_materials.staff_chat_id(data, callback.from_user.id)
        if linked is None or int(linked) != chat_id:
            await callback.answer("Нет доступа к материалам этой точки", show_alert=True)
            return
        if not await _ensure_schema():
            await callback.answer("База обучения недоступна", show_alert=True)
            return
        learner = menu_training._learner_hash(callback.from_user.id)
        pref = await _get_pref(learner, chat_id)
        enabled = not bool(pref["trainee_mode"])
        pool = db_pulse.pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE menu_training_preferences SET trainee_mode=$3,updated_at=now() WHERE learner_hash=$1 AND restaurant_chat_id=$2",
                learner,
                chat_id,
                enabled,
            )
        await callback.answer("Режим стажёра включён" if enabled else "Режим стажёра выключен", show_alert=True)
        await callback.message.answer(
            "🎓 <b>Режим стажёра включён</b>\n\nЯ буду 1–2 раза в день напоминать пройти короткую тренировку. "
            "Слабые темы будут возвращаться чаще." if enabled else "🎓 Режим стажёра выключен.",
            parse_mode="HTML",
        )


async def worker() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            if BOT is None or LOAD_DATA is None or not await _ensure_schema():
                await asyncio.sleep(300)
                continue
            data = await LOAD_DATA()
            links = data.get("staff_restaurant_links") or {}
            if not isinstance(links, dict):
                links = {}
            now = datetime.now(TZ)
            slot = f"{now.date().isoformat()}:{now.hour}"
            pool = db_pulse.pool()
            for uid_raw, cid_raw in list(links.items()):
                try:
                    uid = int(uid_raw)
                    chat_id = int(cid_raw)
                except Exception:
                    continue
                rec = (data.get("chats") or {}).get(str(chat_id))
                if not isinstance(rec, dict):
                    continue
                root = rec.get("menu_training")
                if not isinstance(root, dict) or root.get("status") != "published" or not root.get("published_dishes"):
                    continue
                version = int(root.get("version") or 0)
                learner = menu_training._learner_hash(uid)
                pref = await _get_pref(learner, chat_id)
                if pref is None:
                    continue

                # Notify privately when a published menu version changes. No old mastery is
                # destroyed except changed dishes (handled by menu_training_publish).
                if version > int(pref["last_version_notified"] or 0):
                    try:
                        await BOT.send_message(
                            uid,
                            "🆕 <b>Меню обновилось</b>\n\nЕсть новые или изменённые позиции. "
                            "Пройди короткую тренировку — старый прогресс по неизменившимся блюдам сохранён.",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="⚡ Пройти обновление", callback_data=f"mt:q:quick:{chat_id}"[:64])
                            ]]),
                        )
                    except Exception:
                        pass
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE menu_training_preferences SET last_version_notified=$3,updated_at=now() WHERE learner_hash=$1 AND restaurant_chat_id=$2",
                            learner,
                            chat_id,
                            version,
                        )

                if bool(pref["trainee_mode"]) and now.hour in NUDGE_HOURS and str(pref["last_nudge_slot"] or "") != slot:
                    try:
                        await BOT.send_message(
                            uid,
                            "🎓 <b>5 минут на меню</b>\n\nКороткая тренировка уже собрана с учётом твоих слабых тем.",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="⚡ Начать тренировку", callback_data=f"mt:q:quick:{chat_id}"[:64])
                            ]]),
                        )
                    except Exception:
                        pass
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE menu_training_preferences SET last_nudge_slot=$3,updated_at=now() WHERE learner_hash=$1 AND restaurant_chat_id=$2",
                            learner,
                            chat_id,
                            slot,
                        )
        except Exception as exc:
            print("[menu-training nudges]", repr(exc))
        await asyncio.sleep(300)
