"""Privacy-safe publication of reviewed menu knowledge.

Publishing a new menu version preserves mastery for unchanged dishes and clears only
changed/new dish mastery by anonymous learner hash. No learner identity is exposed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import F
from aiogram.types import CallbackQuery

import db_pulse
import pulse_model

LOAD_DATA: Callable[[], Awaitable[dict[str, Any]]] | None = None
SAVE_DATA: Callable[[dict[str, Any]], Awaitable[None]] | None = None
IS_GLOBAL_ADMIN: Callable[[int], bool] | None = None


def configure(load_data, save_data, is_global_admin) -> None:
    global LOAD_DATA, SAVE_DATA, IS_GLOBAL_ADMIN
    LOAD_DATA = load_data
    SAVE_DATA = save_data
    IS_GLOBAL_ADMIN = is_global_admin


def _can(data: dict[str, Any], uid: int, chat_id: int) -> bool:
    if IS_GLOBAL_ADMIN and IS_GLOBAL_ADMIN(uid):
        return True
    try:
        return str(chat_id) in pulse_model.allowed_chat_ids_for_manager(data, uid)
    except Exception:
        return False


def _fingerprint(dish: dict[str, Any]) -> str:
    payload = {
        "name": dish.get("name") or "",
        "category": dish.get("category") or "",
        "ingredients": dish.get("ingredients") or [],
        "allergens": dish.get("allergens") or [],
        "allergens_confirmed": bool(dish.get("allergens_confirmed")),
        "preparation": dish.get("preparation") or "",
        "serving": dish.get("serving") or "",
        "weight": dish.get("weight") or "",
        "sales_description": dish.get("sales_description") or "",
        "important_facts": dish.get("important_facts") or [],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


async def _reset_changed_mastery(chat_id: int, changed_keys: list[str]) -> None:
    if not changed_keys:
        return
    pool = db_pulse.pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM menu_training_mastery WHERE restaurant_chat_id=$1 AND dish_key=ANY($2::text[])",
                chat_id,
                changed_keys,
            )
    except Exception as exc:
        # Table may not exist yet if nobody has ever started training; that's fine.
        print("[menu-training publish reset]", repr(exc))


def register(dp: Any) -> None:
    @dp.callback_query(F.data.startswith("mt:pub:"))
    async def publish(callback: CallbackQuery) -> None:
        try:
            chat_id = int(callback.data.split(":", 2)[2])
        except Exception:
            await callback.answer("Ошибка", show_alert=True)
            return
        data = await LOAD_DATA()
        if not _can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id))
        if not isinstance(rec, dict):
            await callback.answer("Точка не найдена", show_alert=True)
            return
        root = rec.get("menu_training")
        if not isinstance(root, dict):
            await callback.answer("Черновик пуст", show_alert=True)
            return
        draft = [d for d in (root.get("draft_dishes") or []) if isinstance(d, dict) and d.get("name")]
        if not draft:
            await callback.answer("Черновик пуст", show_alert=True)
            return
        old = [d for d in (root.get("published_dishes") or []) if isinstance(d, dict) and d.get("key")]
        old_fp = {str(d.get("key")): _fingerprint(d) for d in old}
        changed_keys = [
            str(d.get("key"))
            for d in draft
            if d.get("key") and old_fp.get(str(d.get("key"))) != _fingerprint(d)
        ]
        root["published_dishes"] = draft
        root["status"] = "published"
        root["version"] = int(root.get("version") or 0) + 1
        root["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        root["changed_keys"] = changed_keys
        await SAVE_DATA(data)
        await _reset_changed_mastery(chat_id, changed_keys)
        unchanged = max(0, len(draft) - len(changed_keys))
        await callback.answer(
            f"Опубликовано: {len(draft)}. Новых/изменённых: {len(changed_keys)}, без сброса прогресса: {unchanged}.",
            show_alert=True,
        )
