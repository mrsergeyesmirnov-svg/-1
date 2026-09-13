"""Manager-only review of AI-parsed TTK drafts.

This UI deliberately contains no learner identities or learner progress.
"""
from __future__ import annotations

import html
from typing import Any, Awaitable, Callable

from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

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


def _draft(rec: dict[str, Any]) -> list[dict[str, Any]]:
    root = rec.get("menu_training")
    if not isinstance(root, dict):
        return []
    return [d for d in (root.get("draft_dishes") or []) if isinstance(d, dict) and d.get("name")]


def _list_text(rec: dict[str, Any], title: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    dishes = _draft(rec)
    per_page = 8
    pages = max(1, (len(dishes) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    start = page * per_page
    chunk = dishes[start:start + per_page]
    lines = [
        f"<b>👀 Черновик ТТК</b> · {html.escape(title)}",
        "",
        f"Позиций: <b>{len(dishes)}</b> · страница {page + 1}/{pages}",
        "",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for idx, dish in enumerate(chunk, start=start):
        name = str(dish.get("name") or "Без названия")
        allergen = "✅" if dish.get("allergens_confirmed") else "⚠️"
        lines.append(f"{idx + 1}. {allergen} {html.escape(name)}")
        rows.append([
            InlineKeyboardButton(text=f"{idx + 1}. {name[:28]}", callback_data=f"mtr:d:{{chat}}:{idx}"[:64])
        ])
    # placeholder chat is replaced by caller to keep this formatter pure
    if not chunk:
        lines.append("<i>Черновик пуст.</i>")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _list_keyboard(chat_id: int, rec: dict[str, Any], page: int) -> InlineKeyboardMarkup:
    dishes = _draft(rec)
    per_page = 8
    pages = max(1, (len(dishes) + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    start = page * per_page
    rows: list[list[InlineKeyboardButton]] = []
    for idx, dish in enumerate(dishes[start:start + per_page], start=start):
        name = str(dish.get("name") or "Без названия")
        rows.append([InlineKeyboardButton(text=f"{idx + 1}. {name[:30]}", callback_data=f"mtr:d:{chat_id}:{idx}"[:64])])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"mtr:l:{chat_id}:{page-1}"[:64]))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"mtr:l:{chat_id}:{page+1}"[:64]))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🧠 Статус и публикация", callback_data=f"mt:m:{chat_id}"[:64])])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail(dish: dict[str, Any]) -> str:
    ingredients = ", ".join(str(x) for x in (dish.get("ingredients") or [])) or "—"
    allergens = ", ".join(str(x) for x in (dish.get("allergens") or [])) or "—"
    allergen_status = "подтверждены источником" if dish.get("allergens_confirmed") else "НЕ подтверждены источником — вопросы по аллергенам не создаются"
    facts = "\n".join(f"• {html.escape(str(x))}" for x in (dish.get("important_facts") or [])) or "—"
    return (
        f"<b>{html.escape(str(dish.get('name') or 'Без названия'))}</b>\n"
        f"Категория: {html.escape(str(dish.get('category') or '—'))}\n"
        f"Вес/выход: {html.escape(str(dish.get('weight') or '—'))}\n\n"
        f"<b>Состав</b>\n{html.escape(ingredients)}\n\n"
        f"<b>Аллергены</b>\n{html.escape(allergens)}\n<i>{html.escape(allergen_status)}</i>\n\n"
        f"<b>Технология</b>\n{html.escape(str(dish.get('preparation') or '—'))}\n\n"
        f"<b>Подача</b>\n{html.escape(str(dish.get('serving') or '—'))}\n\n"
        f"<b>Продающее описание</b>\n{html.escape(str(dish.get('sales_description') or '—'))}\n\n"
        f"<b>Важные факты</b>\n{facts}"
    )


def register(dp: Any) -> None:
    @dp.callback_query(F.data.startswith("mtr:l:"))
    async def list_draft(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 4:
            await callback.answer("Ошибка", show_alert=True)
            return
        chat_id, page = int(parts[2]), int(parts[3])
        data = await LOAD_DATA()
        if not _can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id)) or {}
        title = str(rec.get("title", chat_id))
        dishes = _draft(rec)
        pages = max(1, (len(dishes) + 7) // 8)
        page = max(0, min(page, pages - 1))
        start = page * 8
        lines = [
            f"<b>👀 Черновик ТТК</b> · {html.escape(title)}",
            "",
            f"Позиций: <b>{len(dishes)}</b> · страница {page + 1}/{pages}",
            "",
            "✅ — аллергены явно подтверждены источником",
            "⚠️ — аллергенность не подтверждена, такие вопросы бот не задаст",
        ]
        for idx, dish in enumerate(dishes[start:start + 8], start=start):
            marker = "✅" if dish.get("allergens_confirmed") else "⚠️"
            lines.append(f"\n{idx + 1}. {marker} {html.escape(str(dish.get('name') or 'Без названия'))}")
        if not dishes:
            lines.append("\n<i>Черновик пуст.</i>")
        await callback.answer()
        await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=_list_keyboard(chat_id, rec, page))

    @dp.callback_query(F.data.startswith("mtr:d:"))
    async def detail(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 4:
            await callback.answer("Ошибка", show_alert=True)
            return
        chat_id, idx = int(parts[2]), int(parts[3])
        data = await LOAD_DATA()
        if not _can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id)) or {}
        dishes = _draft(rec)
        if idx < 0 or idx >= len(dishes):
            await callback.answer("Позиция не найдена", show_alert=True)
            return
        page = idx // 8
        await callback.answer()
        await callback.message.answer(
            _detail(dishes[idx]),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Исключить из черновика", callback_data=f"mtr:x:{chat_id}:{idx}"[:64])],
                [InlineKeyboardButton(text="← К списку", callback_data=f"mtr:l:{chat_id}:{page}"[:64])],
            ]),
        )

    @dp.callback_query(F.data.startswith("mtr:x:"))
    async def exclude(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 4:
            await callback.answer("Ошибка", show_alert=True)
            return
        chat_id, idx = int(parts[2]), int(parts[3])
        data = await LOAD_DATA()
        if not _can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id))
        if not isinstance(rec, dict):
            await callback.answer("Точка не найдена", show_alert=True)
            return
        root = rec.get("menu_training")
        dishes = _draft(rec)
        if not isinstance(root, dict) or idx < 0 or idx >= len(dishes):
            await callback.answer("Позиция не найдена", show_alert=True)
            return
        removed = dishes.pop(idx)
        root["draft_dishes"] = dishes
        await SAVE_DATA(data)
        await callback.answer(f"Исключено: {str(removed.get('name') or '')[:40]}", show_alert=True)
