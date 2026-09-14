"""Immediate TTK analysis actions, materials UI cleanup, and training answer routing.

This module does not read shift feedback and does not expose learner data.
"""
from __future__ import annotations

import asyncio
import html
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import menu_training
import training_materials

BOT: Any = None
LOAD_DATA: Callable[[], Awaitable[dict[str, Any]]] | None = None
SAVE_DATA: Callable[[dict[str, Any]], Awaitable[None]] | None = None
IS_GLOBAL_ADMIN: Callable[[int], bool] | None = None


def configure(bot, load_data, save_data, is_global_admin) -> None:
    global BOT, LOAD_DATA, SAVE_DATA, IS_GLOBAL_ADMIN
    BOT = bot
    LOAD_DATA = load_data
    SAVE_DATA = save_data
    IS_GLOBAL_ADMIN = is_global_admin


def apply_ui_cleanup(bot_module: Any, review_module: Any) -> None:
    """Temporarily hide onboarding guide and privacy-explainer copy from Materials."""
    onboarding = bot_module.onboarding_reels

    onboarding.enrich_manager_menu_text = lambda text: text
    onboarding.patch_manager_menu_keyboard = lambda markup, chat_id: markup

    original_format = bot_module.training_materials.format_manager_menu

    def clean_manager_menu(rec: dict[str, Any], chat_title: str) -> str:
        text = original_format(rec, chat_title)
        text = text.replace(
            "\n\n<i>Поимённый прогресс сотрудников менеджерам не показывается.</i>",
            "",
        )
        return text

    bot_module.training_materials.format_manager_menu = clean_manager_menu

    original_hub = review_module._hub

    def clean_hub(rec: dict[str, Any], title: str, chat_id: int):
        text, keyboard = original_hub(rec, title, chat_id)
        text = text.replace(
            "\n\n<i>Прогресс конкретных сотрудников менеджеру не раскрывается.</i>",
            "",
        )
        return text, keyboard

    review_module._hub = clean_hub

    original_status = menu_training._manager_status_text

    def clean_status(rec: dict[str, Any], title: str) -> str:
        text = original_status(rec, title)
        text = text.replace(
            "\n\n<i>Прогресс конкретных сотрудников здесь намеренно не показывается: обучение не раскрывает личности менеджеру.</i>",
            "",
        )
        return text

    menu_training._manager_status_text = clean_status


async def _handle_open_training_answer(message: Message) -> bool:
    """Consume a reply to the current open training question before bot.py catch-all text handler."""
    if message.chat.type != "private" or not message.text or not message.reply_to_message:
        return False
    if not await menu_training._ensure_schema():
        return False

    learner = menu_training._learner_hash(message.from_user.id)
    pool = menu_training.db_pulse.pool()
    if pool is None:
        return False
    async with pool.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT * FROM menu_training_sessions "
            "WHERE learner_hash=$1 AND status='active' "
            "ORDER BY created_at DESC LIMIT 1",
            learner,
        )
    if not session:
        return False

    prompt_id = session["prompt_message_id"]
    if not prompt_id or int(message.reply_to_message.message_id) != int(prompt_id):
        return False

    questions = menu_training._json(session["questions"], [])
    idx = int(session["current_index"] or 0)
    if idx >= len(questions):
        return False
    q = questions[idx]
    if q.get("type") != "open":
        return False

    wait_msg = await message.answer("⏱ Проверяю ответ…")
    try:
        # A training answer should feel instant. Never leave the user waiting indefinitely.
        grade = await asyncio.wait_for(
            menu_training._grade_open(q, message.text.strip()),
            timeout=15,
        )
    except asyncio.TimeoutError:
        try:
            await wait_msg.edit_text(
                "ИИ отвечает дольше обычного. Ответ не потерян — отправь его ещё раз через несколько секунд."
            )
        except Exception:
            await message.answer(
                "ИИ отвечает дольше обычного. Ответ не потерян — отправь его ещё раз через несколько секунд."
            )
        return True
    except Exception as exc:
        try:
            await wait_msg.edit_text(
                "Не удалось проверить ответ. Он не засчитан — попробуй отправить ещё раз."
            )
        except Exception:
            await message.answer("Не удалось проверить ответ. Попробуй отправить ещё раз.")
        print(f"[menu-training open grade] {exc!r}")
        return True

    value = max(0.0, min(1.0, float(grade.get("score") or 0) / 10.0))
    correct = bool(grade.get("correct"))
    feedback = html.escape(str(grade.get("feedback") or ""))
    ideal = html.escape(str(grade.get("ideal_answer") or q.get("model_answer") or ""))
    result_text = (
        f"<b>{'✅' if correct else '🔁'} {float(grade.get('score') or 0):.0f}/10</b>\n"
        f"{feedback}\n\n<b>Сильный ответ:</b> {ideal}"
    )
    try:
        await wait_msg.edit_text(result_text, parse_mode="HTML")
    except Exception:
        await message.answer(result_text, parse_mode="HTML")

    updated = await menu_training._accept_result(
        session,
        q,
        {
            "value": value,
            "correct": correct,
            "answer": message.text.strip(),
            "feedback": str(grade.get("feedback") or ""),
        },
    )
    if int(updated["current_index"] or 0) >= len(questions):
        await menu_training._finish_session(message.chat.id, str(session["id"]))
    else:
        await menu_training._show_question(message.chat.id, updated)
    return True


class _TrainingAnswerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            try:
                if await _handle_open_training_answer(event):
                    return None
            except Exception as exc:
                print(f"[menu-training middleware] {exc!r}")
        return await handler(event, data)


async def _run_analysis(uid: int, chat_id: int, metas: list[dict[str, Any]], label: str) -> None:
    done = 0
    failed = 0
    for meta in metas:
        try:
            data = await LOAD_DATA()
            rec = (data.get("chats") or {}).get(str(chat_id))
            if not isinstance(rec, dict):
                failed += 1
                continue
            row = next(
                (
                    f
                    for f in training_materials.list_files(rec)
                    if str(f.get("id")) == str(meta.get("id"))
                ),
                None,
            )
            if not row:
                failed += 1
                continue
            row["analysis_status"] = "processing"
            row["analysis_error"] = ""
            await SAVE_DATA(data)
            await menu_training._process_pending_file(chat_id, dict(row))

            check = await LOAD_DATA()
            check_rec = (check.get("chats") or {}).get(str(chat_id)) or {}
            check_row = next(
                (
                    f
                    for f in training_materials.list_files(check_rec)
                    if str(f.get("id")) == str(meta.get("id"))
                ),
                None,
            )
            if check_row and check_row.get("analysis_status") in {"done", "ignored"}:
                done += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            print(f"[menu-training immediate] chat={chat_id}: {exc!r}")

    data = await LOAD_DATA()
    rec = (data.get("chats") or {}).get(str(chat_id)) or {}
    root = rec.get("menu_training") if isinstance(rec.get("menu_training"), dict) else {}
    draft_n = len((root or {}).get("draft_dishes") or [])
    text = (
        f"<b>✅ Анализ ТТК завершён</b>\n\n"
        f"{html.escape(label)}\n"
        f"Обработано: <b>{done}</b>"
        + (f" · с ошибкой: <b>{failed}</b>" if failed else "")
        + f"\nЧерновик меню: <b>{draft_n}</b> позиций."
    )
    try:
        await BOT.send_message(
            uid,
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🧠 Открыть ТТК и тесты",
                            callback_data=f"mt:m:{chat_id}"[:64],
                        )
                    ]
                ]
            ),
        )
    except Exception as exc:
        print(f"[menu-training immediate notify] uid={uid}: {exc!r}")


def _supported_files(rec: dict[str, Any], folder_id: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in training_materials.list_files(rec, folder_id):
        ext = Path(str(row.get("title") or "")).suffix.lower()
        if ext in menu_training.SUPPORTED_EXTENSIONS:
            out.append(dict(row))
    return out


def register(dp: Any) -> None:
    # The main bot has a broad @dp.message(F.text) handler registered before the
    # training module. Middleware is therefore required so replies to open test
    # questions are not swallowed by that generic handler.
    if not getattr(dp, "_pulse_training_answer_middleware", False):
        dp.message.outer_middleware(_TrainingAnswerMiddleware())
        setattr(dp, "_pulse_training_answer_middleware", True)

    @dp.callback_query(F.data.startswith("mt:af:"))
    async def analyse_folder_now(callback: CallbackQuery) -> None:
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4:
            await callback.answer("Ошибка кнопки", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer("Точка не найдена", show_alert=True)
            return
        folder_id = parts[3]
        data = await LOAD_DATA()
        if not menu_training._manager_can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id))
        if not isinstance(rec, dict):
            await callback.answer("Точка не найдена", show_alert=True)
            return
        metas = _supported_files(rec, folder_id)
        if not metas:
            await callback.answer("В папке нет PDF, DOCX, XLSX или TXT", show_alert=True)
            return
        for row in training_materials.list_files(rec, folder_id):
            if any(str(row.get("id")) == str(m.get("id")) for m in metas):
                row["analysis_status"] = "processing"
                row["analysis_error"] = ""
        await SAVE_DATA(data)
        await callback.answer(f"Запустил анализ: {len(metas)}", show_alert=True)
        if callback.message:
            await callback.message.answer(
                f"🧠 Анализирую ТТК: <b>{len(metas)}</b> файл(а).\n"
                "Когда закончу, пришлю результат сюда.",
                parse_mode="HTML",
            )
        asyncio.create_task(
            _run_analysis(
                callback.from_user.id,
                chat_id,
                metas,
                f"Папка: {folder_id}",
            )
        )

    @dp.callback_query(F.data.startswith("mt:rean:"))
    async def reanalyse_all_now(callback: CallbackQuery) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 2)[2])
        except Exception:
            await callback.answer("Ошибка кнопки", show_alert=True)
            return
        data = await LOAD_DATA()
        if not menu_training._manager_can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id))
        if not isinstance(rec, dict):
            await callback.answer("Точка не найдена", show_alert=True)
            return
        metas = _supported_files(rec)
        if not metas:
            await callback.answer("Нет ТТК для анализа", show_alert=True)
            return
        root = menu_training._menu_root(rec)
        root["draft_dishes"] = []
        for row in training_materials.list_files(rec):
            if any(str(row.get("id")) == str(m.get("id")) for m in metas):
                row["analysis_status"] = "processing"
                row["analysis_error"] = ""
        await SAVE_DATA(data)
        await callback.answer(f"Запустил анализ: {len(metas)}", show_alert=True)
        if callback.message:
            await callback.message.answer(
                f"🧠 Повторно анализирую все ТТК: <b>{len(metas)}</b> файл(а).\n"
                "Когда закончу, пришлю результат сюда.",
                parse_mode="HTML",
            )
        asyncio.create_task(
            _run_analysis(
                callback.from_user.id,
                chat_id,
                metas,
                "Повторный анализ всех ТТК",
            )
        )
