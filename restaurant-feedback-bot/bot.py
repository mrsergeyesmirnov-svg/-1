"""
Бот смен: в группе — только напоминание и ссылка в личку; оценка и текст — только в личке (анонимно для чата).
Чаты = разные рестораны. Расписание авто-напоминаний + /send_now.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from html import escape
import re
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import BaseFilter, CommandStart, Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ChatMemberUpdated,
    FSInputFile,
    MenuButtonWebApp,
    WebAppInfo,
)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

import pulse_model
import db_pulse
import report_pulse
import problems_pulse
import manager_alerts
import survey_buttons
import chef_survey
import shift_checklists
import pdf_report
import admin_metrics
import ops_day
import monthly_ops
import staff_assign
import training_materials
import onboarding_reels
import reminders_ui
import iiko_bridge
import ai_advisor
import ai_auditor
import miniapp_api
import shift_survey

# На Railway без Volume файлы в контейнере теряются при redeploy.
# Смонтируйте Volume и задайте PULSE_DATA_DIR=/data (или другой путь) — туда пойдут bot_data.json и feedback_log.jsonl.
_data_root = Path(os.getenv("PULSE_DATA_DIR", "").strip() or Path(__file__).resolve().parent)
_data_root.mkdir(parents=True, exist_ok=True)

DATA_PATH = _data_root / "bot_data.json"
FEEDBACK_LOG_PATH = _data_root / "feedback_log.jsonl"
DATA_LOCK = asyncio.Lock()
LOG_LOCK = asyncio.Lock()
DEFAULT_TZ = "Europe/Moscow"

_MSK_FALLBACK = timezone(timedelta(hours=3))


def get_tz(tz_name: str | None = None) -> timezone | ZoneInfo:
    """Windows без tzdata не знает Europe/Moscow — ставьте pip install tzdata или используется МСК UTC+3."""
    name = (tz_name or DEFAULT_TZ).strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        if name == DEFAULT_TZ:
            return _MSK_FALLBACK
        try:
            return ZoneInfo("UTC")
        except Exception:
            return _MSK_FALLBACK


def _parse_admin_ids() -> set[int]:
    """Только из .env — без подстановок. Владелец бота один (ваш id в ADMIN_IDS)."""
    raw = os.getenv("ADMIN_IDS", "").strip()
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


ADMIN_IDS = _parse_admin_ids()
ADMINS_BY_ABS = {abs(x) for x in ADMIN_IDS}

SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("Задайте BOT_TOKEN в окружении или в файле .env рядом с bot.py")
if not ADMIN_IDS:
    raise SystemExit(
        "Задайте ADMIN_IDS в .env — ваш Telegram id (узнать: временно поставьте любой токен, "
        "напишите @userinfobot в Telegram). Пример: ADMIN_IDS=5274130715"
    )

bot = Bot(token=TOKEN)
dp = Dispatcher()


class ManagerMenuFilter(BaseFilter):
    """Текст одной из кнопок меню и роль менеджера или глобальный админ."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.type != "private" or not message.text:
            return False
        if message.text.strip() not in pulse_model.MANAGER_MENU_BUTTONS:
            return False
        data = await load_data()
        uid = message.from_user.id
        return is_global_admin(uid) or pulse_model.has_manager_access(data, uid)


class AdminCommandsFilter(BaseFilter):
    """Справочник команд — только глобальный админ."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.type != "private" or not message.text:
            return False
        if message.text.strip() != pulse_model.BTN_COMMANDS:
            return False
        return is_global_admin(message.from_user.id)


class ChefMenuFilter(BaseFilter):
    """Меню шефа (стоп-лист) — если нет роли менеджера."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.type != "private" or not message.text:
            return False
        if message.text.strip() not in pulse_model.CHEF_MENU_BUTTONS:
            return False
        data = await load_data()
        uid = message.from_user.id
        if is_global_admin(uid) or pulse_model.has_manager_access(data, uid):
            return False
        return pulse_model.has_chef_access(data, uid)


async def manager_ui_for_user(user_id: int) -> bool:
    data = await load_data()
    return is_global_admin(user_id) or pulse_model.has_manager_access(data, user_id)


async def chef_ui_for_user(user_id: int) -> bool:
    data = await load_data()
    return is_global_admin(user_id) or pulse_model.has_chef_access(data, user_id)


def is_global_admin(user_id: int) -> bool:
    """Доступ: точное совпадение id или совпадение по модулю (на случай разного знака в .env)."""
    if user_id in ADMIN_IDS:
        return True
    if abs(user_id) in ADMINS_BY_ABS:
        return True
    return False


def _manager_menu_markup(uid: int):
    ga = is_global_admin(uid)
    show_ai_audit = ga
    happiness_only = False
    try:
        if DATA_PATH.exists():
            data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
            if not ga:
                happiness_only = pulse_model.is_happiness_manager_only(data, uid)
            show_ai_audit = ga or pulse_model.has_ai_auditor_access(data, uid)
    except Exception:
        pass
    return pulse_model.manager_menu_reply_markup(
        show_inbox=ga,
        show_commands=ga,
        show_ai_audit=show_ai_audit,
        happiness_only=happiness_only,
    )


def _chat_managers_only(data: dict, chat_id: int) -> set[int]:
    """Получатели операционных DM — только менеджеры точки, без глобального админа."""
    return set(problems_pulse.managers_for_chat(data, chat_id))


async def load_data() -> dict:
    async with DATA_LOCK:
        if not DATA_PATH.exists():
            data = pulse_model.default_data()
            DATA_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return data
        try:
            data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = pulse_model.default_data()
        if pulse_model.migrate_in_place(data):
            DATA_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return data


async def _resolve_telegram_username(username: str) -> int | None:
    """@username → telegram id через Bot API getChat."""
    un = (username or "").strip().lstrip("@")
    if not un:
        return None
    try:
        chat = await bot.get_chat(f"@{un}")
        return int(chat.id)
    except Exception as e:
        print(f"[resolve-username] @{un}: {e}")
        return None


async def save_data(data: dict) -> None:
    async with DATA_LOCK:
        DATA_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _split_admin_messages(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        chunk = line + "\n"
        if size + len(chunk) > limit and buf:
            parts.append("".join(buf).rstrip())
            buf = []
            size = 0
        buf.append(chunk)
        size += len(chunk)
    if buf:
        parts.append("".join(buf).rstrip())
    return parts or [text[:limit]]


async def _unique_users_map(chat_ids: list[str], *, days: int | None = None) -> dict[str, int]:
    return await admin_metrics.count_unique_users_by_chat(
        chat_ids,
        jsonl_path=FEEDBACK_LOG_PATH,
        days=days,
    )


def _format_chat_tariff_line(plan: str | None, unique_users: int) -> str:
    tariff = pulse_model.tariff_display(plan) if plan else "—"
    suggested = pulse_model.suggest_tariff_for_users(unique_users)
    lines = [f"   тариф: {escape(tariff)}"]
    lines.append(f"   уникальных сотрудников: <b>{unique_users}</b>")
    if plan and pulse_model.tariff_over_limit(plan, unique_users):
        lines.append(
            f"   ⚠️ превышение лимита — рекомендуется "
            f"<code>{escape(suggested)}</code> ({escape(pulse_model.tariff_label(suggested))})"
        )
    elif not plan and unique_users > 0:
        lines.append(
            f"   💡 рекомендуемый тариф: <code>{escape(suggested)}</code> "
            f"({escape(pulse_model.tariff_label(suggested))})"
        )
    elif plan and plan != suggested and unique_users > 0:
        lines.append(
            f"   💡 по числу людей логичнее <code>{escape(suggested)}</code> "
            f"({escape(pulse_model.tariff_label(suggested))})"
        )
    return "\n".join(lines)


async def _notify_admins_tariff_over(
    *,
    chat_id: str,
    title: str,
    plan: str,
    unique_users: int,
    org_name: str | None = None,
    org_id: str | None = None,
) -> None:
    suggested = pulse_model.suggest_tariff_for_users(unique_users)
    mx = pulse_model.tariff_max_users(plan)
    org_line = ""
    if org_name and org_id:
        org_line = (
            f"\nОрганизация: <b>{escape(str(org_name))}</b> "
            f"(<code>{escape(str(org_id))}</code>)"
        )
    text = (
        "⚠️ <b>Точка вышла за лимит тарифа</b>\n\n"
        f"<b>{escape(str(title))}</b>\n"
        f"<code>{escape(str(chat_id))}</code>"
        f"{org_line}\n\n"
        f"Тариф: <code>{escape(plan)}</code> ({escape(pulse_model.tariff_label(plan))})\n"
        f"Лимит: <b>{mx}</b> · сейчас уникальных: <b>{unique_users}</b>\n\n"
        f"Рекомендуется: <code>{escape(suggested)}</code> "
        f"({escape(pulse_model.tariff_label(suggested))})\n\n"
        f"<code>/set_tariff {escape(str(chat_id))} {escape(suggested)}</code>"
    )
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
        except Exception as e:
            print(f"[tariff-alert] admin={aid} chat={chat_id}: {e}")


async def _check_tariff_limit_for_chat(chat_id: int) -> None:
    """После нового ответа сотрудника — проверка лимита тарифа и алерт админам."""
    try:
        cid = str(chat_id)
        data = await load_data()
        rec = data.get("chats", {}).get(cid)
        if not isinstance(rec, dict):
            return
        if rec.get("removed_at") or rec.get("active") is False:
            return
        plan = pulse_model.chat_tariff(data, cid)
        if not plan or plan in (pulse_model.TARIFF_ENTERPRISE, pulse_model.TARIFF_CUSTOM):
            return
        users_map = await _unique_users_map([cid])
        n_users = users_map.get(cid, 0)
        if not pulse_model.tariff_over_limit(plan, n_users):
            if pulse_model.get_tariff_over_alert(rec):
                pulse_model.clear_tariff_over_alert(rec)
                await save_data(data)
            return
        if not pulse_model.should_notify_tariff_over(rec, plan, n_users):
            return
        title = rec.get("title", cid)
        pulse_model.record_tariff_over_alert(rec, plan, n_users)
        await save_data(data)
        oid = rec.get("organization_id")
        org_name = None
        if oid:
            org = data.get("organizations", {}).get(str(oid), {})
            org_name = org.get("name") if isinstance(org, dict) else None
        await _notify_admins_tariff_over(
            chat_id=cid,
            title=str(title),
            plan=plan,
            unique_users=n_users,
            org_name=str(org_name) if org_name else None,
            org_id=str(oid) if oid else None,
        )
    except Exception as e:
        print(f"[tariff-limit-check] chat={chat_id}", repr(e))


async def log_feedback_event(entry: dict) -> None:
    """Событие для аналитики: user_id + ресторан (чат) + тип. Сбой записи не должен блокировать опрос."""
    rest_chat = entry.get("restaurant_chat_id")
    try:
        row = {
            **entry,
            "ts": datetime.now(get_tz()).isoformat(timespec="seconds"),
        }
        line = json.dumps(row, ensure_ascii=False) + "\n"
        async with LOG_LOCK:
            with FEEDBACK_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line)
        await db_pulse.insert_feedback_event(row)
    except Exception as e:
        print("[log_feedback_event]", repr(e))
        return
    uid = entry.get("user_id")
    if rest_chat is not None and uid is not None:
        try:
            data = await load_data()
            training_materials.record_staff_link(data, int(uid), int(rest_chat))
            await save_data(data)
        except (TypeError, ValueError) as e:
            print("[staff-link]", repr(e))
        except Exception as e:
            print("[staff-link]", repr(e))
    if rest_chat is not None and uid is not None:
        try:
            asyncio.create_task(_check_tariff_limit_for_chat(int(rest_chat)))
        except (TypeError, ValueError):
            pass
    if rest_chat is not None:
        try:
            et = str(entry.get("event") or "")
            asyncio.create_task(
                _maybe_manager_alerts_after_event(int(rest_chat), et)
            )
        except (TypeError, ValueError):
            pass


def chat_record(data: dict, chat_id: int) -> dict | None:
    return data.get("chats", {}).get(str(chat_id))


def org_id_for_restaurant_chat(data: dict, chat_id: int | None) -> str | None:
    if chat_id is None:
        return None
    return pulse_model.chat_organization_id(data, chat_id)


def encode_start_chat(chat_id: int) -> str:
    """Параметр /start для ссылки из группы (A-Za-z0-9_-)."""
    b = base64.urlsafe_b64encode(str(chat_id).encode()).decode().rstrip("=")
    return f"c{b}"


def decode_start_chat(token: str) -> int | None:
    if not token.startswith("c") or len(token) < 2:
        return None
    tail = token[1:]
    pad = (4 - len(tail) % 4) % 4
    try:
        raw = base64.urlsafe_b64decode(tail + "=" * pad).decode()
        return int(raw)
    except Exception:
        return None


def encode_start_more(chat_id: int) -> str:
    """Дожим после плохих оценок с кассы: в личке сразу текст, без повторных звёзд."""
    b = base64.urlsafe_b64encode(str(chat_id).encode()).decode().rstrip("=")
    return f"m{b}"


def decode_start_more(token: str) -> int | None:
    if not token.startswith("m") or len(token) < 2:
        return None
    tail = token[1:]
    pad = (4 - len(tail) % 4) % 4
    try:
        raw = base64.urlsafe_b64decode(tail + "=" * pad).decode()
        return int(raw)
    except Exception:
        return None


# user_id -> id группы, для которой сейчас проходит опрос в личке
user_linked_chat: dict[int, int] = {}
# департамент опроса (floor/kitchen) из чата группы
user_linked_department: dict[int, str] = {}
# slug из /start <slug> (для старых ссылок без привязки к чату)
user_private_slug: dict[int, str] = {}
# ждём текст комментария в личке (после «Опишите подробнее»)
waiting_for_comment: set[int] = set()
# голосовое: расшифровка ждёт подтверждения перед сохранением
pending_voice_comment: dict[int, str] = {}
# выбранная тема проблемы до «отправить так» / уточнения текстом
user_pending_problem: dict[int, str] = {}
# последняя тема смены — привязка комментария к кнопке для алертов менеджеру
user_last_problem_code: dict[int, str] = {}
# последняя эмоция опроса (charged/normal/meh/heavy) — для второго вопроса
user_last_mood: dict[int, str] = {}
# выбранная точка для отчёта (chat_id или "all")
user_report_pick: dict[int, str] = {}
# зал / кухня / весь ресторан
user_report_dept: dict[int, str] = {}
# точка для опроса шефа (без ссылки из группы)
user_chef_survey_chat: dict[int, int] = {}
waiting_chef_survey_comment: dict[int, int] = {}
waiting_chef_button_edit: dict[int, tuple[str, int, str]] = {}
manager_chef_buttons_panel: dict[int, int] = {}
waiting_ops_broadcast: dict[int, int] = {}
user_report_export_ctx: dict[int, dict] = {}
waiting_checklist_add: dict[int, tuple[str, int]] = {}
waiting_checklist_remark: dict[int, dict] = {}
manager_checklist_panel: dict[int, int] = {}
# точка для раздела «Проблемы»
manager_problem_chat: dict[int, int] = {}
# ожидаем комментарий к смене статуса проблемы: user_id -> (problem_id, status)
manager_problem_pending: dict[int, tuple[str, str]] = {}
# настройка быстрых кнопок: (режим add|edit, chat_id, code)
waiting_button_edit: dict[int, tuple[str, int, str]] = {}
# ожидаем название своей темы для «Горящих вопросов»
waiting_signal_title: dict[int, int] = {}
# message_id панели настройки кнопок (редактируем одно сообщение)
manager_buttons_panel: dict[int, int] = {}
# операционный день: утро / вечер / уход кадра / назначение задач
waiting_ops_morning: dict[int, dict] = {}
waiting_ops_evening: dict[int, dict] = {}
waiting_chef_stop: dict[int, dict] = {}
waiting_monthly_plan: dict[int, dict] = {}
waiting_staff_out: dict[int, int] = {}
waiting_task_assign: dict[int, dict] = {}
waiting_staff_assign: dict[int, dict] = {}
waiting_staff_remove: dict[int, dict] = {}
waiting_training_folder: dict[int, int] = {}
waiting_training_upload: dict[int, dict] = {}
waiting_evening_backdate: dict[int, dict] = {}
ops_pick_chat: dict[int, str] = {}  # uid -> prefix for location pick
waiting_reminder_custom: dict[int, dict] = {}  # uid -> {chat_id, slot}


async def _staff_assign_more_markup(uid: int):
    data = await load_data()
    ga = is_global_admin(uid)
    happiness_only = (not ga) and pulse_model.is_happiness_manager_only(data, uid)
    show = (not happiness_only) and staff_assign.can_manage_staff(
        data, uid, is_global_admin=ga
    )
    return pulse_model.manager_menu_more_markup(
        show_staff_assign=show, happiness_only=happiness_only
    )


async def _show_staff_remove_menu(message: Message, uid: int) -> None:
    data = await load_data()
    ga = is_global_admin(uid)
    if not staff_assign.can_manage_staff(data, uid, is_global_admin=ga):
        await message.answer("Недостаточно прав.")
        return
    locs = staff_assign.assignable_locations(data, uid, is_global_admin=ga)
    nets = staff_assign.assignable_network_orgs(data, uid, is_global_admin=ga)
    if not locs and not nets:
        await message.answer(
            "Нет точек для управления доступом.",
            reply_markup=await _staff_assign_more_markup(uid),
        )
        return
    await message.answer(
        staff_assign.format_remove_start_message(),
        parse_mode="HTML",
        reply_markup=staff_assign.locations_keyboard(locs, network_orgs=nets, mode="remove"),
    )


async def _show_training_manager_menu(message: Message, uid: int, chat_id: int) -> None:
    data = await load_data()
    if not await _manager_can_access_chat(data, uid, chat_id):
        await message.answer("Нет доступа.")
        return
    rec = chat_record(data, chat_id) or {}
    title = str(rec.get("title", chat_id))
    text = onboarding_reels.enrich_manager_menu_text(
        training_materials.format_manager_menu(rec, title)
    )
    kb = onboarding_reels.patch_manager_menu_keyboard(
        training_materials.manager_menu_keyboard(chat_id, rec),
        chat_id,
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _show_reminders_panel(message: Message, uid: int, chat_id: int) -> None:
    data = await load_data()
    if not await _manager_can_access_chat(data, uid, chat_id):
        await message.answer("Нет доступа.")
        return
    rec = chat_record(data, chat_id) or {}
    title = str(rec.get("title", chat_id))
    await message.answer(
        reminders_ui.format_panel(rec, title),
        parse_mode="HTML",
        reply_markup=reminders_ui.panel_keyboard(chat_id),
    )


async def _show_training_staff_menu(message: Message, uid: int) -> None:
    data = await load_data()
    chat_id = training_materials.staff_chat_id(data, uid)
    if chat_id is None:
        await message.answer(
            "Материалы доступны после того, как вы хотя бы раз "
            "отметите смену в боте."
        )
        return
    rec = chat_record(data, chat_id) or {}
    title = str(rec.get("title", chat_id))
    kb = training_materials.staff_folders_keyboard(chat_id, rec)
    await message.answer(
        training_materials.format_staff_menu(rec, title),
        parse_mode="HTML",
        reply_markup=kb,
    )


def _parse_backdate_text(text: str, tz_name: str) -> str | None:
    raw = text.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(raw, fmt).date()
            return d.isoformat()
        except ValueError:
            continue
    return None


async def _show_staff_assign_menu(message: Message, uid: int) -> None:
    data = await load_data()
    ga = is_global_admin(uid)
    if not staff_assign.can_manage_staff(data, uid, is_global_admin=ga):
        await message.answer("Нет доступа.")
        return
    locs = staff_assign.assignable_locations(data, uid, is_global_admin=ga)
    nets = staff_assign.assignable_network_orgs(data, uid, is_global_admin=ga)
    if not locs and not nets:
        await message.answer(
            "Нет точек для назначения. Сначала привяжите чат к организации.",
            reply_markup=await _staff_assign_more_markup(uid),
        )
        return
    await message.answer(
        staff_assign.format_start_message(),
        parse_mode="HTML",
        reply_markup=staff_assign.locations_keyboard(locs, network_orgs=nets),
    )


async def _apply_staff_assign_from_state(
    message: Message, uid: int, target_uid: int
) -> bool:
    """True если обработано."""
    state = waiting_staff_assign.pop(uid, None)
    if not state:
        return False
    data = await load_data()
    ga = is_global_admin(uid)
    org_id = str(state["org_id"])
    role = str(state["role"])
    chat_id = state.get("chat_id")
    if chat_id is not None:
        chat_id = int(chat_id)
    ok, err = staff_assign.validate_assignment(
        data,
        uid,
        is_global_admin=ga,
        target_uid=target_uid,
        org_id=org_id,
        role=role,
        chat_id=chat_id,
    )
    if not ok:
        waiting_staff_assign[uid] = state
        await message.answer(
            err or "Не удалось назначить.",
            reply_markup=await _staff_assign_more_markup(uid),
        )
        return True
    staff_assign.apply_assignment(
        data,
        target_uid=target_uid,
        org_id=org_id,
        role=role,
        chat_id=chat_id,
    )
    await save_data(data)
    role_label = pulse_model.role_label_ru(role)
    if role == pulse_model.ROLE_NETWORK_ADMIN:
        org = data.get("organizations", {}).get(org_id, {})
        place = str(org.get("name", org_id))
    else:
        rec = chat_record(data, int(chat_id)) or {}
        place = str(rec.get("title", chat_id))
    await message.answer(
        staff_assign.format_success(target_uid, role_label, place),
        parse_mode="HTML",
        reply_markup=await _staff_assign_more_markup(uid),
    )
    return True

PRIVATE_WELCOME = """Привет! 👋

«Состояние смены» — бот и Mini App для ресторанной команды.

В боте линейка оставляет отзыв о смене (анонимно для чата):
• оценка 1–10
• что мешало и что было хорошего
• атмосфера и идеи

В Mini App — обучение, тесты, отчёты управам и ИИ-аудит владельцу.
Опрос о смене по-прежнему только здесь, в боте (кнопка из группы).

Политика конфиденциальности: https://www.pulseteam.online/privacy.html"""

BOT_SHORT_DESCRIPTION = (
    "Состояние смены: Mini App для команды и управов. "
    "Отзыв линейки о смене — в боте."
)

BOT_DESCRIPTION = (
    "«Состояние смены» — цифровой контур Академии счастья.\n\n"
    "Mini App\n"
    "· Линейка: тесты и обучение\n"
    "· Управляющие: отчёты, горящие вопросы, материалы, доступы\n"
    "· Владелец: ИИ-аудит, сводка по точкам, оплаты\n\n"
    "Бот\n"
    "· Оценка и отзыв о смене — только здесь, из группы «в личку»\n"
    "· Напоминания, стоп-лист, операционный день\n\n"
    "Сайт: https://www.pulseteam.online/sostoyanie/\n"
    "Конфиденциальность: https://www.pulseteam.online/privacy.html"
)

# Личка: /start без ссылки из чата — коротко, без длинного приветствия
PRIVATE_START_NO_LINK = (
    "Чтобы оценить смену, зайдите в групповой чат ресторана и нажмите там "
    "«Рассказать в личке»."
)

# Сообщение в группу при подключении бота — «как раньше»: тёплое, по-человечески
GROUP_JOIN_WELCOME = (
    "Привет! 👋 Рады быть в этом чате.\n\n"
    "Этот бот помогает делать смены комфортнее и улучшать рабочие процессы в ресторане. "
    "Для нас <b>каждый такой чат — отдельная точка</b> (свой «ресторан» в системе).\n\n"
    "Здесь можно <b>анонимно</b> делиться впечатлением от смены, проблемами, атмосферой и идеями — "
    "обратная связь помогает быстрее замечать сложности и беречь команду ❤️\n\n"
    "<b>Как это устроено:</b> мы будем присылать короткие напоминания с <b>кнопкой в личку</b> — "
    "оценка и текст идут <b>только в диалоге с ботом</b>, в этом чате никто не увидит ни звёздочек, ни ваших слов.\n\n"
    "<b>Для администраторов чата:</b>\n"
            "• /settime 22:00 — первое напоминание\n"
            "• /settime2 14:00 — второе напоминание (опционально)\n"
            "• /times — расписание\n"
            "• /deltime / /deltime2 — убрать время\n"
    "• /timezone Europe/Moscow — часовой пояс\n"
    "• /send или /send_now — отправить напоминание сейчас\n"
    "• /link_org org_xxxx — привязать чат к организации (зал или кухня)\n"
    "• /smena_help — краткая справка по командам"
)

# Короткие напоминания в группу (без «pulse-check» и длинных пояснений)
REMINDER_BTN_TEXT = "Рассказать в личке ❤️"


def _chat_tz_name(data: dict, chat_id: int) -> str:
    rec = chat_record(data, chat_id) or {}
    return str(rec.get("timezone") or DEFAULT_TZ)


def _ops_evening_day(data: dict, chat_id: int) -> str:
    rec = chat_record(data, chat_id) or {}
    sched = ops_day.ops_schedule(rec)
    return ops_day.shift_day_for_evening(_chat_tz_name(data, chat_id), sched["morning_post"])


async def _ops_nudge_managers(
    data: dict,
    chat_id: int,
    *,
    kind: str,
    deadline_hm: str,
) -> None:
    title = str((chat_record(data, chat_id) or {}).get("title", chat_id))
    if kind == "morning":
        text = (
            f"⏰ <b>План дня</b> — {escape(title)}\n\n"
            f"До публикации в чат осталось несколько минут (в <b>{escape(deadline_hm)}</b>).\n"
            "Откройте бота → «План дня»: выручка, число на смене, расстановка."
        )
    else:
        text = (
            f"⏰ <b>Закрытие дня</b> — {escape(title)}\n\n"
            f"До итогов в чат осталось несколько минут (в <b>{escape(deadline_hm)}</b>).\n"
            "Откройте бота → «Закрытие дня»: выручка и комментарий."
        )
    for mid in _chat_managers_only(data, chat_id):
        try:
            await bot.send_message(mid, text, parse_mode="HTML")
        except Exception as e:
            print(f"[ops-nudge] {kind} chat={chat_id} mid={mid}: {e}")


async def _ops_nudge_chef_survey(
    data: dict,
    chat_id: int,
    *,
    kind: str,
) -> None:
    """Напоминание шефам оценить смену кухни в личке."""
    title = str((chat_record(data, chat_id) or {}).get("title", chat_id))
    if kind == "morning":
        text = (
            f"🌅 <b>Оцените смену</b> — {escape(title)}\n\n"
            "Как прошло утро на кухне? Откройте бота → "
            f"«{escape(pulse_model.BTN_RATE_SHIFT)}»."
        )
    else:
        text = (
            f"🌙 <b>Оцените смену</b> — {escape(title)}\n\n"
            "Как прошёл вечер на кухне? Откройте бота → "
            f"«{escape(pulse_model.BTN_RATE_SHIFT)}»."
        )
    for mid in problems_pulse.chefs_for_chat(data, chat_id):
        try:
            await bot.send_message(mid, text, parse_mode="HTML")
        except Exception as e:
            print(f"[chef-survey-nudge] chat={chat_id} mid={mid}: {e}")


async def _ops_nudge_chef_stop(
    data: dict,
    chat_id: int,
    *,
    deadline_hm: str,
    overdue: bool = False,
) -> None:
    title = str((chat_record(data, chat_id) or {}).get("title", chat_id))
    if overdue:
        text = (
            f"⚠️ <b>Стоп-лист</b> — {escape(title)}\n\n"
            f"Дедлайн <b>{escape(deadline_hm)}</b> прошёл — выложите стоп-лист в бот.\n"
            "«Стоп-лист» → текст → сразу уйдёт в группу."
        )
    else:
        text = (
            f"⏰ <b>Стоп-лист</b> — {escape(title)}\n\n"
            f"До <b>{escape(deadline_hm)}</b> осталось несколько минут.\n"
            "Откройте бота → «Стоп-лист»."
        )
    for mid in problems_pulse.ops_staff_for_chat(data, chat_id):
        try:
            await bot.send_message(mid, text, parse_mode="HTML")
        except Exception as e:
            print(f"[ops-chef-stop-nudge] chat={chat_id} mid={mid}: {e}")


def _is_evening_reminder_hour(hour: int) -> bool:
    """17:00–04:59 — про сегодняшний день; 05:00–16:59 — про вчера."""
    return hour >= 17 or hour < 5


def group_reminder_text(data: dict, chat_id: int, *, at: datetime | None = None) -> str:
    tz = get_tz(_chat_tz_name(data, chat_id))
    now = at or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    if _is_evening_reminder_hour(now.hour):
        return "✨ <b>Расскажи о своём рабочем дне</b>"
    return "✨ <b>Расскажи, как прошёл вчерашний день</b>"


def private_rating_prompt(data: dict, chat_id: int | None) -> str:
    return shift_survey.mood_prompt()

PROBLEM_LABELS = {
    "team": "команда",
    "kitchen": "кухня",
    "guests": "гости",
    "processes": "процессы",
    "self": "моё состояние",
    "ok": "ничего не мешало",
    "conflict": "конфликт / напряжение",
    "staff": "нехватка персонала",
    "management": "плохая организация",
    "stress": "сильная нагрузка",
    "comment": "свой комментарий",
}

PERSONAL_LABELS = report_pulse.PERSONAL_FACTOR_LABELS


# Совместимость со старыми сообщениями в чатах (звёзды)
rating_keyboard = shift_survey.mood_keyboard()

final_comment_keyboard = shift_survey.comment_keyboard()

problem_keyboard = shift_survey.blocker_keyboard()


def restaurant_label_for_log(data: dict, user_id: int) -> str:
    cid = user_linked_chat.get(user_id)
    if cid is not None:
        rec = chat_record(data, cid)
        if rec:
            return rec.get("title") or str(cid)
        return str(cid)
    slug = user_private_slug.get(user_id) or data.get("private_slugs", {}).get(str(user_id))
    return slug or "неизвестно"


def survey_department_for_user(data: dict, user_id: int) -> str:
    """Департамент текущего опроса: из сессии или из карточки чата."""
    cached = user_linked_department.get(user_id)
    if cached in (pulse_model.CHAT_DEPT_FLOOR, pulse_model.CHAT_DEPT_KITCHEN):
        return cached
    cid = user_linked_chat.get(user_id)
    return pulse_model.chat_department(data, cid)


def bind_survey_chat(uid: int, chat_id: int, data: dict) -> str:
    """Привязать опрос к чату и запомнить зал/кухню."""
    user_linked_chat[uid] = chat_id
    dept = pulse_model.chat_department(data, chat_id)
    user_linked_department[uid] = dept
    return dept


def link_org_department_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🍽 Чат зала",
                    callback_data="linkdept:floor",
                ),
                InlineKeyboardButton(
                    text="👨‍🍳 Чат кухни",
                    callback_data="linkdept:kitchen",
                ),
            ]
        ]
    )


def voice_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Отправить",
                    callback_data="voice_c:ok",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Записать снова",
                    callback_data="voice_c:retry",
                ),
                InlineKeyboardButton(
                    text="Пропустить",
                    callback_data="voice_c:skip",
                ),
            ],
        ]
    )


def finish_private_flow(user_id: int) -> None:
    user_linked_chat.pop(user_id, None)
    user_linked_department.pop(user_id, None)
    user_private_slug.pop(user_id, None)
    user_pending_problem.pop(user_id, None)
    user_last_problem_code.pop(user_id, None)
    user_last_mood.pop(user_id, None)
    waiting_for_comment.discard(user_id)
    pending_voice_comment.pop(user_id, None)


async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("creator", "administrator")
    except Exception:
        return False


def build_private_shift_url(chat_id: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start={encode_start_chat(chat_id)}"


def shift_link_markup(chat_id: int, bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=REMINDER_BTN_TEXT,
                    url=build_private_shift_url(chat_id, bot_username),
                )
            ]
        ]
    )


def build_private_more_url(chat_id: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start={encode_start_more(chat_id)}"


def more_link_markup(chat_id: int, bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=REMINDER_BTN_TEXT,
                    url=build_private_more_url(chat_id, bot_username),
                )
            ]
        ]
    )


def parse_hhmm(s: str) -> str | None:
    s = s.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", s):
        return None
    h, m = s.split(":")
    hi, mi = int(h), int(m)
    if not (0 <= hi <= 23 and 0 <= mi <= 59):
        return None
    return f"{hi:02d}:{mi:02d}"


def _put_reminder_time(rec: dict, index: int, t: str) -> None:
    arr = list(rec.get("auto_times") or [])
    while len(arr) <= index:
        arr.append("")
    arr[index] = t
    while arr and not arr[-1]:
        arr.pop()
    rec["auto_times"] = arr


def _pop_reminder_slot(rec: dict, index: int) -> None:
    arr = [x for x in (rec.get("auto_times") or []) if x]
    if index < len(arr):
        arr.pop(index)
    rec["auto_times"] = arr


async def _manager_can_access_chat(data: dict, uid: int, chat_id: int) -> bool:
    if is_global_admin(uid):
        return True
    allowed = pulse_model.allowed_chat_ids_for_manager(data, uid)
    return str(chat_id) in allowed


async def _ops_can_access_chat(data: dict, uid: int, chat_id: int) -> bool:
    if is_global_admin(uid):
        return True
    allowed = pulse_model.allowed_chat_ids_for_ops_user(data, uid)
    return str(chat_id) in allowed


async def _resolve_manager_problem_chat(
    data: dict, uid: int, *, pick: str | None = None
) -> int | None:
    scope = report_pulse.chat_scope_for_user(
        data, uid, is_global_admin=is_global_admin(uid)
    )
    if not scope:
        return None
    if pick:
        if len(scope) == 1:
            return int(scope[0][0])
        if any(str(cid) == pick for cid, _ in scope):
            return int(pick)
    if uid in manager_problem_chat:
        cid = manager_problem_chat[uid]
        if any(str(c) == str(cid) for c, _ in scope):
            return cid
    if len(scope) == 1:
        return int(scope[0][0])
    return None


async def _pr_chat_id_from_parts(
    data: dict, uid: int, segments: list[str], action: str
) -> int | None:
    """Точка из callback_data (l/arch/add/sync) или из сессии."""
    if action in ("l", "arch", "add", "sync") and segments:
        try:
            cid = int(segments[0])
            if await _manager_can_access_chat(data, uid, cid):
                return cid
        except ValueError:
            pass
    return await _resolve_manager_problem_chat(data, uid)


async def _upsert_hot_problem_row(
    data: dict,
    *,
    chat_id: int,
    org_id: str | None,
    problem_key: str,
    title: str,
    count: int,
    now,
    threshold: int = 3,
) -> problems_pulse.ProblemRow | None:
    """Создать/обновить горящую тему (Postgres или bot_data) и вернуть row."""
    try:
        pool = db_pulse.pool()
        if pool:
            row_d, _created = await db_pulse.upsert_problem(
                restaurant_chat_id=chat_id,
                organization_id=org_id,
                problem_key=problem_key,
                title=title,
                mentions_count=count,
                now=now,
                threshold=threshold,
                mentions_since_close=0,
            )
            return problems_pulse._row_from_dict(row_d)
        row, _created = await problems_pulse._upsert_local(
            data,
            chat_id=chat_id,
            org_id=org_id,
            problem_key=problem_key,
            title=title,
            count=count,
            now=now,
            threshold=threshold,
        )
        return row
    except Exception as e:
        print(f"[upsert-hot] chat={chat_id} key={problem_key}: {e}")
        return None


async def _notify_new_hot_problems(
    data: dict,
    chat_id: int,
    *,
    restaurant_title: str,
    changes: list,
) -> int:
    """Пуш менеджеру сразу, когда sync создал новую горящую тему."""
    new_rows = [row for row, created in changes if created]
    if not new_rows:
        return 0
    managers = _chat_managers_only(data, chat_id) or set(ADMIN_IDS)
    if not managers:
        print(f"[new-hot] chat={chat_id}: no managers/ADMIN_IDS")
        return 0
    sent_map = data.setdefault("last_auto_sent", {})
    sent = 0
    for row in new_rows:
        key = f"{chat_id}|new_hot|{row.id}"
        if sent_map.get(key):
            continue
        html = problems_pulse.format_new_hot_problem_push(
            row, restaurant_title=restaurant_title
        )
        kb = manager_alerts.alert_keyboard(chat_id, row.id)
        delivered = False
        for mid in managers:
            manager_problem_chat[mid] = chat_id
            try:
                await bot.send_message(
                    mid, html, parse_mode="HTML", reply_markup=kb
                )
                sent += 1
                delivered = True
            except Exception as e:
                print(f"[new-hot] mid={mid} chat={chat_id}: {e}")
        if delivered:
            sent_map[key] = True
            print(f"[new-hot] chat={chat_id} problem={row.id} title={row.title!r}")
    return sent


def _theme_code_from_problem_key(problem_key: str) -> str:
    key = (problem_key or "").strip()
    if key.startswith("ai_"):
        key = key[3:]
    if key.startswith("manual_"):
        return "comment_trend"
    return key or "comment_trend"


async def _deliver_advice_pack(
    mid: int,
    pack: ai_advisor.AdvicePack,
) -> int:
    """Совет + отдельное сообщение с кнопкой «Подробнее» (без ссылок в тексте совета)."""
    sent = 0
    try:
        await bot.send_message(
            mid,
            ai_advisor.format_advice_html(pack),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        sent += 1
    except Exception as e:
        print(f"[ai-deliver] mid={mid}: {e}")
        return sent
    if pack.learn:
        token = ai_advisor.store_learn_more(pack.learn)
        try:
            await bot.send_message(
                mid,
                ai_advisor.teaser_learn_more_text(),
                reply_markup=ai_advisor.learn_more_teaser_keyboard(token),
            )
            sent += 1
        except Exception as e:
            print(f"[ai-learn-teaser] mid={mid}: {e}")
    return sent


async def _send_advisor_for_problem(
    *,
    target_message: Message,
    data: dict,
    prob: problems_pulse.ProblemRow,
    uid: int,
) -> None:
    """OpenAI-совет строго по выбранной горящей теме (не по «последней»)."""
    chat_id = prob.restaurant_chat_id
    rec = chat_record(data, chat_id) or {}
    title = str(rec.get("title") or chat_id)
    tz_name = rec.get("timezone", DEFAULT_TZ)
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    window = timedelta(hours=manager_alerts.ALERT_WINDOW_HOURS)
    cur_events = await report_pulse.load_events(
        [chat_id], now - window, now, jsonl_path=FEEDBACK_LOG_PATH
    )
    theme = _theme_code_from_problem_key(prob.problem_key)
    # Только события этой темы — иначе совет «уплывает» на кухню и т.п.
    scoped = ai_advisor.filter_events_for_theme(
        cur_events, theme, problem_key=prob.problem_key
    )
    comments = ai_advisor.extract_comments(scoped, limit=8)
    # Если текстовых цитат нет — опираемся на отметки кнопки этой темы
    button_n = sum(
        1
        for e in scoped
        if e.event_type == report_pulse.EVENT_PROBLEM
        and (e.problem_code or "") in {theme, prob.problem_key}
    )
    if not comments and button_n <= 0 and prob.mentions_count <= 0:
        await target_message.answer(
            f"По теме «{escape(prob.title)}» пока мало отзывов для разбора.\n"
            "Когда линия отметит эту кнопку или напишет комментарий — "
            "наставник разберёт именно её.",
            parse_mode="HTML",
        )
        return
    alert = manager_alerts.ManagerAlert(
        kind="comment_trend",
        code=theme if theme in ai_advisor.PROBLEM_RU else "comment_trend",
        title=prob.title,
        body_lines=[
            f"Выбрана тема «Горящих»: <b>{escape(prob.title)}</b>.",
            f"Отметок по теме: <b>{max(prob.mentions_count, button_n)}</b>.",
            "Разбирай ТОЛЬКО эту тему, не соседние.",
        ],
        recommendation=manager_alerts.RECOMMENDATIONS.get(
            theme, manager_alerts.RECOMMENDATIONS["rating_drop"]
        ),
        comments=comments[:4],
        priority=1,
        problem_key=theme or prob.problem_key,
    )
    if ai_advisor._client_or_none() is None:
        await target_message.answer(
            "🤖 Наставник недоступен: в Railway нет <code>OPENAI_API_KEY</code>.\n"
            "Добавьте ключ и сделайте Redeploy.",
            parse_mode="HTML",
        )
        return
    wait = await target_message.answer(
        f"🤖 Наставник разбирает тему «{escape(prob.title)}»…"
    )
    try:
        advice = await ai_advisor.build_advice(
            alert,
            restaurant_title=title,
            events=scoped,  # не все события точки
            lock_theme=True,
        )
    except Exception as e:
        print(f"[pr-advisor] uid={uid} pid={prob.id}: {e}")
        advice = None
    try:
        await wait.delete()
    except Exception:
        pass
    if not advice:
        await target_message.answer(
            "Не удалось получить совет по этой теме. "
            "Проверьте ключ OpenAI и логи Railway."
        )
        return
    await target_message.answer(
        ai_advisor.format_advice_html(advice),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    if advice.learn:
        token = ai_advisor.store_learn_more(advice.learn)
        await target_message.answer(
            ai_advisor.teaser_learn_more_text(),
            reply_markup=ai_advisor.learn_more_teaser_keyboard(token),
        )


async def _notify_managers_problem_report(
    data: dict,
    chat_id: int,
    text: str,
    *,
    exclude_uid: int | None = None,
    problem_rows: list | None = None,
    with_problems_keyboard: bool = False,
    archive_count: int = 0,
) -> None:
    admins = _chat_managers_only(data, chat_id)
    kb = None
    if with_problems_keyboard and problem_rows is not None:
        kb = problems_pulse.problems_list_keyboard(
            problem_rows, archive_count=archive_count, chat_id=chat_id
        )
    for mid in admins:
        if exclude_uid and mid == exclude_uid:
            continue
        if with_problems_keyboard:
            manager_problem_chat[mid] = chat_id
        try:
            await bot.send_message(
                mid, text, parse_mode="HTML", reply_markup=kb
            )
        except Exception as e:
            print(f"[notify-manager] {mid}: {e}")


async def _post_problem_to_group(chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        print(f"[problem-group-post] {chat_id}: {e}")


async def _run_weekly_problems_for_chat(
    data: dict, cid: str, info: dict
) -> None:
    if info.get("removed_at") or info.get("active") is False:
        return
    chat_id = int(cid)
    org_id = info.get("organization_id")
    tz_name = info.get("timezone", DEFAULT_TZ)
    changes = await problems_pulse.sync_problems_from_period(
        data,
        chat_id,
        org_id,
        jsonl_path=FEEDBACK_LOG_PATH,
        tz_name=tz_name,
        days=problems_pulse.SIGNALS_SYNC_DAYS,
    )
    await save_data(data)
    title = info.get("title", str(chat_id))
    rows_all = await problems_pulse.list_problems_for_chat(
        data, chat_id, view=problems_pulse.VIEW_ALL
    )
    active_rows = await problems_pulse.list_problems_for_chat(data, chat_id)
    notes = []
    for row, created in changes:
        if created:
            notes.append(f"Новая: {row.title} ({row.mentions_count})")
        else:
            notes.append(f"Обновлено: {row.title} ({row.mentions_count})")
    digest = problems_pulse.format_weekly_digest(rows_all)
    await _post_problem_to_group(chat_id, digest)
    mgr_text = problems_pulse.format_manager_problems_report(
        str(title), active_rows, sync_notes=notes or None
    )
    archive_n = len(
        await problems_pulse.list_problems_for_chat(
            data, chat_id, view=problems_pulse.VIEW_ARCHIVE
        )
    )
    await _notify_managers_problem_report(
        data,
        chat_id,
        mgr_text,
        problem_rows=active_rows,
        with_problems_keyboard=True,
        archive_count=archive_n,
    )
    await _run_monthly_report_for_chat(data, cid, info)


async def _notify_network_stale_problems(data: dict, *, week_key: str) -> None:
    """Понедельник: управляющим сети — точки, где проблемы висят без «в работе»."""
    orgs: set[str] = set()
    for info in (data.get("chats") or {}).values():
        if not isinstance(info, dict) or info.get("removed_at") or info.get("active") is False:
            continue
        oid = info.get("organization_id")
        if oid:
            orgs.add(str(oid))
    sent_map = data.setdefault("last_auto_sent", {})
    for oid in orgs:
        nkey = f"netstale|{oid}|{week_key}"
        if sent_map.get(nkey):
            continue
        text = await problems_pulse.format_network_stale_problems_digest(data, oid)
        if not text:
            continue
        recipients = set(pulse_model.network_admin_ids_for_org(data, oid)) | set(ADMIN_IDS)
        for mid in recipients:
            try:
                await bot.send_message(mid, text, parse_mode="HTML")
            except Exception as e:
                print(f"[net-stale] org={oid} mid={mid}: {e}")
        sent_map[nkey] = True
        print(f"[net-stale] org={oid} recipients={len(recipients)}")


async def _run_manager_alerts_for_chat(
    data: dict,
    cid: str,
    info: dict,
    *,
    force: bool = False,
) -> int:
    """Push-алерты менеджерам, когда порог уже пробит. force=True — без cooldown (админ)."""
    if info.get("removed_at") or info.get("active") is False:
        print(f"[manager-alerts] chat={cid}: skip inactive/removed")
        return 0
    oid = info.get("organization_id")
    if oid and pulse_model.is_org_billing_blocked(data, oid):
        print(f"[manager-alerts] chat={cid}: billing blocked org={oid}")
        return 0
    chat_id = int(cid)
    tz_name = info.get("timezone", DEFAULT_TZ)
    packed = await manager_alerts.build_alerts_for_chat(
        data,
        chat_id,
        jsonl_path=FEEDBACK_LOG_PATH,
        tz_name=tz_name,
    )
    if not packed:
        print(f"[manager-alerts] chat={cid}: no alerts detected (порог/данные)")
        return 0
    managers = _chat_managers_only(data, chat_id) or set(ADMIN_IDS)
    if not managers:
        print(
            f"[manager-alerts] chat={cid}: нет менеджеров и ADMIN_IDS"
        )
        return 0
    sent_map = data.setdefault("last_auto_sent", {})
    now = datetime.now(get_tz(tz_name))
    title = str(info.get("title") or chat_id)
    # События для AI — те же 72ч, что у алертов
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name) if tz_name else get_tz(DEFAULT_TZ)
    cur_start = now - timedelta(hours=manager_alerts.ALERT_WINDOW_HOURS)
    cur_events = await report_pulse.load_events(
        [chat_id], cur_start, now, jsonl_path=FEEDBACK_LOG_PATH
    )
    sent = 0
    for alert, html, kb in packed:
        key = manager_alerts.alert_dedupe_key(chat_id, alert.kind, alert.code)
        if not force and manager_alerts.alert_recently_sent(
            sent_map, key, now=now
        ):
            print(f"[manager-alerts] chat={cid}: cooldown {key}")
            continue
        delivered = False
        for mid in managers:
            manager_problem_chat[mid] = chat_id
            try:
                await bot.send_message(
                    mid, html, parse_mode="HTML", reply_markup=kb
                )
                sent += 1
                delivered = True
            except Exception as e:
                print(f"[manager-alert] mid={mid} chat={cid}: {e}")
        if delivered:
            manager_alerts.mark_alert_sent(sent_map, key, now=now)
            print(
                f"[manager-alerts] chat={cid}: sent kind={alert.kind} "
                f"code={alert.code} to {len(managers)} mgr"
            )
            # AI-наставник: строго по теме алерта
            try:
                scoped = cur_events
                if alert.code and alert.code not in (
                    "comment_trend",
                    "other",
                    "rating_drop",
                    "improved",
                ):
                    scoped = ai_advisor.filter_events_for_theme(
                        cur_events, alert.code, problem_key=alert.problem_key
                    ) or cur_events
                advice = await ai_advisor.build_advice(
                    alert,
                    restaurant_title=title,
                    events=scoped,
                    lock_theme=True,
                )
                if advice:
                    for mid in managers:
                        await _deliver_advice_pack(mid, advice)
            except Exception as e:
                print(f"[ai-advisor] build failed: {e}")
    return sent


async def _maybe_manager_alerts_after_event(chat_id: int, event_type: str) -> None:
    """Сразу после сигнала линии — новая горящая тема, порог алертов, тренды."""
    if event_type not in manager_alerts.TRIGGER_EVENTS:
        return
    try:
        data = await load_data()
        info = chat_record(data, chat_id)
        if not info:
            print(f"[manager-alerts-trigger] chat={chat_id}: no chat record")
            return
        title = str(info.get("title") or chat_id)
        # Сразу синхронизируем пороги кнопок → пуш при НОВОЙ теме
        try:
            changes = await problems_pulse.sync_problems_from_period(
                data,
                chat_id,
                info.get("organization_id"),
                jsonl_path=FEEDBACK_LOG_PATH,
                tz_name=info.get("timezone", DEFAULT_TZ),
                days=problems_pulse.SIGNALS_SYNC_DAYS,
            )
            n_new = await _notify_new_hot_problems(
                data,
                chat_id,
                restaurant_title=title,
                changes=changes,
            )
        except Exception as e:
            print(f"[new-hot-sync] chat={chat_id}: {e}")
            n_new = 0
        n = n_new + await _run_manager_alerts_for_chat(data, str(chat_id), info)
        # Комментарий или кнопка «что мешало» — повторы по всем темам
        if event_type in (report_pulse.EVENT_COMMENT, report_pulse.EVENT_PROBLEM):
            n += await _run_comment_trend_mentor(data, str(chat_id), info)
        if n:
            await save_data(data)
            print(f"[manager-alerts-trigger] chat={chat_id} event={event_type} msgs={n}")
        else:
            print(
                f"[manager-alerts-trigger] chat={chat_id} event={event_type} msgs=0"
            )
    except Exception as e:
        print(f"[manager-alerts-trigger-fail] {chat_id}: {e}")


async def _run_comment_trend_mentor(
    data: dict, cid: str, info: dict, *, force: bool = False
) -> int:
    """
    Кнопки + тексты (≥3) → горящий вопрос.
    Совет наставника — только OpenAI по реальным комментариям (без шаблонов).
    Если менеджеры не привязаны — шлём ADMIN_IDS.
    """
    if info.get("removed_at") or info.get("active") is False:
        return 0
    oid = info.get("organization_id")
    if oid and pulse_model.is_org_billing_blocked(data, oid):
        return 0
    chat_id = int(cid)
    tz_name = info.get("timezone", DEFAULT_TZ)
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    window = timedelta(hours=manager_alerts.ALERT_WINDOW_HOURS)
    cur_events = await report_pulse.load_events(
        [chat_id], now - window, now, jsonl_path=FEEDBACK_LOG_PATH
    )
    prev_events = await report_pulse.load_events(
        [chat_id], now - window * 2, now - window, jsonl_path=FEEDBACK_LOG_PATH
    )
    if not cur_events:
        return 0

    title = str(info.get("title") or cid)
    try:
        trends = await ai_advisor.detect_comment_trends(cur_events)
    except Exception as e:
        print(f"[ai-comment-trend] detect {cid}: {e}")
        return 0
    if not trends:
        return 0
    alert = ai_advisor.trend_to_alert(trends[0])

    sent_map = data.setdefault("last_auto_sent", {})
    trend_key = manager_alerts.alert_dedupe_key(
        chat_id, "comment_trend", (alert.title or "")[:40]
    )
    if not force and manager_alerts.alert_recently_sent(
        sent_map, trend_key, now=now, cooldown_hours=24
    ):
        print(f"[ai-comment-trend] cooldown {trend_key}")
        return 0

    problem_row = None
    try:
        pkey = f"ai_{(alert.problem_key or 'trend')}"[:48]
        # Для кнопки «команда» и т.п. — та же тема, что в списке горящих
        theme = _theme_code_from_problem_key(alert.problem_key or "")
        if theme in ("team", "kitchen", "guests", "processes", "self"):
            pkey = theme
        problem_row = await _upsert_hot_problem_row(
            data,
            chat_id=chat_id,
            org_id=str(oid) if oid else None,
            problem_key=pkey,
            title=(alert.title or "Тенденция в отзывах")[:120],
            count=max(3, int(getattr(alert, "priority", 1) and 3)),
            now=now,
            threshold=3,
        )
    except Exception as e:
        print(f"[ai-comment-trend-upsert] {cid}: {e}")

    managers = _chat_managers_only(data, chat_id) or set(ADMIN_IDS)
    if not managers:
        print(f"[ai-comment-trend] no managers and no ADMIN_IDS chat={cid}")
        return 0

    html_alert = manager_alerts.format_alert_message(alert, restaurant_title=title)
    kb = manager_alerts.alert_keyboard(
        chat_id, problem_row.id if problem_row else None
    )
    sent = 0
    for mid in managers:
        manager_problem_chat[mid] = chat_id
        try:
            await bot.send_message(
                mid, html_alert, parse_mode="HTML", reply_markup=kb
            )
            sent += 1
        except Exception as e:
            print(f"[ai-comment-trend] mid={mid}: {e}")

    # Совет — OpenAI только по теме тренда
    advice = None
    try:
        scoped = cur_events
        if alert.code and alert.code not in ("comment_trend", "other"):
            scoped = ai_advisor.filter_events_for_theme(
                cur_events, alert.code, problem_key=alert.problem_key
            ) or cur_events
        advice = await ai_advisor.build_advice(
            alert,
            restaurant_title=title,
            events=scoped,
            lock_theme=True,
        )
    except Exception as e:
        print(f"[ai-comment-trend] build_advice {cid}: {e}")
    if advice:
        for mid in managers:
            sent += await _deliver_advice_pack(mid, advice)
    elif ai_advisor._client_or_none() is None:
        note = (
            "🤖 Наставник не написал совет: в Railway нет <code>OPENAI_API_KEY</code>.\n"
            "Тренд уже в «Горящих вопросах». Добавьте ключ и Redeploy — "
            "тогда AI будет разбирать комментарии персонала."
        )
        for mid in set(ADMIN_IDS) | managers:
            try:
                await bot.send_message(mid, note, parse_mode="HTML")
                sent += 1
            except Exception as e:
                print(f"[ai-comment-trend] no-key note mid={mid}: {e}")
            break  # один раз достаточно

    if sent:
        manager_alerts.mark_alert_sent(sent_map, trend_key, now=now)
        print(
            f"[ai-comment-trend] chat={cid} sent={sent} "
            f"advice={'yes' if advice else 'no'}"
        )
    return sent


async def _run_weekly_ai_advice_for_chat(
    data: dict, cid: str, info: dict
) -> None:
    """Воскресенье 20:00 — OpenAI читает неделю по комментариям (не шаблон)."""
    if ai_advisor._client_or_none() is None:
        print(f"[ai-weekly] skip chat={cid}: no OPENAI_API_KEY")
        return
    if info.get("removed_at") or info.get("active") is False:
        return
    oid = info.get("organization_id")
    if oid and pulse_model.is_org_billing_blocked(data, oid):
        return
    chat_id = int(cid)
    tz_name = info.get("timezone", DEFAULT_TZ)
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    window = timedelta(days=7)
    cur_events = await report_pulse.load_events(
        [chat_id], now - window, now, jsonl_path=FEEDBACK_LOG_PATH
    )
    prev_events = await report_pulse.load_events(
        [chat_id], now - window * 2, now - window, jsonl_path=FEEDBACK_LOG_PATH
    )
    if not cur_events:
        return
    title = str(info.get("title") or cid)
    alert = None
    try:
        alert, advice = await ai_advisor.mentor_pack_for_events(
            cur_events,
            prev_events,
            restaurant_title=title,
            data=data,
            chat_id=chat_id,
        )
        if not advice:
            advice = await ai_advisor.build_advice_from_events(
                cur_events,
                prev_events,
                restaurant_title=title,
                data=data,
                chat_id=chat_id,
            )
    except Exception as e:
        print(f"[ai-weekly] build_advice failed chat={cid}: {e}")
        return
    if not advice:
        return
    if alert and alert.kind == "comment_trend":
        try:
            pkey = f"ai_{(alert.problem_key or 'trend')}"[:48]
            await problems_pulse._upsert_local(
                data,
                chat_id=chat_id,
                org_id=str(oid) if oid else None,
                problem_key=pkey,
                title=(alert.title or "Тенденция недели")[:120],
                count=5,
                now=now,
                threshold=3,
            )
            await save_data(data)
        except Exception as e:
            print(f"[ai-weekly-upsert] {cid}: {e}")
    managers = _chat_managers_only(data, chat_id) or set(ADMIN_IDS)
    for mid in managers:
        await _deliver_advice_pack(mid, advice)


async def _show_problems_for_manager(
    message: Message,
    uid: int,
    chat_id: int,
) -> None:
    data = await load_data()
    manager_problem_chat[uid] = chat_id
    survey_buttons.get_buttons(data, chat_id)
    rec = chat_record(data, chat_id) or {}
    sync_note: str | None = None
    try:
        changes = await problems_pulse.sync_problems_from_period(
            data,
            chat_id,
            rec.get("organization_id"),
            jsonl_path=FEEDBACK_LOG_PATH,
            tz_name=rec.get("timezone", DEFAULT_TZ),
            days=problems_pulse.SIGNALS_SYNC_DAYS,
        )
        # Если тема только что появилась при открытии списка — пуш менеджерам
        await _notify_new_hot_problems(
            data,
            chat_id,
            restaurant_title=str(rec.get("title") or chat_id),
            changes=changes,
        )
        await save_data(data)
    except Exception as e:
        print(f"[signals-sync] chat={chat_id} err={e!r}")
        sync_note = "Не удалось обновить из отзывов — показан сохранённый список."
    rows = await problems_pulse.list_problems_for_chat(data, chat_id)
    archive = await problems_pulse.list_problems_for_chat(
        data, chat_id, view=problems_pulse.VIEW_ARCHIVE
    )
    title = rec.get("title", str(chat_id))
    text = problems_pulse.format_problem_list(
        rows,
        title=pulse_model.BTN_SIGNALS,
        chat_title=str(title),
        sync_days=problems_pulse.SIGNALS_SYNC_DAYS,
        archive_hint=bool(archive),
    )
    if sync_note:
        text += f"\n\n<i>{escape(sync_note)}</i>"
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=problems_pulse.problems_list_keyboard(
            rows, archive_count=len(archive), chat_id=chat_id
        ),
    )


async def _show_signals_archive(
    message: Message,
    uid: int,
    chat_id: int,
) -> None:
    data = await load_data()
    manager_problem_chat[uid] = chat_id
    rows = await problems_pulse.list_problems_for_chat(
        data, chat_id, view=problems_pulse.VIEW_ARCHIVE
    )
    rec = chat_record(data, chat_id) or {}
    title = rec.get("title", str(chat_id))
    text = problems_pulse.format_archive_list(rows, chat_title=str(title))
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=problems_pulse.problems_archive_keyboard(rows, chat_id=chat_id),
    )


async def _refresh_buttons_panel(
    *,
    uid: int,
    chat_id: int,
    edit_target: Message | None = None,
    reply_target: Message | None = None,
) -> None:
    """Одна панель настроек: правим текст сообщения, без простыни в чате."""
    data = await load_data()
    if not await _manager_can_access_chat(data, uid, chat_id):
        if reply_target:
            await reply_target.answer("Нет доступа к этой точке.")
        return
    manager_problem_chat[uid] = chat_id
    survey_buttons.get_buttons(data, chat_id)
    await save_data(data)
    rec = chat_record(data, chat_id)
    title = rec.get("title", str(chat_id)) if rec else str(chat_id)
    text = survey_buttons.format_config_message(data, chat_id, str(title))
    kb = survey_buttons.config_keyboard(data, chat_id)

    if edit_target is not None:
        try:
            await edit_target.edit_text(text, parse_mode="HTML", reply_markup=kb)
            manager_buttons_panel[uid] = edit_target.message_id
            return
        except Exception:
            pass

    anchor = edit_target or reply_target
    if anchor is None:
        return
    private_chat_id = anchor.chat.id
    panel_id = manager_buttons_panel.get(uid)
    if panel_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=private_chat_id,
                message_id=panel_id,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
        except Exception:
            pass

    msg = await anchor.answer(text, parse_mode="HTML", reply_markup=kb)
    manager_buttons_panel[uid] = msg.message_id


async def _show_buttons_config(message: Message, uid: int, chat_id: int) -> None:
    await _refresh_buttons_panel(uid=uid, chat_id=chat_id, reply_target=message)


async def _refresh_chef_buttons_panel(
    *,
    uid: int,
    chat_id: int,
    edit_target: Message | None = None,
    reply_target: Message | None = None,
) -> None:
    data = await load_data()
    if not await _manager_can_access_chat(data, uid, chat_id):
        if reply_target:
            await reply_target.answer("Нет доступа к этой точке.")
        return
    manager_problem_chat[uid] = chat_id
    chef_survey.get_buttons(data, chat_id)
    await save_data(data)
    rec = chat_record(data, chat_id)
    title = rec.get("title", str(chat_id)) if rec else str(chat_id)
    text = chef_survey.format_config_message(data, chat_id, str(title))
    kb = chef_survey.config_keyboard(data, chat_id)

    if edit_target is not None:
        try:
            await edit_target.edit_text(text, parse_mode="HTML", reply_markup=kb)
            manager_chef_buttons_panel[uid] = edit_target.message_id
            return
        except Exception:
            pass

    anchor = edit_target or reply_target
    if anchor is None:
        return
    private_chat_id = anchor.chat.id
    panel_id = manager_chef_buttons_panel.get(uid)
    if panel_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=private_chat_id,
                message_id=panel_id,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return
        except Exception:
            pass
    msg = await anchor.answer(text, parse_mode="HTML", reply_markup=kb)
    manager_chef_buttons_panel[uid] = msg.message_id


async def _run_monthly_report_for_chat(data: dict, cid: str, info: dict) -> None:
    """Автоотчёт за скользящие 3 недели — менеджерам точки в личку."""
    if info.get("removed_at") or info.get("active") is False:
        return
    oid = info.get("organization_id")
    if oid and pulse_model.is_org_billing_blocked(data, oid):
        return
    chat_id = int(cid)
    tz_name = info.get("timezone", DEFAULT_TZ)
    title = str(info.get("title", chat_id))
    managers = _chat_managers_only(data, chat_id)
    if not managers:
        return

    intro = (
        f"📬 <b>Отчёт за 3 недели</b> — {escape(title)}\n"
        f"<i>Автоматически · последние {report_pulse.MONTH_REPORT_DAYS} дн.</i>\n\n"
    )
    for mid in managers:
        result = await report_pulse.build_reports_for_manager(
            data,
            mid,
            report_pulse.PERIOD_MONTH,
            is_global_admin=is_global_admin(mid),
            tz_name=tz_name,
            jsonl_path=FEEDBACK_LOG_PATH,
            selected_chat=cid,
        )
        if not result.parts:
            continue
        try:
            await bot.send_message(
                mid,
                intro + result.parts[0],
                parse_mode="HTML",
            )
            for chunk in result.parts[1:]:
                await bot.send_message(mid, chunk, parse_mode="HTML")
        except Exception as e:
            print(f"[month-report] manager={mid} chat={cid}: {e}")


async def _ops_scope(data: dict, uid: int) -> list[tuple[str, str]]:
    return report_pulse.chat_scope_for_user(
        data, uid, is_global_admin=is_global_admin(uid)
    )


async def _chef_stop_scope(data: dict, uid: int) -> list[tuple[str, str]]:
    if is_global_admin(uid):
        return await _ops_scope(data, uid)
    allowed = pulse_model.allowed_chat_ids_for_ops_user(data, uid)
    chats = data.get("chats", {})
    out: list[tuple[str, str]] = []
    for cid in sorted(allowed):
        rec = chats.get(cid) if isinstance(chats, dict) else None
        if not isinstance(rec, dict):
            continue
        if rec.get("removed_at") or rec.get("active") is False:
            continue
        out.append((cid, str(rec.get("title", cid))))
    return out


async def _ops_pick_chat_or_ask(
    message: Message, uid: int, scope: list[tuple[str, str]], *, prefix: str, title: str
) -> int | None:
    if not scope:
        await message.answer("Нет привязанных точек.")
        return None
    if len(scope) == 1:
        return int(scope[0][0])
    ops_pick_chat[uid] = prefix
    await message.answer(
        f"<b>{escape(title)}</b>\n\nВыберите точку:",
        parse_mode="HTML",
        reply_markup=ops_day.morning_location_keyboard(scope, prefix),
    )
    return None


async def _user_can_ai_audit(uid: int, data: dict | None = None) -> bool:
    if is_global_admin(uid):
        return True
    data = data if data is not None else await load_data()
    return pulse_model.has_ai_auditor_access(data, uid)


def _audit_orgs_keyboard(orgs: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for oid, name in orgs[:25]:
        short = name[:40] + ("…" if len(name) > 40 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🌐 {short}",
                    callback_data=f"audit:org:{oid}"[:64],
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="Отмена", callback_data="audit:cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _begin_audit_for_org(message: Message, uid: int, org_id: str) -> None:
    data = await load_data()
    if not await _user_can_ai_audit(uid, data):
        await message.answer("ИИ-аудит доступен менеджеру по счастью и управляющему сети.")
        return
    ga = is_global_admin(uid)
    allowed = {oid for oid, _ in pulse_model.audit_orgs_for_user(data, uid, is_global_admin=ga)}
    if org_id not in allowed:
        await message.answer("Нет доступа к этой организации.")
        return
    org = pulse_model.get_organization(data, org_id) or {}
    title = str(org.get("name") or org_id)
    ai_auditor.start_session(
        uid,
        restaurant_id=org_id,
        restaurant_title=title,
        organization_id=org_id,
    )
    await message.answer(
        f"<b>🧠 ИИ-аудит</b> · {escape(title)}\n\n"
        "Пришлите <b>голосовые</b>, <b>аудио</b> или <b>файлы</b> кусками "
        "(разговор с управляющим / baseline по сети или точке).\n"
        "Группу ресторана подключать не обязательно — достаточно организации.\n"
        "Лимит одного файла ~20 МБ — длинное лучше нарезать.\n\n"
        f"Когда всё собрано — «{pulse_model.BTN_AUDIT_FINISH}».",
        parse_mode="HTML",
        reply_markup=pulse_model.audit_session_markup(),
    )


async def _offer_ai_audit(message: Message, uid: int) -> None:
    data = await load_data()
    if not await _user_can_ai_audit(uid, data):
        await message.answer(
            "ИИ-аудит — для роли «Менеджер по счастью» или управляющего сети.\n"
            "Назначьте роль: «Подключить доступ» или "
            "<code>/link_manager ID org_id happiness CHAT_ID</code>.",
            parse_mode="HTML",
            reply_markup=_manager_menu_markup(uid),
        )
        return
    orgs = pulse_model.audit_orgs_for_user(data, uid, is_global_admin=is_global_admin(uid))
    if not orgs:
        await message.answer(
            "Нет организаций для аудита.\n"
            "Создайте: <code>/create_org Название</code> — группу точки подключать не обязательно.",
            parse_mode="HTML",
            reply_markup=_manager_menu_markup(uid),
        )
        return
    if len(orgs) == 1:
        await _begin_audit_for_org(message, uid, orgs[0][0])
        return
    await message.answer(
        "<b>🧠 ИИ-аудит</b>\n\nВыберите организацию:",
        parse_mode="HTML",
        reply_markup=_audit_orgs_keyboard(orgs),
    )


async def _audit_ingest_media(message: Message, uid: int) -> bool:
    """True если сообщение обработано как часть аудита."""
    sess = ai_auditor.get_active(uid)
    if not sess:
        return False
    kind = "file"
    file_id = None
    file_unique_id = ""
    filename = "file.bin"
    mime = ""
    size = None
    if message.voice:
        kind = "voice"
        file_id = message.voice.file_id
        file_unique_id = message.voice.file_unique_id or ""
        filename = "voice.ogg"
        mime = message.voice.mime_type or "audio/ogg"
        size = message.voice.file_size
    elif message.audio:
        kind = "audio"
        file_id = message.audio.file_id
        file_unique_id = message.audio.file_unique_id or ""
        filename = message.audio.file_name or "audio.mp3"
        mime = message.audio.mime_type or ""
        size = message.audio.file_size
    elif message.video_note:
        kind = "video_note"
        file_id = message.video_note.file_id
        file_unique_id = message.video_note.file_unique_id or ""
        filename = "video_note.mp4"
        size = message.video_note.file_size
    elif message.document:
        kind = "document"
        doc = message.document
        file_id = doc.file_id
        file_unique_id = doc.file_unique_id or ""
        filename = doc.file_name or "document.bin"
        mime = doc.mime_type or ""
        size = doc.file_size
    elif message.text and message.text.strip() not in pulse_model.MANAGER_MENU_BUTTONS:
        # текстовая заметка в сессии
        text = message.text.strip()
        if not text:
            return True
        store = ai_auditor.load_store()
        active = store.get("active", {}).get(str(uid))
        if not isinstance(active, dict):
            return True
        chunks = active.setdefault("chunks", [])
        if len(chunks) >= ai_auditor.MAX_CHUNKS:
            await message.answer(
                f"Уже {ai_auditor.MAX_CHUNKS} фрагментов — завершите анализ.",
                reply_markup=pulse_model.audit_session_markup(),
            )
            return True
        chunks.append(
            {
                "kind": "text",
                "text": text[:8000],
                "filename": "note.txt",
                "added_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        store["active"][str(uid)] = active
        ai_auditor.save_store(store)
        await message.answer(
            f"✅ Текстовая заметка #{len(chunks)}. Можно ещё голос/файл или завершить.",
            reply_markup=pulse_model.audit_session_markup(),
        )
        return True
    else:
        return False
    if not file_id:
        return False
    sess2, err = ai_auditor.add_chunk(
        uid,
        kind=kind,
        file_id=file_id,
        file_unique_id=file_unique_id,
        filename=filename,
        mime=mime,
        size=size,
    )
    if err and sess2 is None:
        await message.answer(err)
        return True
    if err:
        await message.answer(err, reply_markup=pulse_model.audit_session_markup())
        return True
    n = len((sess2 or {}).get("chunks") or [])
    await message.answer(
        f"✅ Фрагмент #{n} принят ({escape(filename)}).\n"
        "Пришлите ещё или нажмите «Завершить анализ».",
        parse_mode="HTML",
        reply_markup=pulse_model.audit_session_markup(),
    )
    return True


async def _deliver_audit_report(message: Message, uid: int, record: dict) -> None:
    html = ai_auditor.tg_summary_html(record)
    pdf_path = Path(str(record.get("pdf_path") or ""))
    await message.answer(
        html,
        parse_mode="HTML",
        reply_markup=_manager_menu_markup(uid),
    )
    if pdf_path.is_file():
        try:
            await message.answer_document(
                FSInputFile(str(pdf_path), filename=f"{record.get('id', 'audit')}.pdf"),
                caption="PDF · операционное здоровье",
            )
        except Exception as e:
            print(f"[ai-auditor] send pdf to user: {e}")
            await message.answer("PDF сохранён, но не удалось отправить файл в Telegram.")

    # Всегда копия главному админу
    title = str(record.get("restaurant_title") or "")
    for aid in ADMIN_IDS:
        if aid == uid:
            continue
        try:
            await bot.send_message(
                aid,
                f"📬 Аудит от <code>{uid}</code>\n{html}",
                parse_mode="HTML",
            )
            if pdf_path.is_file():
                await bot.send_document(
                    aid,
                    FSInputFile(str(pdf_path), filename=f"{record.get('id', 'audit')}.pdf"),
                    caption=f"PDF · {title[:60]}",
                )
        except Exception as e:
            print(f"[ai-auditor] notify admin {aid}: {e}")


async def _finish_ai_audit(message: Message, uid: int) -> None:
    sess = ai_auditor.get_active(uid)
    if not sess:
        await message.answer(
            "Нет активной сессии аудита.",
            reply_markup=_manager_menu_markup(uid),
        )
        return
    await message.answer("⏳ Расшифровка и расчёт индекса здоровья… Это может занять минуту.")

    async def _download(file_id: str) -> bytes:
        f = await bot.get_file(file_id)
        buf = await bot.download_file(f.file_path)
        return buf.read()

    record, err = await ai_auditor.process_session(uid, download_bytes=_download)
    if err or not record:
        await message.answer(
            err or "Не удалось завершить аудит.",
            reply_markup=pulse_model.audit_session_markup()
            if ai_auditor.get_active(uid)
            else _manager_menu_markup(uid),
        )
        return
    await _deliver_audit_report(message, uid, record)


async def _post_morning_to_group(
    data: dict, chat_id: int, day: str
) -> tuple[str, str | None]:
    """(ok|incomplete|already_posted|send_failed, detail)"""
    rec = chat_record(data, chat_id) or {}
    if not shift_checklists.morning_gate_complete(rec, day):
        return "incomplete", "morning_checklists"
    found = ops_day.find_publishable_morning(rec, day)
    if not found:
        morning = ops_day.get_morning(rec, day)
        if ops_day.morning_complete(morning) and morning.get("posted"):
            return "already_posted", None
        return "incomplete", None
    day, morning = found
    title = str(rec.get("title", str(chat_id)))
    try:
        hc = int(morning.get("shift_headcount") or 0)
    except (TypeError, ValueError):
        hc = 0
    text = ops_day.format_morning_group_post(
        title,
        revenue_plan=ops_day._morning_revenue_plan(morning),
        shift_headcount=hc,
        chef_stop_text=ops_day.chef_stop_text_for_day(rec, day),
        roster=str(morning.get("roster", "")),
    )
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        err = str(e)
        print(f"[ops-morning-post] {chat_id}: {err}")
        return "send_failed", err
    ops_day.mark_morning_posted(rec, day)
    return "ok", None


async def _post_evening_to_group(
    data: dict, chat_id: int, day: str
) -> tuple[str, str | None]:
    """(ok|incomplete|already_posted|send_failed, detail)"""
    rec = chat_record(data, chat_id) or {}
    tz_name = str(rec.get("timezone") or DEFAULT_TZ)
    try:
        from zoneinfo import ZoneInfo

        today_d = datetime.now(ZoneInfo(tz_name)).date()
        day_d = date.fromisoformat(day)
        past_day = day_d < today_d
    except Exception:
        past_day = False
    if not past_day and not shift_checklists.shift_gate_complete(rec, day, "closing"):
        return "incomplete", "closing_checklist"
    found = ops_day.find_publishable_evening(rec, day)
    if not found:
        evening = ops_day.get_evening(rec, day)
        if ops_day.evening_complete(evening) and evening.get("posted"):
            return "already_posted", None
        return "incomplete", None
    day, evening = found
    morning = ops_day.get_morning(rec, day)
    title = str(rec.get("title", str(chat_id)))
    plan_amt = ops_day._morning_revenue_plan(morning)
    actual = ops_day.parse_amount(str(evening.get("revenue") or ""))
    pct = evening.get("revenue_pct")
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = None
    text = ops_day.format_evening_group_post(
        title,
        plan_met=str(evening.get("plan_met") or "") or None,
        revenue_plan=plan_amt,
        revenue_actual=actual,
        revenue_pct=pct,
        comment=str(evening.get("comment", "")),
        morning_posted=bool(morning.get("posted")),
    )
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        err = str(e)
        print(f"[ops-evening-post] {chat_id}: {err}")
        return "send_failed", err
    ops_day.mark_evening_posted(rec, day)
    return "ok", None


async def _post_chef_stop_to_group(
    data: dict, chat_id: int, day: str
) -> tuple[str, str | None]:
    rec = chat_record(data, chat_id) or {}
    stop = ops_day.get_chef_stop(rec, day)
    if not ops_day.chef_stop_has_content(stop):
        return "incomplete", None
    title = str(rec.get("title", str(chat_id)))
    version = int(stop.get("version") or 1)
    text = ops_day.format_chef_stop_group_post(
        title, str(stop.get("text") or ""), updated=version > 1
    )
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        err = str(e)
        print(f"[ops-chef-stop-post] {chat_id}: {err}")
        return "send_failed", err
    stop["posted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return "ok", None


async def _send_report_chunks(
    target: Message,
    uid: int,
    parts: list[str],
    *,
    reply_markup=None,
    offer_pdf: bool = False,
    export_ctx: dict | None = None,
) -> None:
    if offer_pdf and export_ctx and parts:
        user_report_export_ctx[uid] = export_ctx
        pdf_row = report_pulse.report_pdf_export_keyboard().inline_keyboard[0]
        if reply_markup is not None and getattr(reply_markup, "inline_keyboard", None):
            from aiogram.types import InlineKeyboardMarkup

            mk = InlineKeyboardMarkup(
                inline_keyboard=list(reply_markup.inline_keyboard) + [pdf_row]
            )
        else:
            mk = report_pulse.report_pdf_export_keyboard()
    else:
        mk = reply_markup if reply_markup is not None else _manager_menu_markup(uid)
    for i, chunk in enumerate(parts):
        is_last = i == len(parts) - 1
        rm = mk if is_last else None
        try:
            await target.answer(chunk, parse_mode="HTML", reply_markup=rm)
        except Exception as e:
            print(f"[report-send] uid={uid}", repr(e))
            await target.answer(chunk[:3500], reply_markup=rm)


async def _admin_digest_for_chat(data: dict, chat_id: int) -> str:
    rec = chat_record(data, chat_id) or {}
    tz_name = rec.get("timezone", DEFAULT_TZ)
    day = _ops_evening_day(data, chat_id)
    eng = await ops_day.engagement_line_for_shift_day(
        data,
        chat_id,
        day,
        jsonl_path=FEEDBACK_LOG_PATH,
        tz_name=tz_name,
    )
    deps = ops_day.count_staff_departures(
        await ops_day.load_departure_events(FEEDBACK_LOG_PATH, chat_id),
        chat_id,
        days=30,
    )
    return await ops_day.build_daily_manager_digest(
        data, chat_id, day=day, engagement_line=eng, departures_30d=deps
    )


async def _send_daily_digest_to_managers(data: dict, chat_id: int, day: str) -> None:
    rec = chat_record(data, chat_id) or {}
    tz_name = rec.get("timezone", DEFAULT_TZ)
    eng = await ops_day.engagement_line_for_shift_day(
        data,
        chat_id,
        day,
        jsonl_path=FEEDBACK_LOG_PATH,
        tz_name=tz_name,
    )
    deps = ops_day.count_staff_departures(
        await ops_day.load_departure_events(FEEDBACK_LOG_PATH, chat_id),
        chat_id,
        days=30,
    )
    text = await ops_day.build_daily_manager_digest(
        data, chat_id, day=day, engagement_line=eng, departures_30d=deps
    )
    for mid in _chat_managers_only(data, chat_id):
        try:
            await bot.send_message(mid, text, parse_mode="HTML")
        except Exception as e:
            print(f"[ops-digest] {mid}: {e}")


async def _show_morning_status(
    message: Message, uid: int, chat_id: int, *, skip_opening_gate: bool = False
) -> None:
    data = await load_data()
    rec = chat_record(data, chat_id) or {}
    tz_name = rec.get("timezone", DEFAULT_TZ)
    day = ops_day.today_key(tz_name)
    title = str(rec.get("title", chat_id))
    if (
        not skip_opening_gate
        and shift_checklists.get_template_items(rec, "opening_waiters")
        and not shift_checklists.shift_gate_complete(rec, day, "opening_waiters")
    ):
        await message.answer(
            shift_checklists.format_checklist_prompt(rec, title, day, "opening_waiters"),
            parse_mode="HTML",
            reply_markup=shift_checklists.run_checklist_keyboard(
                rec, chat_id, day, "opening_waiters"
            ),
        )
        return
    morning = ops_day.get_morning(rec, day)
    sched = ops_day.ops_schedule(rec)
    if ops_day.morning_complete(morning):
        try:
            hc = int(morning.get("shift_headcount") or 0)
        except (TypeError, ValueError):
            hc = 0
        preview = ops_day.format_morning_group_post(
            str(rec.get("title", chat_id)),
            revenue_plan=ops_day._morning_revenue_plan(morning),
            shift_headcount=hc,
            chef_stop_text=ops_day.chef_stop_text_for_day(rec, day),
            roster=str(morning.get("roster", "")),
        )
        posted = "✅ опубликован в чате" if morning.get("posted") else "черновик"
        await message.answer(
            f"<b>План дня</b> · {escape(str(rec.get('title', chat_id)))}\n"
            f"Статус: {posted}\n"
            f"Автопубликация: <b>{escape(sched['morning_post'])}</b>\n\n"
            f"{preview}",
            parse_mode="HTML",
            reply_markup=ops_day.morning_actions_keyboard(
                chat_id, day, posted=bool(morning.get("posted"))
            ),
        )
    else:
        waiting_ops_morning[uid] = {
            "chat_id": chat_id,
            "step": "revenue_plan",
            "data": {},
        }
        await message.answer(
            f"<b>План дня</b> · {escape(str(rec.get('title', chat_id)))}\n\n"
            "Шаг 1/3: <b>план по выручке</b> на сегодня (число, например 850000).\n"
            f"<i>Стоп-лист — отдельно у шефа до {escape(sched['chef_stop_deadline'])}.</i>\n"
            f"<i>Автопубликация плана в чат в {escape(sched['morning_post'])}.</i>",
            parse_mode="HTML",
        )


async def _show_stop_list_status(message: Message, uid: int, chat_id: int) -> None:
    data = await load_data()
    rec = chat_record(data, chat_id) or {}
    tz_name = rec.get("timezone", DEFAULT_TZ)
    day = ops_day.today_key(tz_name)
    sched = ops_day.ops_schedule(rec)
    stop = ops_day.get_chef_stop(rec, day)
    title = escape(str(rec.get("title", chat_id)))
    deadline = escape(sched["chef_stop_deadline"])
    if ops_day.chef_stop_has_content(stop):
        body = (
            f"<b>Стоп-лист</b> · {title}\n"
            f"Дедлайн: <b>{deadline}</b>\n\n"
            f"<b>Сейчас:</b>\n{escape(str(stop.get('text') or ''))}\n\n"
            "Отправьте <b>новый текст</b> — заменит список целиком и опубликует в группу.\n"
            f"Или нажмите «{pulse_model.BTN_STOP_ADD}», чтобы дописать позиции."
        )
    else:
        body = (
            f"<b>Стоп-лист</b> · {title}\n"
            f"Дедлайн: <b>{deadline}</b>\n\n"
            "Введите стоп-лист на сегодня — сразу уйдёт в группу."
        )
    waiting_chef_stop[uid] = {"chat_id": chat_id, "day": day, "mode": "replace"}
    await message.answer(body, parse_mode="HTML")


async def _show_stop_list_append(message: Message, uid: int, chat_id: int) -> None:
    data = await load_data()
    rec = chat_record(data, chat_id) or {}
    tz_name = rec.get("timezone", DEFAULT_TZ)
    day = ops_day.today_key(tz_name)
    stop = ops_day.get_chef_stop(rec, day)
    title = escape(str(rec.get("title", chat_id)))
    current = str(stop.get("text") or "").strip() if ops_day.chef_stop_has_content(stop) else ""
    if current:
        body = (
            f"<b>Добавить в стоп-лист</b> · {title}\n\n"
            f"<b>Текущий список:</b>\n{escape(current)}\n\n"
            "Отправьте <b>новые позиции</b> — допишем к списку и опубликуем в группу."
        )
    else:
        body = (
            f"<b>Добавить в стоп-лист</b> · {title}\n\n"
            "<i>Стоп-листа пока нет — первое сообщение станет полным списком.</i>"
        )
    waiting_chef_stop[uid] = {"chat_id": chat_id, "day": day, "mode": "append"}
    await message.answer(body, parse_mode="HTML")


async def _show_stop_current(message: Message, uid: int, chat_id: int) -> None:
    data = await load_data()
    rec = chat_record(data, chat_id) or {}
    tz_name = rec.get("timezone", DEFAULT_TZ)
    day = ops_day.today_key(tz_name)
    title = escape(str(rec.get("title", chat_id)))
    stop = ops_day.get_chef_stop(rec, day)
    if ops_day.chef_stop_has_content(stop):
        body = (
            f"<b>Актуальный стоп-лист</b> · {title}\n\n"
            f"{escape(str(stop.get('text') or ''))}"
        )
    else:
        body = (
            f"<b>Актуальный стоп-лист</b> · {title}\n\n"
            "<i>На сегодня стоп-лист ещё не задан.</i>"
        )
    await message.answer(body, parse_mode="HTML")


async def _handle_menu_rate_shift(message: Message, uid: int) -> None:
    data = await load_data()
    scope = await _chef_stop_scope(data, uid)
    chat_id = await _ops_pick_chat_or_ask(
        message, uid, scope, prefix="ops:svpick", title="Оценить смену"
    )
    if chat_id is not None:
        await _show_chef_survey(message, uid, chat_id)


async def _handle_menu_stop_list(
    message: Message, uid: int, *, mode: str = "replace"
) -> None:
    data = await load_data()
    scope = await _chef_stop_scope(data, uid)
    if mode == "append":
        prefix, title = "ops:stopadd", "Добавить в стоп-лист"
    else:
        prefix, title = "ops:stopick", "Стоп-лист"
    chat_id = await _ops_pick_chat_or_ask(
        message, uid, scope, prefix=prefix, title=title
    )
    if chat_id is not None:
        if mode == "append":
            await _show_stop_list_append(message, uid, chat_id)
        else:
            await _show_stop_list_status(message, uid, chat_id)


async def _handle_menu_stop_current(message: Message, uid: int) -> None:
    data = await load_data()
    scope = await _chef_stop_scope(data, uid)
    chat_id = await _ops_pick_chat_or_ask(
        message, uid, scope, prefix="ops:stview", title="Актуальный стоп"
    )
    if chat_id is not None:
        await _show_stop_current(message, uid, chat_id)


async def _handle_menu_ops_broadcast(message: Message, uid: int) -> None:
    data = await load_data()
    scope = await _ops_scope(data, uid)
    if not scope and is_global_admin(uid):
        scope = await _chef_stop_scope(data, uid)
    chat_id = await _ops_pick_chat_or_ask(
        message, uid, scope, prefix="ops:bcpick", title="Сообщение команде"
    )
    if chat_id is not None:
        rec = chat_record(data, chat_id) or {}
        waiting_ops_broadcast[uid] = chat_id
        await message.answer(
            f"<b>📣 Шефу и менеджерам</b> · {escape(str(rec.get('title', chat_id)))}\n\n"
            "Напишите текст одним сообщением — его получат "
            "<b>менеджеры и шеф</b> этой точки в личку бота.",
            parse_mode="HTML",
        )


async def _send_ops_broadcast(
    data: dict, chat_id: int, text: str, from_uid: int
) -> tuple[int, int]:
    rec = chat_record(data, chat_id) or {}
    title = str(rec.get("title", chat_id))
    body = (
        f"📣 <b>Сообщение по точке</b> · {escape(title)}\n\n"
        f"{escape(text.strip())}"
    )
    recipients = set(problems_pulse.ops_staff_for_chat(data, chat_id))
    sent = 0
    for mid in recipients:
        try:
            await bot.send_message(mid, body, parse_mode="HTML")
            sent += 1
        except Exception as e:
            print(f"[ops-broadcast] chat={chat_id} mid={mid}: {e}")
    await log_feedback_event(
        {
            "event": "ops_broadcast",
            "user_id": from_uid,
            "restaurant_chat_id": chat_id,
            "restaurant_label": title,
            "organization_id": org_id_for_restaurant_chat(data, chat_id),
            "comment": text.strip(),
        }
    )
    return sent, len(recipients)


async def _show_chef_survey(message: Message, uid: int, chat_id: int) -> None:
    data = await load_data()
    rec = chat_record(data, chat_id) or {}
    title = str(rec.get("title", chat_id))
    user_chef_survey_chat[uid] = chat_id
    waiting_chef_survey_comment.pop(uid, None)
    chef_survey.get_buttons(data, chat_id)
    await message.answer(
        chef_survey.survey_prompt_text(title),
        parse_mode="HTML",
        reply_markup=chef_survey.build_survey_keyboard(data, chat_id),
    )


async def _show_evening_status(
    message: Message,
    uid: int,
    chat_id: int,
    *,
    skip_closing_gate: bool = False,
    day: str | None = None,
) -> None:
    data = await load_data()
    rec = chat_record(data, chat_id) or {}
    tz_name = rec.get("timezone", DEFAULT_TZ)
    if day is None:
        day = shift_checklists.day_for_kind(rec, "closing", tz_name)
    title = str(rec.get("title", chat_id))
    try:
        from zoneinfo import ZoneInfo

        today_d = datetime.now(ZoneInfo(str(tz_name))).date()
        past_day = date.fromisoformat(day) < today_d
    except Exception:
        past_day = False
    if (
        not skip_closing_gate
        and not past_day
        and shift_checklists.get_template_items(rec, "closing")
        and not shift_checklists.shift_gate_complete(rec, day, "closing")
    ):
        await message.answer(
            shift_checklists.format_checklist_prompt(rec, title, day, "closing"),
            parse_mode="HTML",
            reply_markup=shift_checklists.run_checklist_keyboard(
                rec, chat_id, day, "closing"
            ),
        )
        return
    evening = ops_day.get_evening(rec, day)
    sched = ops_day.ops_schedule(rec)
    if ops_day.evening_complete(evening):
        met = ops_day.PLAN_MET_RU.get(str(evening.get("plan_met")), "—")
        rev = ops_day.format_money(ops_day.parse_amount(str(evening.get("revenue") or "")))
        pct = evening.get("revenue_pct")
        pct_s = f" · <b>{pct}%</b>" if pct is not None else ""
        posted = "✅ в чате" if evening.get("posted") else "черновик"
        await message.answer(
            f"<b>Закрытие дня</b> · {escape(str(rec.get('title', chat_id)))}\n"
            f"{escape(met)} · {escape(rev)}{pct_s}\n"
            f"Статус: {posted}\n"
            f"Автопубликация: <b>{escape(sched['evening_post'])}</b>",
            parse_mode="HTML",
            reply_markup=ops_day.evening_actions_keyboard(
                chat_id, day, posted=bool(evening.get("posted"))
            ),
        )
    else:
        waiting_ops_evening[uid] = {
            "chat_id": chat_id,
            "step": "revenue",
            "data": {},
            "day": day,
        }
        await message.answer(
            f"<b>Закрытие дня</b> · {escape(str(rec.get('title', chat_id)))}\n\n"
            "Шаг 1/3: <b>фактическая выручка</b> за смену (число).\n"
            "<i>План выполнен или нет — посчитаем сами по утреннему плану.</i>",
            parse_mode="HTML",
        )


@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    if event.new_chat_member.user.id != event.bot.id:
        return
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return

    data = await load_data()
    cid = str(chat.id)
    chats = data.setdefault("chats", {})
    new_st = event.new_chat_member.status
    old_st = event.old_chat_member.status if event.old_chat_member else None

    if new_st in ("member", "administrator") and old_st in (None, "left", "kicked", "restricted"):
        prev = chats.get(cid, {})
        chats[cid] = {
            "title": chat.title or f"Чат {cid}",
            "type": chat.type,
            "added_at": prev.get("added_at")
            or datetime.now(get_tz()).isoformat(timespec="seconds"),
            "auto_times": prev.get("auto_times", []),
            "timezone": prev.get("timezone", DEFAULT_TZ),
            "active": True,
            # None = ждёт /link_org; при повторном входе сохраняем привязку из prev
            "organization_id": prev.get("organization_id"),
        }
        chats[cid].pop("removed_at", None)
        await save_data(data)
        try:
            await bot.send_message(
                chat.id,
                GROUP_JOIN_WELCOME,
                parse_mode="HTML",
            )
        except Exception:
            pass
        print(f"[chat+]{cid} {chats[cid]['title']}")

    if new_st in ("left", "kicked") and old_st not in (None, "left", "kicked"):
        if cid in chats:
            chats[cid]["removed_at"] = datetime.now(get_tz()).isoformat(timespec="seconds")
            chats[cid]["active"] = False
        await save_data(data)
        print(f"[chat-]{cid}")


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.chat.type in ("group", "supergroup"):
        # /start в чате = то же приветствие, что в личке, плюс кнопка в личку для этой точки
        me = await bot.get_me()
        await message.answer(PRIVATE_WELCOME)
        await message.answer(
            "Нажмите кнопку — откроется чат с ботом. Оценка и текст только там; "
            "для аналитики ответ привяжется к этому чату.",
            reply_markup=shift_link_markup(message.chat.id, me.username),
            disable_web_page_preview=True,
        )
        return
    args = message.text.split(maxsplit=1)
    uid = message.from_user.id

    if len(args) > 1:
        arg = args[1].strip()
        more_chat = decode_start_more(arg)
        if more_chat is not None:
            data = await load_data()
            bind_survey_chat(uid, more_chat, data)
            user_private_slug.pop(uid, None)
            waiting_for_comment.add(uid)
            pending_voice_comment.pop(uid, None)
            await message.answer(
                "Напишите, что случилось на смене — текстом или голосовым. "
                "В чат ресторана это не попадёт.",
                reply_markup=pulse_model.support_only_reply_markup(),
            )
            await message.answer(
                shift_survey.comment_prompt(),
                parse_mode="HTML",
                reply_markup=final_comment_keyboard,
            )
            return

        linked = decode_start_chat(arg)
        if linked is not None:
            data = await load_data()
            bind_survey_chat(uid, linked, data)
            user_private_slug.pop(uid, None)
            await message.answer(
                "Здесь можно ответить анонимно — как прошла смена?",
                reply_markup=pulse_model.support_only_reply_markup(),
            )
            await message.answer(
                private_rating_prompt(data, linked),
                reply_markup=rating_keyboard,
            )
            return

        user_private_slug[uid] = arg
        data = await load_data()
        data.setdefault("private_slugs", {})[str(uid)] = arg
        await save_data(data)
        await message.answer(
            "Здесь можно ответить анонимно — как прошла смена?",
            reply_markup=pulse_model.support_only_reply_markup(),
        )
        await message.answer(
            private_rating_prompt(data, None),
            reply_markup=rating_keyboard,
        )
        return

    if await manager_ui_for_user(uid):
        admin_hint = ""
        if is_global_admin(uid):
            admin_hint = (
                "\n\n<b>📁 Сводки</b> — отчёты по точкам по запросу.\n"
                f"<b>{pulse_model.BTN_COMMANDS}</b> — полная инструкция.\n"
                f"<b>{pulse_model.BTN_FOLDER_SHIFT}</b> — стоп-лист, оценка кухни, "
                f"«{pulse_model.BTN_OPS_BROADCAST}»."
            )
        await message.answer(
            "Пульс смен — меню ниже.\n"
            "Оценку рабочего дня по-прежнему начинайте из <b>рабочей группы</b> "
            "(кнопка «Рассказать в личке»)."
            f"{admin_hint}",
            parse_mode="HTML",
            reply_markup=_manager_menu_markup(uid),
        )
    elif await chef_ui_for_user(uid):
        await message.answer(
            "Pulse Team — всё для кухни <b>в личке</b>:\n\n"
            f"• <b>{pulse_model.BTN_RATE_SHIFT}</b> — утром и вечером придёт напоминание\n"
            f"• <b>{pulse_model.BTN_FOLDER_SHIFT_STOP}</b> — стоп-лист в группу\n"
            f"  · <b>{pulse_model.BTN_STOP_CURRENT}</b> — только посмотреть, без правки",
            parse_mode="HTML",
            reply_markup=pulse_model.chef_menu_reply_markup(),
        )
    else:
        await message.answer(
            PRIVATE_START_NO_LINK,
            reply_markup=pulse_model.support_only_reply_markup(),
        )


@dp.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    await message.answer(
        f"Ваш Telegram ID: <code>{uid}</code>\n\n"
        "Отправьте это сообщение вашему руководителю или в поддержку "
        "напрямую — с информацией о ресторане и роли.",
        parse_mode="HTML",
    )


@dp.message(Command("smena_help"))
async def cmd_help(message: Message) -> None:
    if message.chat.type in ("group", "supergroup"):
        await message.answer(
            "<b>Команды в этом чате</b>\n"
            "/settime 22:00 — первое напоминание в личку\n"
            "/settime2 14:00 — второе напоминание (опционально)\n"
            "/times — расписание\n"
            "/deltime 22:00 — убрать первое время\n"
            "/deltime2 — убрать второе время\n"
            "/timezone Europe/Moscow — часовой пояс\n"
            "/send или /send_now — напоминание в группу сейчас (со ссылкой в личку)\n"
            "/set_ops morning 11:30 chef_stop 11:45 evening 00:00 — план, стоп, закрытие\n"
            "/link_org org_xxxx — привязать чат (зал/кухня; глобальный админ или админ чата)\n"
            "/start в этом чате — ваша личная ссылка в личку для оценки <b>этой</b> точки\n\n"
            "Оценка всегда <b>в личке с ботом</b>, чтобы ответ привязался к этой точке.",
            parse_mode="HTML",
        )
        return
    if is_global_admin(message.from_user.id):
        await message.answer(
            "В личке:\n"
            "<b>/admin</b> — чаты и org\n"
            "<b>/orgs</b>, <b>/create_org</b>, <b>/link_manager</b>, <b>/link_org</b> (в группе)\n"
            "<b>/managers</b> — все подключённые менеджеры\n"
            f"«{pulse_model.BTN_STAFF_ASSIGN}» в «{pulse_model.BTN_FOLDER_MORE}» — точка → роль → ID\n"
            "<b>/del_org</b> org_id — удалить организацию (отвязать точки)\n"
            "<b>/set_subscription</b> org_id active|grace|suspended — пауза по оплате\n"
            "<b>/set_tariff</b> chat_id t10|t25|t40|enterprise — тариф точки\n"
            "<b>/metrics</b> — уникальные пользователи и тарифы по точкам\n"
            "<b>/tariff_history</b> chat_id — история смен тарифа\n"
            "<b>/send_alerts_now</b> — сразу push-алерты менеджерам по сигналам смен\n"
            "Алерт в личку, если точка превысила лимит тарифа (после нового ответа сотрудника)\n"
            "<b>/broadcast</b> — сообщение всем менеджерам об изменениях сервиса\n"
            f"Полный справочник — кнопка «{pulse_model.BTN_COMMANDS}» в главном меню.\n"
            f"У менеджеров — папки: «{pulse_model.BTN_FOLDER_ANALYTICS}», "
            f"«{pulse_model.BTN_FOLDER_SHIFT}», «{pulse_model.BTN_FOLDER_MORE}».",
            parse_mode="HTML",
        )
    elif await manager_ui_for_user(message.from_user.id):
        await message.answer(
            f"Меню: <b>{escape(pulse_model.BTN_FOLDER_ANALYTICS)}</b> · "
            f"<b>{escape(pulse_model.BTN_FOLDER_SHIFT)}</b> · "
            f"<b>{escape(pulse_model.BTN_FOLDER_MORE)}</b>.\n"
            "Оценку смены начинайте из <b>группы</b> по кнопке «в личку».",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "Если пришло напоминание из чата ресторана — откройте кнопку «в личку» там. "
            "Остальное позже добавим для менеджеров.",
        )


async def _broadcast_to_managers(text: str) -> tuple[int, int]:
    """Рассылка всем привязанным менеджерам (глобальный админ)."""
    data = await load_data()
    body = f"📣 <b>Обновление Pulse Team</b>\n\n{text}"
    targets: set[int] = set()
    for uid_s in data.get("managers", {}):
        try:
            uid = int(uid_s)
        except ValueError:
            continue
        if pulse_model.has_manager_access(data, uid):
            targets.add(uid)
    sent = failed = 0
    for mid in targets:
        try:
            await bot.send_message(mid, body, parse_mode="HTML")
            sent += 1
        except Exception as e:
            failed += 1
            print(f"[broadcast] {mid}: {e}")
    return sent, failed


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message) -> None:
    if message.chat.type != "private":
        return
    if not is_global_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    if message.reply_to_message and (message.reply_to_message.text or message.reply_to_message.caption):
        text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "<b>Рассылка менеджерам</b>\n\n"
                "<code>/broadcast</code> текст сообщения\n"
                "или ответьте <code>/broadcast</code> на нужное сообщение.\n\n"
                "Можно HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, ссылки.",
                parse_mode="HTML",
            )
            return
        text = parts[1].strip()
    if not text:
        await message.answer("Пустое сообщение.")
        return
    sent, failed = await _broadcast_to_managers(text)
    await message.answer(
        f"Готово: доставлено <b>{sent}</b>, не удалось <b>{failed}</b>.",
        parse_mode="HTML",
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if message.chat.type in ("group", "supergroup"):
        return
    uid = message.from_user.id
    if not is_global_admin(uid):
        await message.answer(
            "Нет доступа.\n\n"
            f"Ваш Telegram ID: <code>{uid}</code>\n\n"
            "Отправьте это сообщение вашему руководителю или в поддержку "
            "напрямую — с информацией о ресторане и роли.",
            parse_mode="HTML",
        )
        return
    data = await load_data()
    orgs = data.get("organizations", {})
    chats = data.get("chats", {})
    chat_ids = [cid for cid, rec in chats.items() if isinstance(rec, dict)]
    users_map = await _unique_users_map(chat_ids) if chat_ids else {}
    lines = [
        f"Организаций: {len(orgs)} · чатов в базе: {len(chats)}\n",
        "<i>Тарифы: t10 (до 10), t25 (11–25), t40 (26–40), enterprise (40+), custom</i>\n",
    ]
    for cid, info in sorted(chats.items(), key=lambda x: x[0]):
        if not isinstance(info, dict):
            continue
        title = info.get("title", cid)
        active = "✓" if info.get("active", True) and not info.get("removed_at") else "✗"
        times = ", ".join(info.get("auto_times", [])) or "—"
        tz = info.get("timezone", DEFAULT_TZ)
        oid = info.get("organization_id") or "—"
        plan = pulse_model.chat_tariff(data, cid)
        n_users = users_map.get(str(cid), 0)
        lines.append(
            f"{active} id <code>{escape(str(cid))}</code>\n"
            f"   {escape(str(title))}\n"
            f"   org: <code>{escape(str(oid))}</code>\n"
            f"   авто: {escape(times)} ({escape(str(tz))})\n"
            f"{_format_chat_tariff_line(plan, n_users)}"
        )
    text = "\n".join(lines) if len(lines) > 2 else "Пока нет подключённых групп."
    for chunk in _split_admin_messages(text):
        await message.answer(chunk, parse_mode="HTML")
    await message.answer(
        "iiko: <code>/iiko_point org_guid chat_id</code> · "
        "ответ на CSV <code>/iiko_import</code> · <code>/iiko_list</code>",
        parse_mode="HTML",
    )


@dp.message(Command("iiko_point"))
async def cmd_iiko_point(message: Message) -> None:
    if message.chat.type != "private" or not is_global_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Одна точка iiko → чат Pulse (сотрудников по одному не собираем):\n"
            "<code>/iiko_point &lt;organizationId из iiko&gt; &lt;chat_id зала&gt; [chat_id кухни]</code>\n"
            "chat_id — из /admin.",
            parse_mode="HTML",
        )
        return
    org = parts[1].strip()
    try:
        hall = int(parts[2])
        kitchen = int(parts[3]) if len(parts) > 3 else hall
    except ValueError:
        await message.answer("chat_id — целое число (часто отрицательное).")
        return
    data = await load_data()
    iiko_bridge.bind_point(
        data, iiko_org_id=org, chat_id=hall, hall_chat_id=hall, kitchen_chat_id=kitchen
    )
    await save_data(data)
    await message.answer(
        f"Точка iiko <code>{escape(org)}</code> → зал <code>{hall}</code>, кухня <code>{kitchen}</code>",
        parse_mode="HTML",
    )


@dp.message(Command("iiko_import"))
async def cmd_iiko_import(message: Message) -> None:
    if message.chat.type != "private" or not is_global_admin(message.from_user.id):
        return
    raw = ""
    reply = message.reply_to_message
    if reply and reply.document:
        try:
            f = await bot.get_file(reply.document.file_id)
            buf = await bot.download_file(f.file_path)
            raw = buf.read().decode("utf-8-sig", errors="replace")
        except Exception as e:
            await message.answer(f"Не прочитал файл: {e}")
            return
    elif reply and (reply.text or reply.caption):
        raw = (reply.text or reply.caption or "").strip()
    else:
        parts = (message.text or "").split(maxsplit=1)
        raw = parts[1].strip() if len(parts) > 1 else ""
    if not raw:
        await message.answer(
            "Людей по одному не собираем. Закрытие смены и так знает PIN на кассе.\n\n"
            "Если нужна выгрузка (зал/кухня для чатов) — iikoOffice → Персонал → "
            "Сотрудники → Excel. Файл или копипаст столбцов "
            "<code>id;фио;должность</code> (id / uuid / код).\n"
            "Ответьте на таблицу командой <code>/iiko_import</code>. Telegram-id не нужны.",
            parse_mode="HTML",
        )
        return
    data = await load_data()
    n = iiko_bridge.import_employees_csv(data, raw)
    await save_data(data)
    await message.answer(f"Загружено/обновлено строк: <b>{n}</b>", parse_mode="HTML")


@dp.message(Command("iiko_list"))
async def cmd_iiko_list(message: Message) -> None:
    if message.chat.type != "private" or not is_global_admin(message.from_user.id):
        return
    data = await load_data()
    iiko_bridge.ensure_tables(data)
    points = data.get("iiko_points") or {}
    staff = data.get("iiko_staff") or []
    if not points and not staff:
        await message.answer("Пусто. Сначала <code>/iiko_point</code>, при желании CSV.", parse_mode="HTML")
        return
    lines = ["<b>iiko точки</b>"]
    for oid, rec in points.items():
        if not isinstance(rec, dict):
            continue
        lines.append(
            f"<code>{escape(str(oid))}</code> → чат <code>{rec.get('chat_id')}</code> "
            f"(кухня <code>{rec.get('kitchen_chat_id')}</code>)"
        )
    lines.append(f"\nСотрудников в справочнике (опционально): {len(staff)}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("orgs"))
async def cmd_orgs(message: Message) -> None:
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    if not is_global_admin(uid):
        await message.answer("Команда только для глобального администратора бота.")
        return
    data = await load_data()
    orgs = data.get("organizations", {})
    chats = data.get("chats", {})
    if not orgs:
        await message.answer(
            "Организаций пока нет. Создайте: <code>/create_org Название сети</code>",
            parse_mode="HTML",
        )
        return
    lines: list[str] = ["<b>Организации</b>\n"]
    for oid, org in sorted(orgs.items(), key=lambda x: x[0]):
        name = org.get("name", oid)
        sub = org.get("subscription", pulse_model.SUB_ACTIVE)
        n = sum(
            1
            for c, rec in chats.items()
            if isinstance(rec, dict) and rec.get("organization_id") == oid and not rec.get("removed_at")
        )
        lines.append(
            f"• <code>{escape(str(oid))}</code> — <b>{escape(str(name))}</b>\n"
            f"  подписка: <code>{escape(str(sub))}</code> · активных чатов: {n}\n"
        )
    lines.append("Для удаления используйте <code>/del_org org_id</code>.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("managers"))
async def cmd_managers(message: Message) -> None:
    """Показывает всех подключенных менеджеров (только глобальный админ)."""
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    if not is_global_admin(uid):
        await message.answer("Команда только для глобального администратора бота.")
        return

    data = await load_data()
    raw = data.get("managers", {})
    if not isinstance(raw, dict) or not raw:
        await message.answer("Менеджеров пока нет.")
        return

    lines: list[str] = ["<b>Подключённые менеджеры</b>\n"]
    for uid_s in sorted(raw.keys(), key=lambda x: int(x) if str(x).lstrip("-").isdigit() else str(x)):
        try:
            mid = int(uid_s)
        except ValueError:
            continue
        profiles = pulse_model.manager_profiles(data, mid)
        if not profiles:
            continue
        for p in profiles:
            oid = p.get("organization_id")
            if not oid:
                continue
            org = data.get("organizations", {}).get(oid, {})
            org_name = org.get("name", oid)
            role = p.get("role")
            if role == pulse_model.ROLE_NETWORK_ADMIN:
                n_chats = len(pulse_model.list_chats_for_org(data, str(oid)))
                lines.append(
                    f"• <code>{escape(str(mid))}</code> — управляющий сетью "
                    f"<b>{escape(str(org_name))}</b> (<code>{escape(str(oid))}</code>) · точек: {n_chats}\n"
                )
            elif role == pulse_model.ROLE_LOCATION_ADMIN:
                locs = p.get("location_chat_ids") or []
                sample = ", ".join((locs or [])[:3])
                more = "" if len(locs) <= 3 else f" (+{len(locs)-3})"
                lines.append(
                    f"• <code>{escape(str(mid))}</code> — {pulse_model.ROLE_LABELS_RU[pulse_model.ROLE_LOCATION_ADMIN]} "
                    f"<b>{escape(str(org_name))}</b> (<code>{escape(str(oid))}</code>) · групп: {len(locs)} "
                    f"({escape(sample)}){more}\n"
                )
            elif role == pulse_model.ROLE_SENIOR_MANAGER:
                locs = p.get("location_chat_ids") or []
                sample = ", ".join((locs or [])[:3])
                more = "" if len(locs) <= 3 else f" (+{len(locs)-3})"
                lines.append(
                    f"• <code>{escape(str(mid))}</code> — {pulse_model.ROLE_LABELS_RU[pulse_model.ROLE_SENIOR_MANAGER]} "
                    f"<b>{escape(str(org_name))}</b> (<code>{escape(str(oid))}</code>) · групп: {len(locs)} "
                    f"({escape(sample)}){more}\n"
                )
            elif role == pulse_model.ROLE_CHEF:
                locs = p.get("location_chat_ids") or []
                sample = ", ".join((locs or [])[:3])
                more = "" if len(locs) <= 3 else f" (+{len(locs)-3})"
                lines.append(
                    f"• <code>{escape(str(mid))}</code> — <b>шеф</b> "
                    f"<b>{escape(str(org_name))}</b> (<code>{escape(str(oid))}</code>) · групп: {len(locs)} "
                    f"({escape(sample)}){more}\n"
                )
            elif role == pulse_model.ROLE_HAPPINESS_MANAGER:
                locs = p.get("location_chat_ids") or []
                sample = ", ".join((locs or [])[:3])
                more = "" if len(locs) <= 3 else f" (+{len(locs)-3})"
                lines.append(
                    f"• <code>{escape(str(mid))}</code> — "
                    f"{pulse_model.ROLE_LABELS_RU[pulse_model.ROLE_HAPPINESS_MANAGER]} "
                    f"<b>{escape(str(org_name))}</b> (<code>{escape(str(oid))}</code>) · групп: {len(locs)} "
                    f"({escape(sample)}){more}\n"
                )
            else:
                lines.append(f"• <code>{escape(str(mid))}</code> — org <code>{escape(str(oid))}</code> (role: {escape(str(role))})\n")

    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("del_org"))
async def cmd_del_org(message: Message) -> None:
    """Удаляет организацию и отвязывает все точки (только глобальный админ)."""
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    if not is_global_admin(uid):
        await message.answer("Команда только для глобального администратора бота.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Формат: <code>/del_org org_xxxx</code>", parse_mode="HTML")
        return

    oid = parts[1].strip()
    data = await load_data()
    orgs = data.get("organizations", {})
    if not isinstance(orgs, dict) or oid not in orgs:
        await message.answer("Нет такой организации (проверьте id в <code>/orgs</code>).", parse_mode="HTML")
        return

    chats = data.get("chats", {})
    unlinked = 0
    if isinstance(chats, dict):
        for cid, rec in list(chats.items()):
            if isinstance(rec, dict) and rec.get("organization_id") == oid:
                rec["organization_id"] = None
                rec["active"] = False
                rec["removed_at"] = datetime.now(get_tz()).isoformat(timespec="seconds")
                unlinked += 1

    managers = data.get("managers", {})
    removed_mgr_profiles = 0
    if isinstance(managers, dict):
        for mid, profiles in list(managers.items()):
            if not isinstance(profiles, list):
                continue
            new_profiles = [
                p
                for p in profiles
                if not (isinstance(p, dict) and p.get("organization_id") == oid)
            ]
            removed_mgr_profiles += len(profiles) - len(new_profiles)
            if new_profiles:
                managers[mid] = new_profiles
            else:
                managers.pop(mid, None)

    orgs.pop(oid, None)
    await save_data(data)
    await message.answer(
        f"Организация <code>{escape(oid)}</code> удалена.\n"
        f"Отвязано точек: {unlinked}.\n"
        f"Удалено менеджерских привязок: {removed_mgr_profiles}.",
        parse_mode="HTML",
    )


@dp.message(Command("create_org"))
async def cmd_create_org(message: Message) -> None:
    if message.chat.type != "private":
        return
    if not is_global_admin(message.from_user.id):
        await message.answer("Команда только для глобального администратора бота.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Например: <code>/create_org Сеть Италия</code>",
            parse_mode="HTML",
        )
        return
    name = parts[1].strip()
    data = await load_data()
    oid = pulse_model.create_organization(data, name)
    await save_data(data)
    await message.answer(
        f"Создана организация <b>{escape(name)}</b>\n<code>{escape(oid)}</code>\n\n"
        f"Можно сразу запустить <b>🧠 ИИ-аудит</b> — группу точки подключать не обязательно.\n"
        f"Для опросов смены: <code>/link_org {escape(oid)}</code> в группе зала "
        f"и отдельно в группе кухни — выберите тип чата.",
        parse_mode="HTML",
    )


@dp.message(Command("link_manager"))
async def cmd_link_manager(message: Message) -> None:
    if message.chat.type != "private":
        return
    if not is_global_admin(message.from_user.id):
        await message.answer("Команда только для глобального администратора бота.")
        return
    parts = (message.text or "").split()
    # /link_manager USER_ID ORG_ID network
    # /link_manager USER_ID ORG_ID location CHAT_ID
    # /link_manager USER_ID ORG_ID chef CHAT_ID
    # /link_manager USER_ID ORG_ID senior CHAT_ID
    # /link_manager USER_ID ORG_ID happiness CHAT_ID
    if len(parts) < 4:
        await message.answer(
            "Формат:\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; network</code> — управляющий сети\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; location &lt;chat_id&gt;</code> — менеджер точки\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; senior &lt;chat_id&gt;</code> — старший менеджер\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; happiness &lt;chat_id&gt;</code> — менеджер по счастью\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; chef &lt;chat_id&gt;</code> — шеф\n\n"
            "Узнать id: человек пишет боту <code>/myid</code>.",
            parse_mode="HTML",
        )
        return
    try:
        target_uid = int(parts[1])
    except ValueError:
        await message.answer("Первый аргумент — числовой Telegram id.")
        return
    org_id = parts[2]
    mode = parts[3].lower()
    data = await load_data()
    if org_id not in data.get("organizations", {}):
        await message.answer(
            "Нет такой организации. Сначала <code>/create_org</code> или проверьте id в <code>/orgs</code>.",
            parse_mode="HTML",
        )
        return
    if mode == "network":
        pulse_model.set_manager_binding(
            data, target_uid, org_id, pulse_model.ROLE_NETWORK_ADMIN, None
        )
    elif mode in ("location", "chef", "senior", "happiness", "happy"):
        if len(parts) < 5:
            await message.answer("Укажите chat_id группы (число, часто отрицательное).")
            return
        try:
            loc_cid = str(int(parts[4]))
        except ValueError:
            await message.answer("chat_id должен быть целым числом (id группы из /admin).")
            return
        if mode == "chef":
            role = pulse_model.ROLE_CHEF
        elif mode == "senior":
            role = pulse_model.ROLE_SENIOR_MANAGER
        elif mode in ("happiness", "happy"):
            role = pulse_model.ROLE_HAPPINESS_MANAGER
        else:
            role = pulse_model.ROLE_LOCATION_ADMIN
        pulse_model.set_manager_binding(data, target_uid, org_id, role, [loc_cid])
    else:
        await message.answer(
            "Режим: <code>network</code>, <code>location</code>, "
            "<code>senior</code>, <code>happiness</code> или <code>chef</code>.",
            parse_mode="HTML",
        )
        return
    await save_data(data)
    if mode == "chef":
        role_ru = pulse_model.role_label_ru(pulse_model.ROLE_CHEF)
    elif mode == "senior":
        role_ru = pulse_model.role_label_ru(pulse_model.ROLE_SENIOR_MANAGER)
    elif mode in ("happiness", "happy"):
        role_ru = pulse_model.role_label_ru(pulse_model.ROLE_HAPPINESS_MANAGER)
    elif mode == "location":
        role_ru = pulse_model.role_label_ru(pulse_model.ROLE_LOCATION_ADMIN)
    else:
        role_ru = pulse_model.role_label_ru(pulse_model.ROLE_NETWORK_ADMIN)
    await message.answer(
        f"Готово: <code>{target_uid}</code> → <b>{escape(role_ru)}</b> "
        f"· <code>{escape(org_id)}</code>.",
        parse_mode="HTML",
    )


@dp.message(Command("link_org"))
async def cmd_link_org(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Команду пишут в групповом чате точки.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Например: <code>/link_org org_a1b2c3d4</code>\n"
            "После привязки выберите <b>чат зала</b> или <b>чат кухни</b>.\n"
            "Или сразу: <code>/link_org org_a1b2c3d4 floor</code> / "
            "<code>kitchen</code> (зал / кухня).",
            parse_mode="HTML",
        )
        return
    uid = message.from_user.id
    data = await load_data()
    org_id = parts[1].strip()
    dept_arg = pulse_model.parse_chat_department(parts[2]) if len(parts) > 2 else None
    if len(parts) > 2 and dept_arg is None:
        await message.answer(
            "Департамент: <code>floor</code> / <code>зал</code> или "
            "<code>kitchen</code> / <code>кухня</code>.",
            parse_mode="HTML",
        )
        return
    is_tg_admin = await is_chat_admin(message.chat.id, uid)
    import miniapp_ops as _mops

    can_pulse = _mops.can_link_org_chats(
        data, uid, is_global_admin=is_global_admin(uid), org_id=org_id
    )
    if not (is_tg_admin or can_pulse):
        await message.answer(
            "Эту команду могут выполнить: админ чата Telegram, "
            "управляющий точки/сети в Pulse или глобальный админ бота."
        )
        return
    if org_id not in data.get("organizations", {}):
        await message.answer(
            "Нет такой организации. Проверьте id или создайте <code>/create_org</code>.",
            parse_mode="HTML",
        )
        return
    cid = str(message.chat.id)
    chats = data.setdefault("chats", {})
    if cid not in chats:
        chats[cid] = {
            "title": message.chat.title or cid,
            "type": message.chat.type,
            "added_at": datetime.now(get_tz()).isoformat(timespec="seconds"),
            "auto_times": [],
            "timezone": DEFAULT_TZ,
            "active": True,
        }
    chats[cid]["organization_id"] = org_id
    chats[cid]["title"] = message.chat.title or chats[cid].get("title") or cid
    if dept_arg:
        chats[cid]["department"] = dept_arg
        if dept_arg == pulse_model.CHAT_DEPT_FLOOR:
            pulse_model.ensure_chat_location_id(data, message.chat.id)
            await save_data(data)
            dept_ru = pulse_model.department_title_ru(dept_arg)
            await message.answer(
                f"Чат привязан к <code>{escape(org_id)}</code> как <b>{escape(dept_ru)}</b>.\n"
                "Сотрудники из этой группы пишут отзывы в зал. "
                "Отдельно подключите групповой чат кухни той же точки.",
                parse_mode="HTML",
            )
            return
        await save_data(data)
        await _finish_kitchen_link(message, data, org_id, message.chat.id)
        return
    await save_data(data)
    await message.answer(
        f"Чат привязан к организации <code>{escape(org_id)}</code>.\n"
        "Выберите тип чата — от этого зависит, куда попадут отзывы в отчётах:",
        parse_mode="HTML",
        reply_markup=link_org_department_keyboard(),
    )


async def _finish_kitchen_link(
    target: Message, data: dict, org_id: str, kitchen_chat_id: int
) -> None:
    """После выбора «кухня» — связать с чатом зала той же точки."""
    floors = [
        (cid, title)
        for cid, title in pulse_model.floor_chats_in_org(data, org_id)
        if cid != kitchen_chat_id
    ]
    if not floors:
        pulse_model.ensure_chat_location_id(data, kitchen_chat_id)
        await save_data(data)
        await target.answer(
            "Готово: это <b>чат кухни</b>.\n"
            "Чата зала в этой организации пока нет — когда подключете зал, "
            "привяжите кухню заново или напишите поддержке, чтобы связать точки.",
            parse_mode="HTML",
        )
        return
    if len(floors) == 1:
        peer_id, peer_title = floors[0]
        pulse_model.pair_chats_same_location(data, kitchen_chat_id, peer_id)
        await save_data(data)
        await target.answer(
            f"Готово: <b>чат кухни</b>, связан с залом «{escape(peer_title)}».\n"
            "Отзывы кухни и зала в отчётах одной точки — фильтр Зал / Кухня / Весь ресторан.",
            parse_mode="HTML",
        )
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"🍽 {title}"[:60],
                callback_data=f"linkpair:{peer_id}"[:64],
            )
        ]
        for peer_id, title in floors[:20]
    ]
    await target.answer(
        "Это <b>чат кухни</b>. К какому залу этой сети его привязать?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("linkdept:"))
async def link_org_department_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    dept = pulse_model.parse_chat_department(parts[1] if len(parts) > 1 else "")
    if dept is None:
        await callback.answer("Неизвестный тип.", show_alert=True)
        return
    uid = callback.from_user.id
    chat_id = callback.message.chat.id
    if not (is_global_admin(uid) or await is_chat_admin(chat_id, uid)):
        await callback.answer("Только админ чата или глобальный админ.", show_alert=True)
        return
    data = await load_data()
    rec = data.get("chats", {}).get(str(chat_id))
    if not isinstance(rec, dict) or not rec.get("organization_id"):
        await callback.answer("Сначала /link_org org_id", show_alert=True)
        return
    org_id = str(rec["organization_id"])
    rec["department"] = dept
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if dept == pulse_model.CHAT_DEPT_FLOOR:
        pulse_model.ensure_chat_location_id(data, chat_id)
        await save_data(data)
        await callback.message.answer(
            "Готово: это <b>чат зала</b>. "
            "Отзывы из кнопки в этой группе попадут в отчёты по залу. "
            "Подключите отдельно чат кухни той же точки.",
            parse_mode="HTML",
        )
        await callback.answer("Чат: Зал")
        return
    await save_data(data)
    await callback.answer("Чат: Кухня")
    await _finish_kitchen_link(callback.message, data, org_id, chat_id)


@dp.callback_query(F.data.startswith("linkpair:"))
async def link_kitchen_pair_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    try:
        peer_id = int(parts[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    uid = callback.from_user.id
    chat_id = callback.message.chat.id
    if not (is_global_admin(uid) or await is_chat_admin(chat_id, uid)):
        await callback.answer("Только админ чата или глобальный админ.", show_alert=True)
        return
    data = await load_data()
    rec = data.get("chats", {}).get(str(chat_id))
    peer = data.get("chats", {}).get(str(peer_id))
    if not isinstance(rec, dict) or not isinstance(peer, dict):
        await callback.answer("Чат не найден.", show_alert=True)
        return
    if rec.get("organization_id") != peer.get("organization_id"):
        await callback.answer("Разные организации.", show_alert=True)
        return
    rec["department"] = pulse_model.CHAT_DEPT_KITCHEN
    pulse_model.pair_chats_same_location(data, chat_id, peer_id)
    await save_data(data)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    peer_title = str(peer.get("title") or peer_id)
    await callback.message.answer(
        f"Готово: кухня связана с залом «{escape(peer_title)}». "
        "В отчётах одной точки — Зал / Кухня / Весь ресторан.",
        parse_mode="HTML",
    )
    await callback.answer("Связано")


@dp.message(Command("set_subscription"))
async def cmd_set_subscription(message: Message) -> None:
    if message.chat.type != "private":
        return
    if not is_global_admin(message.from_user.id):
        await message.answer("Команда только для глобального администратора бота.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Пример: <code>/set_subscription org_abcd1234 suspended</code>\n"
            "Статусы: <code>active</code>, <code>grace</code>, <code>suspended</code> "
            "(при suspended напоминания в чаты сети не уходят).",
            parse_mode="HTML",
        )
        return
    oid = parts[1].strip()
    state = parts[2].strip().lower()
    if state not in (pulse_model.SUB_ACTIVE, pulse_model.SUB_GRACE, pulse_model.SUB_SUSPENDED):
        await message.answer("Укажите один из статусов: active, grace, suspended.")
        return
    data = await load_data()
    org = data.get("organizations", {}).get(oid)
    if not org:
        await message.answer(
            "Нет такой организации. Смотрите <code>/orgs</code>.",
            parse_mode="HTML",
        )
        return
    org["subscription"] = state
    await save_data(data)
    await message.answer(
        f"Готово: <code>{escape(oid)}</code> → подписка <b>{escape(state)}</b>.",
        parse_mode="HTML",
    )


@dp.message(Command("send_alerts_now"))
async def cmd_send_alerts_now(message: Message) -> None:
    """Глобальный админ: сразу посчитать и отправить manager push-алерты."""
    if message.chat.type != "private":
        return
    if not is_global_admin(message.from_user.id):
        await message.answer("Команда только для глобального администратора бота.")
        return
    data = await load_data()
    total_msgs = 0
    chats_hit = 0
    for cid, info in list((data.get("chats") or {}).items()):
        if not isinstance(info, dict):
            continue
        try:
            n = await _run_manager_alerts_for_chat(data, cid, info, force=True)
            if n:
                chats_hit += 1
                total_msgs += n
        except Exception as e:
            print(f"[send_alerts_now] {cid}: {e}")
    await save_data(data)
    await message.answer(
        f"Алерты: точек с сигналом <b>{chats_hit}</b>, сообщений менеджерам "
        f"<b>{total_msgs}</b>.\n"
        f"В бою уходят <b>сразу</b>, когда порог пробит "
        f"(антидубль {manager_alerts.ALERT_COOLDOWN_HOURS} ч на тему).",
        parse_mode="HTML",
    )


@dp.message(Command("set_tariff"))
async def cmd_set_tariff(message: Message) -> None:
    uid = message.from_user.id
    if not is_global_admin(uid):
        await message.answer("Команда только для глобального администратора бота.")
        return
    in_group = message.chat.type in ("group", "supergroup")
    parts = (message.text or "").split(maxsplit=3)
    note: str | None = None
    if in_group:
        if len(parts) < 2:
            await message.answer(
                "В группе: <code>/set_tariff t25</code>\n"
                "Коды: <code>t10</code>, <code>t25</code>, <code>t40</code>, "
                "<code>enterprise</code>, <code>custom</code>",
                parse_mode="HTML",
            )
            return
        cid = str(message.chat.id)
        plan_raw = parts[1]
        if len(parts) >= 3:
            note = " ".join(parts[2:])
    else:
        if message.chat.type != "private":
            return
        if len(parts) < 3:
            await message.answer(
                "В личке: <code>/set_tariff chat_id t25</code>\n"
                "Пример: <code>/set_tariff -5126869718 t25</code>\n"
                "Коды: t10, t25, t40, enterprise, custom\n"
                "Алиасы: 10, 25, 40, 2990, 6990 и т.д.",
                parse_mode="HTML",
            )
            return
        cid = parts[1].strip()
        plan_raw = parts[2]
        if len(parts) >= 4:
            note = parts[3]
    plan = pulse_model.parse_tariff(plan_raw)
    if not plan:
        await message.answer(
            f"Неизвестный тариф «{escape(plan_raw)}». "
            "Используйте: t10, t25, t40, enterprise, custom.",
            parse_mode="HTML",
        )
        return
    data = await load_data()
    if cid not in data.get("chats", {}):
        await message.answer(
            f"Чат <code>{escape(cid)}</code> не найден в базе. Сначала добавьте бота в группу.",
            parse_mode="HTML",
        )
        return
    users_map = await _unique_users_map([cid])
    n_users = users_map.get(str(cid), 0)
    old, changed = pulse_model.set_chat_tariff(
        data,
        cid,
        plan,
        admin_id=uid,
        unique_users=n_users,
        note=note,
    )
    pulse_model.sync_tariff_alert_after_set(data["chats"][cid], plan, n_users)
    await save_data(data)
    title = data["chats"][cid].get("title", cid)
    if not changed:
        await message.answer(
            f"Тариф уже <code>{escape(plan)}</code> — {escape(pulse_model.tariff_label(plan))}.",
            parse_mode="HTML",
        )
        return
    old_txt = escape(old) if old else "не задан"
    warn = ""
    if pulse_model.tariff_over_limit(plan, n_users):
        sug = pulse_model.suggest_tariff_for_users(n_users)
        warn = (
            f"\n\n⚠️ На точке <b>{n_users}</b> чел. — выше лимита тарифа. "
            f"Рекомендуется <code>{escape(sug)}</code>."
        )
    await message.answer(
        f"✅ <b>{escape(str(title))}</b>\n"
        f"Тариф: {old_txt} → <code>{escape(plan)}</code> "
        f"({escape(pulse_model.tariff_label(plan))})\n"
        f"Уникальных сотрудников на момент смены: <b>{n_users}</b>{warn}",
        parse_mode="HTML",
    )


@dp.message(Command("tariff_history"))
async def cmd_tariff_history(message: Message) -> None:
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    if not is_global_admin(uid):
        await message.answer("Команда только для глобального администратора бота.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/tariff_history chat_id</code>\n"
            "Список chat_id — в <code>/admin</code>.",
            parse_mode="HTML",
        )
        return
    cid = parts[1].strip()
    data = await load_data()
    rec = data.get("chats", {}).get(cid)
    if not isinstance(rec, dict):
        await message.answer("Точка не найдена.")
        return
    title = rec.get("title", cid)
    plan = pulse_model.chat_tariff(data, cid)
    hist = pulse_model.chat_tariff_history(data, cid)
    users_map = await _unique_users_map([cid])
    n_now = users_map.get(str(cid), 0)
    lines = [
        f"<b>История тарифа</b> — {escape(str(title))}\n",
        f"Сейчас: <code>{escape(plan or '—')}</code> "
        f"({escape(pulse_model.tariff_label(plan))})\n"
        f"Уникальных сотрудников сейчас: <b>{n_now}</b>\n",
    ]
    if not hist:
        lines.append("\nСмен тарифа пока не было. Задайте: <code>/set_tariff …</code>")
    else:
        lines.append("\n<b>Журнал:</b>")
        for entry in reversed(hist[-15:]):
            lines.append(f"• {pulse_model.format_tariff_history_line(entry)}")
    for chunk in _split_admin_messages("\n".join(lines)):
        await message.answer(chunk, parse_mode="HTML")


@dp.message(Command("metrics"))
async def cmd_metrics(message: Message) -> None:
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    if not is_global_admin(uid):
        await message.answer("Команда только для глобального администратора бота.")
        return
    data = await load_data()
    chats = data.get("chats", {})
    active = [
        (cid, rec)
        for cid, rec in chats.items()
        if isinstance(rec, dict) and not rec.get("removed_at")
    ]
    if not active:
        await message.answer("Нет активных точек.")
        return
    chat_ids = [cid for cid, _ in active]
    users_all = await _unique_users_map(chat_ids, days=None)
    users_30 = await _unique_users_map(chat_ids, days=30)
    rows: list[tuple[int, str, dict]] = []
    for cid, rec in active:
        n = users_all.get(str(cid), 0)
        rows.append((n, cid, rec))
    rows.sort(key=lambda x: (-x[0], x[1]))
    lines = [
        "<b>📊 Метрики по точкам</b>\n",
        "<i>Уникальный сотрудник = хотя бы один ответ с этой точки (оценка, тема, комментарий).</i>\n",
    ]
    alerts = 0
    for n, cid, rec in rows:
        title = rec.get("title", cid)
        plan = pulse_model.chat_tariff(data, cid)
        n30 = users_30.get(str(cid), 0)
        suggested = pulse_model.suggest_tariff_for_users(n) if n > 0 else None
        block = [
            f"• <b>{escape(str(title))}</b> <code>{escape(str(cid))}</code>",
            f"  всего: <b>{n}</b> · за 30 дн.: <b>{n30}</b>",
            f"  тариф: {escape(pulse_model.tariff_display(plan) if plan else '—')}",
        ]
        if n > 0 and plan and pulse_model.tariff_over_limit(plan, n):
            alerts += 1
            block.append(
                f"  ⚠️ <b>переход?</b> {n} чел. при {escape(plan)} → "
                f"рекомендуется <code>{escape(suggested or '')}</code>"
            )
        elif n > 0 and suggested and plan != suggested:
            block.append(
                f"  💡 рекомендация: <code>{escape(suggested)}</code> "
                f"({escape(pulse_model.tariff_label(suggested))})"
            )
        elif n > 0 and not plan:
            block.append(
                f"  💡 задайте тариф: <code>/set_tariff {escape(str(cid))} {escape(suggested or 't10')}</code>"
            )
        lines.append("\n".join(block))
    if alerts:
        lines.insert(2, f"⚠️ Точек с превышением лимита тарифа: <b>{alerts}</b>\n")
    for chunk in _split_admin_messages("\n".join(lines)):
        await message.answer(chunk, parse_mode="HTML")


@dp.message(Command("settime"))
async def cmd_settime(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эту команду пишут в групповом чате ресторана.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Например: /settime 22:00")
        return
    t = parse_hhmm(parts[1])
    if not t:
        await message.answer("Нужен формат ЧЧ:ММ, например 09:30 или 22:00")
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Это могут настроить админы чата.")
        return

    data = await load_data()
    cid = str(message.chat.id)
    chats = data.setdefault("chats", {})
    if cid not in chats:
        chats[cid] = {
            "title": message.chat.title or cid,
            "type": message.chat.type,
            "added_at": datetime.now(get_tz()).isoformat(timespec="seconds"),
            "auto_times": [],
            "timezone": DEFAULT_TZ,
        }
    _put_reminder_time(chats[cid], 0, t)
    await save_data(data)
    tz_disp = escape(str(chats[cid].get("timezone", DEFAULT_TZ)))
    times = ", ".join(chats[cid].get("auto_times", []))
    await message.answer(
        f"Готово. <b>Первое</b> напоминание в <b>{escape(t)}</b> ({tz_disp}).\n"
        f"Расписание: {escape(times)}\n"
        "Второе время: <code>/settime2 14:00</code>",
        parse_mode="HTML",
    )


@dp.message(Command("settime2"))
async def cmd_settime2(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эту команду пишут в групповом чате ресторана.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Например: /settime2 14:00 — второе напоминание за день.")
        return
    t = parse_hhmm(parts[1])
    if not t:
        await message.answer("Нужен формат ЧЧ:ММ, например 14:00")
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Это могут настроить админы чата.")
        return
    data = await load_data()
    cid = str(message.chat.id)
    chats = data.setdefault("chats", {})
    if cid not in chats:
        chats[cid] = {
            "title": message.chat.title or cid,
            "type": message.chat.type,
            "added_at": datetime.now(get_tz()).isoformat(timespec="seconds"),
            "auto_times": [],
            "timezone": DEFAULT_TZ,
        }
    _put_reminder_time(chats[cid], 1, t)
    await save_data(data)
    tz_disp = escape(str(chats[cid].get("timezone", DEFAULT_TZ)))
    times = ", ".join(chats[cid].get("auto_times", []))
    await message.answer(
        f"Готово. <b>Второе</b> напоминание в <b>{escape(t)}</b> ({tz_disp}).\n"
        f"Расписание: {escape(times)}",
        parse_mode="HTML",
    )


@dp.message(Command("set_ops"))
async def cmd_set_ops(message: Message) -> None:
    """Расписание: /set_ops morning 11:30 evening 00:00 chef_stop 11:45"""
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Команду пишут в групповом чате ресторана.")
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Это могут настроить админы чата.")
        return
    parts = (message.text or "").split()
    morning: str | None = None
    evening: str | None = None
    chef_stop: str | None = None
    i = 1
    while i < len(parts):
        key = parts[i].lower()
        if key == "morning" and i + 1 < len(parts):
            morning = parse_hhmm(parts[i + 1])
            i += 2
            continue
        if key == "evening" and i + 1 < len(parts):
            evening = parse_hhmm(parts[i + 1])
            i += 2
            continue
        if key == "chef_stop" and i + 1 < len(parts):
            chef_stop = parse_hhmm(parts[i + 1])
            i += 2
            continue
        i += 1
    if not morning and not evening and not chef_stop:
        await message.answer(
            "Пример:\n<code>/set_ops morning 11:30 chef_stop 11:45 evening 00:00</code>\n"
            "Напоминания: план −5 мин, стоп-лист −5 мин, итоги −10 мин.\n"
            "Опрос смены — <code>/settime 22:40</code>.",
            parse_mode="HTML",
        )
        return
    data = await load_data()
    cid = str(message.chat.id)
    chats = data.setdefault("chats", {})
    if cid not in chats:
        chats[cid] = {
            "title": message.chat.title or cid,
            "type": message.chat.type,
            "added_at": datetime.now(get_tz()).isoformat(timespec="seconds"),
            "auto_times": [],
            "timezone": DEFAULT_TZ,
        }
    sched = chats[cid].setdefault("ops_schedule", {})
    if morning:
        sched["morning_post"] = morning
    if evening:
        sched["evening_post"] = evening
    if chef_stop:
        sched["chef_stop_deadline"] = chef_stop
    await save_data(data)
    s = ops_day.ops_schedule(chats[cid])
    await message.answer(
        f"Операционный день: стоп-лист до <b>{escape(s['chef_stop_deadline'])}</b>, "
        f"план в <b>{escape(s['morning_post'])}</b>, "
        f"итоги в <b>{escape(s['evening_post'])}</b> "
        f"({escape(str(chats[cid].get('timezone', DEFAULT_TZ)))}).",
        parse_mode="HTML",
    )


@dp.message(Command("times"))
async def cmd_times(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    data = await load_data()
    rec = chat_record(data, message.chat.id)
    if not rec:
        await message.answer("Чат ещё не в базе. Пере-добавьте бота или выполните /send_now.")
        return
    times = rec.get("auto_times", [])
    tz = rec.get("timezone", DEFAULT_TZ)
    if not times:
        times_disp = "пока не задано"
    elif len(times) == 1:
        times_disp = f"1-е: {times[0]}"
    else:
        times_disp = f"1-е: {times[0]}, 2-е: {times[1]}"
    s = ops_day.ops_schedule(rec)
    mn = ops_day.reminder_hhmm(s["morning_post"], ops_day.MORNING_REMINDER_MINUTES)
    ev = ops_day.reminder_hhmm(s["evening_post"], ops_day.EVENING_REMINDER_MINUTES)
    await message.answer(
        "Напоминания со ссылкой в личку:\n"
        f"<b>{escape(times_disp)}</b>\n"
        f"Часовой пояс: <code>{escape(str(tz))}</code>\n"
        "/settime и /settime2 — опрос смены\n\n"
        f"План дня: <b>{escape(s['morning_post'])}</b> "
        f"(напоминание {escape(mn)})\n"
        f"Итоги: <b>{escape(s['evening_post'])}</b> "
        f"(напоминание {escape(ev)})\n"
        "<code>/set_ops morning 11:30 evening 00:00</code>",
        parse_mode="HTML",
    )


@dp.message(Command("deltime"))
async def cmd_deltime(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Например: /deltime 22:00")
        return
    t = parse_hhmm(parts[1])
    if not t:
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Нужны права администратора чата.")
        return
    data = await load_data()
    cid = str(message.chat.id)
    rec = data.get("chats", {}).get(cid)
    if not rec:
        return
    arr = rec.get("auto_times", [])
    if t in arr:
        arr.remove(t)
    await save_data(data)
    await message.answer(f"Время {t} убрано из расписания.")


@dp.message(Command("deltime2"))
async def cmd_deltime2(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Нужны права администратора чата.")
        return
    data = await load_data()
    cid = str(message.chat.id)
    rec = data.get("chats", {}).get(cid)
    if not rec:
        return
    arr = rec.get("auto_times", [])
    if len(arr) < 2:
        await message.answer("Второе напоминание не задано.")
        return
    _pop_reminder_slot(rec, 1)
    await save_data(data)
    await message.answer("Второе напоминание убрано.")


@dp.message(Command("timezone"))
async def cmd_timezone(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Например: /timezone Europe/Moscow")
        return
    tz_name = parts[1].strip()
    try:
        ZoneInfo(tz_name)
    except Exception:
        if tz_name != DEFAULT_TZ:
            await message.answer("Не нашлась такая зона. Пример: Europe/Moscow")
            return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Нужны права администратора чата.")
        return
    data = await load_data()
    cid = str(message.chat.id)
    chats = data.setdefault("chats", {})
    if cid not in chats:
        chats[cid] = {
            "title": message.chat.title or cid,
            "type": message.chat.type,
            "added_at": datetime.now(get_tz()).isoformat(timespec="seconds"),
            "auto_times": [],
            "timezone": tz_name,
        }
    else:
        chats[cid]["timezone"] = tz_name
    await save_data(data)
    await message.answer(f"Часовой пояс для расписания: {tz_name}")


@dp.message(Command("send_now", "send"))
async def cmd_send_now(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        text = (
            "Напоминание с кнопкой «в личку» отправляется <b>из группового чата точки</b>:\n"
            "откройте чат ресторана и выполните там <b>/send</b> или <b>/send_now</b> "
            "(нужны права администратора чата).\n\n"
            "Оценку смены сотрудники всегда начинают <b>из группы</b> — так ответ привязывается к нужной точке."
        )
        mk = (
            pulse_model.manager_menu_reply_markup()
            if await manager_ui_for_user(message.from_user.id)
            else pulse_model.remove_reply_markup()
        )
        await message.answer(text, parse_mode="HTML", reply_markup=mk)
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Напоминание может отправить администратор чата.")
        return
    data = await load_data()
    oid = pulse_model.chat_organization_id(data, message.chat.id)
    if oid and pulse_model.is_org_billing_blocked(data, oid):
        await message.answer(
            "Подписка организации <b>приостановлена</b> — напоминания не отправляем, пока не возобновят доступ.",
            parse_mode="HTML",
        )
        return
    await post_shift_reminder_to_group(message.chat.id)


async def post_shift_reminder_to_group(chat_id: int) -> None:
    data = await load_data()
    oid = pulse_model.chat_organization_id(data, chat_id)
    if oid and pulse_model.is_org_billing_blocked(data, oid):
        print(f"[skip-suspended] chat={chat_id} org={oid}")
        return
    me = await bot.get_me()
    text = group_reminder_text(data, chat_id)
    await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=shift_link_markup(chat_id, me.username),
        disable_web_page_preview=True,
    )


async def answer_private_flow_end(message: Message, user_id: int, text: str) -> None:
    extra: dict = {}
    if await manager_ui_for_user(user_id):
        extra["reply_markup"] = pulse_model.manager_menu_reply_markup()
    else:
        data = await load_data()
        show_tr = training_materials.staff_chat_id(data, user_id) is not None
        extra["reply_markup"] = pulse_model.support_only_reply_markup(show_training=show_tr)
    await message.answer(text, **extra)


def _problem_keyboard_for_user(data: dict, user_id: int) -> InlineKeyboardMarkup:
    positive = user_last_mood.get(user_id) == "charged"
    return shift_survey.blocker_keyboard(positive=positive)


async def prompt_blocker(message: Message, *, user_id: int | None = None) -> None:
    uid = user_id if user_id is not None else (
        message.from_user.id if message.from_user else 0
    )
    positive = user_last_mood.get(uid) == "charged"
    await message.answer(
        shift_survey.blocker_prompt(positive=positive),
        reply_markup=shift_survey.blocker_keyboard(positive=positive),
    )


async def prompt_final_comment(message: Message, user_id: int) -> None:
    waiting_for_comment.add(user_id)
    await message.answer(
        shift_survey.comment_prompt(),
        parse_mode="HTML",
        reply_markup=shift_survey.comment_keyboard(),
    )


async def scheduler_loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            data = await load_data()
            today = date.today().isoformat()
            chats = data.get("chats", {})
            sent_map = data.setdefault("last_auto_sent", {})
            changed = False

            for cid, info in list(chats.items()):
                if info.get("removed_at") or info.get("active") is False:
                    continue
                oid = info.get("organization_id")
                if oid and pulse_model.is_org_billing_blocked(data, oid):
                    continue
                tz_name = info.get("timezone", DEFAULT_TZ)
                tz = get_tz(tz_name)
                hm = datetime.now(tz).strftime("%H:%M")
                for t in info.get("auto_times", []):
                    if not t or t != hm:
                        continue
                    key = f"{cid}|{t}|{today}"
                    if sent_map.get(key):
                        continue
                    try:
                        await post_shift_reminder_to_group(int(cid))
                        sent_map[key] = True
                        changed = True
                        print(f"[auto] chat={cid} time={t}")
                    except Exception as e:
                        print(f"[auto-fail] {cid}: {e}")

                sched = ops_day.ops_schedule(info)
                morning_day = ops_day.today_key(tz_name)
                evening_day = ops_day.shift_day_for_evening(tz_name, sched["morning_post"])
                morning_nudge = ops_day.reminder_hhmm(
                    sched["morning_post"], ops_day.MORNING_REMINDER_MINUTES
                )
                evening_nudge = ops_day.reminder_hhmm(
                    sched["evening_post"], ops_day.EVENING_REMINDER_MINUTES
                )
                chef_stop_nudge = ops_day.reminder_hhmm(
                    sched["chef_stop_deadline"], ops_day.CHEF_STOP_REMINDER_MINUTES
                )

                if hm == chef_stop_nudge:
                    cskey = f"{cid}|ops_csr|{morning_day}"
                    if not sent_map.get(cskey):
                        rec_cs = chat_record(data, int(cid)) or {}
                        stop = ops_day.get_chef_stop(rec_cs, morning_day)
                        if not ops_day.chef_stop_has_content(stop):
                            try:
                                await _ops_nudge_chef_stop(
                                    data,
                                    int(cid),
                                    deadline_hm=sched["chef_stop_deadline"],
                                )
                                sent_map[cskey] = True
                                changed = True
                                print(f"[ops-chef-stop-nudge] chat={cid}")
                            except Exception as e:
                                print(f"[ops-chef-stop-nudge-fail] {cid}: {e}")

                if hm == sched["chef_stop_deadline"]:
                    csokey = f"{cid}|ops_cso|{morning_day}"
                    if not sent_map.get(csokey):
                        rec_cso = chat_record(data, int(cid)) or {}
                        stop = ops_day.get_chef_stop(rec_cso, morning_day)
                        if not ops_day.chef_stop_has_content(stop):
                            try:
                                await _ops_nudge_chef_stop(
                                    data,
                                    int(cid),
                                    deadline_hm=sched["chef_stop_deadline"],
                                    overdue=True,
                                )
                                sent_map[csokey] = True
                                changed = True
                                print(f"[ops-chef-stop-overdue] chat={cid}")
                            except Exception as e:
                                print(f"[ops-chef-stop-overdue-fail] {cid}: {e}")

                if hm == morning_nudge:
                    mrkey = f"{cid}|ops_mr|{morning_day}"
                    if not sent_map.get(mrkey):
                        rec_m = chat_record(data, int(cid)) or {}
                        morning = ops_day.get_morning(rec_m, morning_day)
                        if not ops_day.morning_complete(morning):
                            try:
                                await _ops_nudge_managers(
                                    data,
                                    int(cid),
                                    kind="morning",
                                    deadline_hm=sched["morning_post"],
                                )
                                sent_map[mrkey] = True
                                changed = True
                                print(f"[ops-morning-nudge] chat={cid}")
                            except Exception as e:
                                print(f"[ops-morning-nudge-fail] {cid}: {e}")

                if hm == sched["morning_post"]:
                    mkey = f"{cid}|ops_m|{morning_day}"
                    if not sent_map.get(mkey):
                        try:
                            result, _detail = await _post_morning_to_group(
                                data, int(cid), morning_day
                            )
                            if result == "ok":
                                sent_map[mkey] = True
                                changed = True
                                print(f"[ops-morning-auto] chat={cid}")
                        except Exception as e:
                            print(f"[ops-morning-auto-fail] {cid}: {e}")
                    csvm = f"{cid}|chef_svm|{morning_day}"
                    if not sent_map.get(csvm) and problems_pulse.chefs_for_chat(
                        data, int(cid)
                    ):
                        try:
                            await _ops_nudge_chef_survey(
                                data, int(cid), kind="morning"
                            )
                            sent_map[csvm] = True
                            changed = True
                            print(f"[chef-survey-morning] chat={cid}")
                        except Exception as e:
                            print(f"[chef-survey-morning-fail] {cid}: {e}")

                if hm == evening_nudge:
                    erkey = f"{cid}|ops_er|{evening_day}"
                    if not sent_map.get(erkey):
                        rec_e = chat_record(data, int(cid)) or {}
                        evening = ops_day.get_evening(rec_e, evening_day)
                        if not ops_day.evening_complete(evening):
                            try:
                                await _ops_nudge_managers(
                                    data,
                                    int(cid),
                                    kind="evening",
                                    deadline_hm=sched["evening_post"],
                                )
                                sent_map[erkey] = True
                                changed = True
                                print(f"[ops-evening-nudge] chat={cid}")
                            except Exception as e:
                                print(f"[ops-evening-nudge-fail] {cid}: {e}")

                if hm == sched["evening_post"]:
                    ekey = f"{cid}|ops_e|{evening_day}"
                    if not sent_map.get(ekey):
                        try:
                            result, _detail = await _post_evening_to_group(
                                data, int(cid), evening_day
                            )
                            sent_map[ekey] = True
                            changed = True
                            if result == "ok":
                                print(f"[ops-evening-auto] chat={cid}")
                                await _send_daily_digest_to_managers(
                                    data, int(cid), evening_day
                                )
                                csve = f"{cid}|chef_sve|{evening_day}"
                                if not sent_map.get(csve) and problems_pulse.chefs_for_chat(
                                    data, int(cid)
                                ):
                                    try:
                                        await _ops_nudge_chef_survey(
                                            data, int(cid), kind="evening"
                                        )
                                        sent_map[csve] = True
                                        changed = True
                                        print(f"[chef-survey-evening] chat={cid}")
                                    except Exception as e:
                                        print(f"[chef-survey-evening-fail] {cid}: {e}")
                            elif result == "incomplete":
                                await _ops_nudge_managers(
                                    data,
                                    int(cid),
                                    kind="evening",
                                    deadline_hm=sched["evening_post"],
                                )
                                print(f"[ops-evening-missed] chat={cid}")
                        except Exception as e:
                            print(f"[ops-evening-auto-fail] {cid}: {e}")

                if hm == "10:00":
                    ukey = f"{cid}|unopened3|{today}"
                    if not sent_map.get(ukey):
                        rec_u = chat_record(data, int(cid)) or info
                        streak = ops_day.consecutive_unopened_days(rec_u, tz_name)
                        if streak >= 3:
                            title_u = escape(str(info.get("title", cid)))
                            alert_text = (
                                f"⚠️ <b>Смена не открыта</b> · {title_u}\n\n"
                                f"Уже <b>{streak}</b> дн. подряд нет открытия в боте.\n"
                                f"Нужен чек-лист и «{pulse_model.BTN_DAY_PLAN}»."
                            )
                            oid = info.get("organization_id")
                            recipients: set[int] = set(ADMIN_IDS) | set(
                                ADMINS_BY_ABS
                            ) | set(
                                pulse_model.network_admin_ids_for_org(
                                    data, str(oid) if oid else None
                                )
                            )
                            for mid in recipients:
                                try:
                                    await bot.send_message(
                                        mid, alert_text, parse_mode="HTML"
                                    )
                                except Exception as e:
                                    print(f"[unopened-alert] mid={mid}: {e}")
                            sent_map[ukey] = True
                            changed = True
                            print(f"[unopened-alert] chat={cid} streak={streak}")

                # Понедельник 10:00 — синхронизация проблем
                now_local = datetime.now(tz)
                if now_local.weekday() == 0 and hm == "10:00":
                    week_key = now_local.strftime("%G-W%V")
                    digest_key = f"{cid}|weekly|{week_key}"
                    if not sent_map.get(digest_key):
                        try:
                            await _run_weekly_problems_for_chat(data, cid, info)
                            sent_map[digest_key] = True
                            changed = True
                            print(f"[weekly-problems] chat={cid}")
                        except Exception as e:
                            print(f"[weekly-problems-fail] {cid}: {e}")

                # Воскресенье 20:00 — AI-советник: итог недели в личку менеджеру
                if now_local.weekday() == 6 and hm == "20:00":
                    week_key = now_local.strftime("%G-W%V")
                    ai_week_key = f"{cid}|ai_weekly|{week_key}"
                    if not sent_map.get(ai_week_key):
                        try:
                            await _run_weekly_ai_advice_for_chat(data, cid, info)
                            sent_map[ai_week_key] = True
                            changed = True
                            print(f"[ai-weekly] chat={cid}")
                        except Exception as e:
                            print(f"[ai-weekly-fail] {cid}: {e}")

            # Понедельник 10:05 — управляющим сети: точки без реакции на проблемы
            try:
                ref_tz = get_tz(DEFAULT_TZ)
                now_msk = datetime.now(ref_tz)
                if now_msk.weekday() == 0 and now_msk.strftime("%H:%M") == "10:05":
                    week_key = now_msk.strftime("%G-W%V")
                    net_key = f"netstale|sent|{week_key}"
                    if not sent_map.get(net_key):
                        await _notify_network_stale_problems(data, week_key=week_key)
                        sent_map[net_key] = True
                        changed = True
            except Exception as e:
                print(f"[net-stale-fail] {e}")

            for k in list(sent_map.keys()):
                if "|mgr_alert|" in str(k):
                    continue
                parts = k.split("|")
                if len(parts) >= 3 and parts[2] < today:
                    del sent_map[k]
                    changed = True

            if manager_alerts.prune_alert_sent_map(sent_map):
                changed = True

            if changed:
                await save_data(data)
        except Exception as e:
            print(f"[scheduler] {e}")
        await asyncio.sleep(45)


@dp.callback_query(
    F.data.startswith("rating_")
    | F.data.startswith("mood_")
    | F.data.startswith("problem_")
    | F.data.startswith("blocker_"),
    lambda c: c.message.chat.type != "private",
)
async def survey_wrong_chat(callback: CallbackQuery) -> None:
    await callback.answer(
        "Оценку нужно пройти в личке: нажмите «Рассказать в личке» в последнем сообщении бота.",
        show_alert=True,
    )


@dp.callback_query(
    F.data.startswith("personal_")
    | F.data.startswith("final_")
    | F.data.startswith("rating5_"),
    lambda c: c.message.chat.type != "private",
)
async def personal_wrong_chat(callback: CallbackQuery) -> None:
    await callback.answer("Продолжите в личке с ботом по ссылке из чата.", show_alert=True)


@dp.callback_query(F.data.startswith("mood_"), lambda c: c.message.chat.type == "private")
async def mood_handler(callback: CallbackQuery) -> None:
    code = callback.data.replace("mood_", "", 1)
    rating = shift_survey.rating_from_mood(code)
    if rating is None:
        await callback.answer()
        return
    user_id = callback.from_user.id
    data = await load_data()
    restaurant = restaurant_label_for_log(data, user_id)
    rest_chat = user_linked_chat.get(user_id)
    label = shift_survey.MOOD_LABELS.get(code, code)

    print("------------")
    print(f"LOG mood user={user_id} rest={restaurant} mood={code} rating={rating}")
    print("------------")
    await log_feedback_event(
        {
            "event": "rating",
            "user_id": user_id,
            "restaurant_chat_id": rest_chat,
            "restaurant_label": restaurant,
            "organization_id": org_id_for_restaurant_chat(data, rest_chat),
            "rating": rating,
            "mood": code,
            "department": survey_department_for_user(data, user_id),
        }
    )
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Записали: <b>{escape(label)}</b>", parse_mode="HTML")
    await callback.answer()
    user_last_mood[user_id] = code
    await prompt_blocker(callback.message, user_id=user_id)


@dp.callback_query(F.data.startswith("rating_"), lambda c: c.message.chat.type == "private")
async def rating_handler_legacy(callback: CallbackQuery) -> None:
    """Старые сообщения со звёздами — всё равно ведём в новый опрос."""
    try:
        rating = int(callback.data.split("_")[1])
    except Exception:
        await callback.answer()
        return
    # Обратная карта: 5→charged, 4→normal, 3/2→meh, 1→heavy
    mood = {5: "charged", 4: "normal", 3: "meh", 2: "meh", 1: "heavy"}.get(rating, "meh")
    callback.data = f"mood_{mood}"
    await mood_handler(callback)


@dp.callback_query(F.data.startswith("blocker_"), lambda c: c.message.chat.type == "private")
async def blocker_handler(callback: CallbackQuery) -> None:
    action = callback.data.replace("blocker_", "", 1)
    user_id = callback.from_user.id
    data = await load_data()
    restaurant = restaurant_label_for_log(data, user_id)
    rest_chat = user_linked_chat.get(user_id)
    org_id = org_id_for_restaurant_chat(data, rest_chat)

    await callback.message.edit_reply_markup(reply_markup=None)

    if action not in shift_survey.BLOCKER_LABELS:
        await callback.answer()
        return

    label = shift_survey.BLOCKER_LABELS[action]
    positive = user_last_mood.get(user_id) == "charged"
    if action != "ok":
        # Заряженная смена: фактор силы, не «проблема» для горящих сигналов
        event_name = "boost" if positive else "problem"
        await log_feedback_event(
            {
                "event": event_name,
                "user_id": user_id,
                "restaurant_chat_id": rest_chat,
                "restaurant_label": restaurant,
                "organization_id": org_id,
                "problem": action,
                "department": survey_department_for_user(data, user_id),
                "mood": user_last_mood.get(user_id),
            }
        )
        user_last_problem_code[user_id] = action
    else:
        user_last_problem_code.pop(user_id, None)

    user_pending_problem.pop(user_id, None)
    await callback.message.answer(f"Записали: <b>{escape(label)}</b>", parse_mode="HTML")
    await callback.answer()
    await prompt_final_comment(callback.message, user_id)


@dp.callback_query(F.data.startswith("problem_"), lambda c: c.message.chat.type == "private")
async def problem_handler_legacy(callback: CallbackQuery) -> None:
    """Старые кнопки problem_* → тот же шаг «что мешало»."""
    action = callback.data.replace("problem_", "", 1)
    if action == "skip":
        action = "ok"
    # legacy → новые коды
    legacy_map = {
        "staff": "team",
        "management": "processes",
        "conflict": "team",
        "stress": "self",
        "comment": "ok",
    }
    action = legacy_map.get(action, action)
    if action not in shift_survey.BLOCKER_LABELS:
        action = "ok"
    callback.data = f"blocker_{action}"
    await blocker_handler(callback)


@dp.callback_query(
    F.data.startswith("rating5_"),
    lambda c: c.message.chat.type == "private",
)
async def rating5_followup_handler(callback: CallbackQuery) -> None:
    """Старый follow-up после 5★ — сразу к комментарию."""
    user_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await prompt_final_comment(callback.message, user_id)


@dp.callback_query(
    F.data.startswith("personal_"),
    lambda c: c.message.chat.type == "private",
)
async def personal_handler(callback: CallbackQuery) -> None:
    """Старый шаг личных факторов — пропускаем к комментарию."""
    user_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await prompt_final_comment(callback.message, user_id)


@dp.callback_query(
    F.data.startswith("final_"),
    lambda c: c.message.chat.type == "private",
)
async def final_comment_handler(callback: CallbackQuery) -> None:
    action = callback.data.replace("final_", "")
    user_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)

    if action == "skip":
        waiting_for_comment.discard(user_id)
        user_pending_problem.pop(user_id, None)
        finish_private_flow(user_id)
        await callback.answer()
        await answer_private_flow_end(
            callback.message, user_id, "Спасибо за обратную связь ❤️"
        )
        return

    await callback.answer()


@dp.message(Command("signals", "problems"))
async def cmd_problems(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer(
            "Команды <code>/signals</code> и <code>/problems</code> — в личке с ботом.",
            parse_mode="HTML",
        )
        return
    uid = message.from_user.id
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await message.answer("Нет доступа.")
        return
    scope = report_pulse.chat_scope_for_user(
        data, uid, is_global_admin=is_global_admin(uid)
    )
    if not scope:
        await message.answer("Нет привязанных точек.")
        return
    if len(scope) > 1:
        await message.answer(
            f"<b>{escape(pulse_model.BTN_SIGNALS)}</b>\n\nВыберите точку:",
            parse_mode="HTML",
            reply_markup=problems_pulse.signals_location_keyboard(scope),
        )
        return
    await _show_problems_for_manager(message, uid, int(scope[0][0]))


@dp.callback_query(F.data.startswith("ai:lm:"))
async def advisor_learn_more_handler(callback: CallbackQuery) -> None:
    """Кнопка «Подробнее» после совета наставника — книга / статья / wiki по теме."""
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    raw = callback.data or ""
    token = raw.split(":", 2)[-1].strip() if raw else ""
    learn = ai_advisor.get_learn_more(token) if token else None
    if not learn:
        await callback.answer(
            "Материал устарел — запросите совет ещё раз.",
            show_alert=True,
        )
        return
    await callback.answer()
    await callback.message.answer(
        ai_advisor.format_learn_more_html(learn),
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


@dp.callback_query(F.data.startswith("pr:"))
async def problems_callback_handler(callback: CallbackQuery) -> None:
    try:
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer()
            return
        uid = callback.from_user.id
        data = await load_data()
        if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
            await callback.answer("Нет доступа.", show_alert=True)
            return

        raw = callback.data or ""
        action, segments = problems_pulse.parse_pr_callback(raw)
        print(f"[pr-cb] uid={uid} action={action!r} segs={segments!r} raw={raw!r}")

        pick_cid = problems_pulse.parse_pr_chat_pick(raw)
        if pick_cid is not None:
            if not await _manager_can_access_chat(data, uid, pick_cid):
                await callback.answer("Нет доступа к точке.", show_alert=True)
                return
            await callback.answer("Загружаю…")
            try:
                await _show_problems_for_manager(callback.message, uid, pick_cid)
            except Exception as e:
                print(f"[pr-c pick] chat={pick_cid} err={e!r}")
                await callback.message.answer(
                    "Не удалось открыть список. Нажмите /signals и выберите точку снова.",
                    parse_mode="HTML",
                )
            return

        if action == "v" and segments:
            pid = problems_pulse.pr_problem_id_from_segments(segments)
            if not pid:
                await callback.answer("Тема не найдена.", show_alert=True)
                return
            prob = await problems_pulse.get_problem(data, pid)
            if not prob:
                await callback.answer("Тема не найдена.", show_alert=True)
                return
            chat_id = prob.restaurant_chat_id
            if not await _manager_can_access_chat(data, uid, chat_id):
                await callback.answer("Нет доступа к точке.", show_alert=True)
                return
            manager_problem_chat[uid] = chat_id
            await callback.answer()
            await callback.message.answer(
                problems_pulse.format_problem_card(prob),
                parse_mode="HTML",
                reply_markup=problems_pulse.problem_card_keyboard(
                    pid, prob.status, chat_id
                ),
            )
            return

        if action == "a" and segments:
            pid = problems_pulse.pr_problem_id_from_segments(segments)
            if not pid:
                await callback.answer("Тема не найдена.", show_alert=True)
                return
            prob = await problems_pulse.get_problem(data, pid)
            if not prob:
                await callback.answer("Тема не найдена.", show_alert=True)
                return
            chat_id = prob.restaurant_chat_id
            if not await _manager_can_access_chat(data, uid, chat_id):
                await callback.answer("Нет доступа к точке.", show_alert=True)
                return
            manager_problem_chat[uid] = chat_id
            await callback.answer("Наставник думает…")
            try:
                await _send_advisor_for_problem(
                    target_message=callback.message,
                    data=data,
                    prob=prob,
                    uid=uid,
                )
            except Exception as e:
                print(f"[pr-a] uid={uid} pid={pid}: {e}")
                await callback.message.answer(
                    "Не удалось запросить совет. Попробуйте ещё раз."
                )
            return

        if action == "w" and segments:
            pid, st_code = problems_pulse.pr_status_change_from_segments(segments)
            status = problems_pulse.STATUS_FROM_CB.get(st_code or "")
            if not pid or not status:
                await callback.answer()
                return
            prob = await problems_pulse.get_problem(data, pid)
            if not prob:
                await callback.answer("Тема не найдена.", show_alert=True)
                return
            chat_id = prob.restaurant_chat_id
            if not await _manager_can_access_chat(data, uid, chat_id):
                await callback.answer("Нет доступа к точке.", show_alert=True)
                return
            manager_problem_chat[uid] = chat_id
            if prob.status == status:
                st_ru = problems_pulse.STATUS_RU.get(status, status)
                await callback.answer(f"Уже: {st_ru}", show_alert=True)
                await callback.message.answer(
                    problems_pulse.format_problem_card(prob),
                    parse_mode="HTML",
                    reply_markup=problems_pulse.problem_card_keyboard(
                        pid, prob.status, chat_id
                    ),
                )
                return
            if (
                status == problems_pulse.STATUS_IN_PROGRESS
                and prob.status == problems_pulse.STATUS_IN_PROGRESS
            ):
                await callback.answer("Уже в работе у команды", show_alert=True)
                await callback.message.answer(
                    problems_pulse.format_problem_card(prob),
                    parse_mode="HTML",
                    reply_markup=problems_pulse.problem_card_keyboard(
                        pid, prob.status, chat_id
                    ),
                )
                return
            manager_problem_pending[uid] = (pid, status)
            await callback.answer()
            st_ru = problems_pulse.STATUS_RU.get(status, status)
            await callback.message.answer(
                f"Статус «<b>{escape(st_ru)}</b>» для «{escape(prob.title)}».\n\n"
                "Напишите комментарий для команды (1–2 предложения) "
                "или нажмите «Пропустить комментарий».",
                parse_mode="HTML",
                reply_markup=problems_pulse.comment_skip_keyboard(pid, chat_id),
            )
            return

        if action == "k" and segments:
            pid = problems_pulse.pr_problem_id_from_segments(segments)
            pending = manager_problem_pending.pop(uid, None)
            if not pid or not pending or pending[0] != pid:
                await callback.answer()
                return
            await callback.answer()
            await _apply_problem_status_change(
                callback.message, uid, pid, pending[1], None
            )
            return

        chat_id = await _pr_chat_id_from_parts(data, uid, segments, action)
        if chat_id is None:
            await callback.answer(
                f"Сначала выберите точку: /signals или «{pulse_model.BTN_SIGNALS}»",
                show_alert=True,
            )
            return
        manager_problem_chat[uid] = chat_id

        if action == "l":
            await callback.answer()
            await _show_problems_for_manager(callback.message, uid, chat_id)
            return

        if action == "arch":
            await callback.answer()
            await _show_signals_archive(callback.message, uid, chat_id)
            return

        if action == "add":
            waiting_signal_title[uid] = chat_id
            await callback.answer()
            await callback.message.answer(
                "Напишите <b>название темы</b> (3–50 символов).\n"
                "Это появится в активном списке — можно вести вручную, без опроса.",
                parse_mode="HTML",
            )
            return

        if action == "sync":
            await callback.answer("Обновляю…")
            rec = chat_record(data, chat_id) or {}
            changes = await problems_pulse.sync_problems_from_period(
                data,
                chat_id,
                rec.get("organization_id"),
                jsonl_path=FEEDBACK_LOG_PATH,
                tz_name=rec.get("timezone", DEFAULT_TZ),
                days=problems_pulse.SIGNALS_SYNC_DAYS,
            )
            await save_data(data)
            active_rows = await problems_pulse.list_problems_for_chat(data, chat_id)
            archive = await problems_pulse.list_problems_for_chat(
                data, chat_id, view=problems_pulse.VIEW_ARCHIVE
            )
            title = rec.get("title", str(chat_id))
            notes = [
                (f"Новая: {r.title}" if c else f"Обновлено: {r.title}")
                for r, c in changes
            ]
            mgr_text = problems_pulse.format_manager_problems_report(
                str(title), active_rows, sync_notes=notes or ["Без изменений по порогам"]
            )
            await _notify_managers_problem_report(
                data, chat_id, mgr_text, exclude_uid=uid, archive_count=len(archive)
            )
            await callback.message.answer(mgr_text, parse_mode="HTML")
            await _show_problems_for_manager(callback.message, uid, chat_id)
            return

        await callback.answer()
    except Exception as e:
        print(f"[pr-cb ERROR] {e!r} data={callback.data!r}")
        try:
            await callback.answer(
                "Ошибка. Откройте /signals и нажмите «Обновить».",
                show_alert=True,
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("pb:"))
async def survey_buttons_callback(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    chat_id: int | None = None
    if action == "cfg" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            pass
    elif action in ("t", "ed", "del") and len(parts) > 3:
        try:
            chat_id = int(parts[2])
        except ValueError:
            pass
    elif action == "add" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            pass
    if chat_id is None:
        chat_id = await _resolve_manager_problem_chat(data, uid)
    if chat_id is None:
        await callback.answer(
            f"Сначала откройте «{pulse_model.BTN_SIGNALS}» и выберите точку.",
            show_alert=True,
        )
        return
    manager_problem_chat[uid] = chat_id

    if action == "cfg":
        await callback.answer()
        await _refresh_buttons_panel(
            uid=uid, chat_id=chat_id, reply_target=callback.message
        )
        return

    if action == "t" and len(parts) > 2:
        code = parts[3] if len(parts) > 3 else parts[2]
        ok, err = survey_buttons.toggle_enabled(data, chat_id, code)
        if not ok:
            await callback.answer(err or "Ошибка", show_alert=True)
            return
        await save_data(data)
        await callback.answer()
        await _refresh_buttons_panel(
            uid=uid, chat_id=chat_id, edit_target=callback.message
        )
        return

    if action == "del" and len(parts) > 2:
        code = parts[3] if len(parts) > 3 else parts[2]
        ok, err = survey_buttons.delete_custom(data, chat_id, code)
        if not ok:
            await callback.answer(err or "Ошибка", show_alert=True)
            return
        await save_data(data)
        await callback.answer("Удалено")
        await _refresh_buttons_panel(
            uid=uid, chat_id=chat_id, edit_target=callback.message
        )
        return

    if action == "ed" and len(parts) > 2:
        code = parts[3] if len(parts) > 3 else parts[2]
        waiting_button_edit[uid] = ("edit", chat_id, code)
        await callback.answer()
        await callback.message.answer(
            "Новый текст кнопки (2–40 символов):",
            parse_mode="HTML",
        )
        return

    if action == "add":
        if survey_buttons.count_enabled(survey_buttons.get_buttons(data, chat_id)) >= survey_buttons.MAX_ENABLED:
            await callback.answer(
                f"Сначала выключите кнопку — максимум {survey_buttons.MAX_ENABLED} активных.",
                show_alert=True,
            )
            return
        waiting_button_edit[uid] = ("add", chat_id, "")
        await callback.answer()
        await callback.message.answer("Текст новой кнопки (2–40 символов):", parse_mode="HTML")
        return

    await callback.answer()


async def _edit_checklist_message(
    message: Message,
    rec: dict,
    chat_id: int,
    day: str,
    kind: str,
    *,
    item_id: str | None = None,
) -> None:
    title = str(rec.get("title", chat_id))
    if item_id:
        text = shift_checklists.format_item_action_prompt(
            rec, title, day, kind, item_id
        )
        kb = shift_checklists.item_action_keyboard(
            rec, chat_id, day, kind, item_id
        )
    else:
        text = shift_checklists.format_checklist_prompt(rec, title, day, kind)
        kb = shift_checklists.run_checklist_keyboard(rec, chat_id, day, kind)
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("cl:"))
async def checklist_callback_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "menu" and len(parts) > 2:
        chat_id = int(parts[2])
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        await callback.answer()
        await callback.message.answer(
            f"<b>{pulse_model.BTN_CHECKLISTS}</b> · {escape(str(rec.get('title', chat_id)))}",
            parse_mode="HTML",
            reply_markup=shift_checklists.checklist_menu_keyboard(chat_id),
        )
        return
    if action == "cfg" and len(parts) > 3:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        text = shift_checklists.format_config_message(
            rec, str(rec.get("title", chat_id)), kind
        )
        kb = shift_checklists.build_config_keyboard(rec, chat_id, kind)
        await callback.answer()
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
        return
    if action == "add" and len(parts) > 3:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        waiting_checklist_add[uid] = (kind, chat_id)
        await callback.answer()
        await callback.message.answer(
            f"Новый пункт ({shift_checklists.kind_title(kind)}), 2–120 символов:",
            parse_mode="HTML",
        )
        return
    if action == "del" and len(parts) > 4:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        item_id = parts[4]
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        ok, err = shift_checklists.delete_template_item(rec, kind, item_id)
        if not ok:
            await callback.answer(err or "Ошибка", show_alert=True)
            return
        await save_data(data)
        await callback.answer("Удалено")
        text = shift_checklists.format_config_message(
            rec, str(rec.get("title", chat_id)), kind
        )
        kb = shift_checklists.build_config_keyboard(rec, chat_id, kind)
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return
    if action in ("chk", "itm") and len(parts) > 5:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        item_id = parts[5]
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        tz_name = rec.get("timezone", DEFAULT_TZ)
        day = shift_checklists.day_for_kind(rec, kind, tz_name)
        shift_checklists.toggle_check(rec, day, kind, item_id, uid)
        await save_data(data)
        await callback.answer()
        await _edit_checklist_message(callback.message, rec, chat_id, day, kind)
        return
    if action == "back" and len(parts) > 4:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        tz_name = rec.get("timezone", DEFAULT_TZ)
        day = shift_checklists.day_for_kind(rec, kind, tz_name)
        await callback.answer()
        await _edit_checklist_message(callback.message, rec, chat_id, day, kind)
        return
    if action == "rmk" and len(parts) > 4:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        day = parts[4]
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            await callback.answer()
            return
        rec = chat_record(data, chat_id) or {}
        if not shift_checklists.all_checked(rec, day, kind):
            await callback.answer("Сначала отметьте все пункты.", show_alert=True)
            return
        waiting_checklist_remark[uid] = {
            "chat_id": chat_id,
            "kind": kind,
            "day": day,
        }
        await callback.answer()
        await callback.message.answer(
            f"📝 <b>Замечание</b> · {escape(shift_checklists.kind_title(kind))}\n\n"
            "Что было не так и как решили?\n"
            "<i>3–500 символов. «—» — отмена.</i>",
            parse_mode="HTML",
        )
        return
    if action == "rskip" and len(parts) > 4:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        day = parts[4]
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            await callback.answer()
            return
        rec = chat_record(data, chat_id) or {}
        if not shift_checklists.all_checked(rec, day, kind):
            await callback.answer("Сначала отметьте все пункты.", show_alert=True)
            return
        shift_checklists.set_remark_skipped(rec, day, kind, uid)
        await save_data(data)
        await callback.answer("Ок")
        await _edit_checklist_message(callback.message, rec, chat_id, day, kind)
        return
    if action == "ok" and len(parts) > 5:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        item_id = parts[5]
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        tz_name = rec.get("timezone", DEFAULT_TZ)
        day = shift_checklists.day_for_kind(rec, kind, tz_name)
        shift_checklists.set_check_status(
            rec, day, kind, item_id, uid, status="ok"
        )
        await save_data(data)
        await callback.answer("✅")
        await _edit_checklist_message(callback.message, rec, chat_id, day, kind)
        return
    if action == "iss" and len(parts) > 5:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        item_id = parts[5]
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        tz_name = rec.get("timezone", DEFAULT_TZ)
        day = shift_checklists.day_for_kind(rec, kind, tz_name)
        shift_checklists.set_check_status(
            rec, day, kind, item_id, uid, status="ok"
        )
        await save_data(data)
        await callback.answer("✅")
        await _edit_checklist_message(callback.message, rec, chat_id, day, kind)
        return
    if action == "clr" and len(parts) > 5:
        kind = shift_checklists.kind_from_code(parts[2])
        if not kind:
            await callback.answer()
            return
        chat_id = int(parts[3])
        item_id = parts[5]
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        tz_name = rec.get("timezone", DEFAULT_TZ)
        day = shift_checklists.day_for_kind(rec, kind, tz_name)
        shift_checklists.set_check_status(
            rec, day, kind, item_id, uid, status="clear"
        )
        await save_data(data)
        await callback.answer("Снято")
        await _edit_checklist_message(callback.message, rec, chat_id, day, kind)
        return
    await callback.answer()


@dp.callback_query(F.data.startswith("audit:"))
async def audit_callback_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not await _user_can_ai_audit(uid, data):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "cancel":
        ai_auditor.cancel_session(uid)
        await callback.answer("Отменено")
        try:
            await callback.message.edit_text("Аудит отменён.")
        except Exception:
            pass
        await callback.message.answer(
            "Главное меню.", reply_markup=_manager_menu_markup(uid)
        )
        return
    if action == "org" and len(parts) > 2:
        org_id = parts[2]
        ga = is_global_admin(uid)
        allowed = {oid for oid, _ in pulse_model.audit_orgs_for_user(data, uid, is_global_admin=ga)}
        if org_id not in allowed:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _begin_audit_for_org(callback.message, uid, org_id)
        return
    await callback.answer()


@dp.callback_query(F.data.startswith("sa:"))
async def staff_assign_callback_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    ga = is_global_admin(uid)
    if not staff_assign.can_manage_staff(data, uid, is_global_admin=ga):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        waiting_staff_assign.pop(uid, None)
        await callback.answer("Отменено")
        return

    if action == "rmmenu":
        waiting_staff_remove.pop(uid, None)
        await callback.answer()
        locs = staff_assign.assignable_locations(data, uid, is_global_admin=ga)
        nets = staff_assign.assignable_network_orgs(data, uid, is_global_admin=ga)
        await callback.message.edit_text(
            staff_assign.format_remove_start_message(),
            parse_mode="HTML",
            reply_markup=staff_assign.locations_keyboard(
                locs, network_orgs=nets, mode="remove"
            ),
        )
        return

    if action == "rmloc" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not staff_assign.can_assign_at_location(
            data, uid, chat_id, is_global_admin=ga
        ):
            await callback.answer("Нет доступа к точке.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        org_id = rec.get("organization_id")
        if not org_id:
            await callback.answer("Точка не привязана к организации.", show_alert=True)
            return
        entries = staff_assign.list_staff_at_location(data, chat_id, str(org_id))
        if not entries:
            await callback.answer("На точке никого не подключено.", show_alert=True)
            return
        title = str(rec.get("title", chat_id))
        await callback.answer()
        await callback.message.edit_text(
            staff_assign.format_remove_pick(title),
            parse_mode="HTML",
            reply_markup=staff_assign.staff_remove_keyboard(
                entries, chat_id=chat_id, org_id=str(org_id)
            ),
        )
        return

    if action == "rmorg" and len(parts) > 2:
        org_id = parts[2]
        if not staff_assign.can_assign_network_org(
            data, uid, org_id, is_global_admin=ga
        ):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        uids = staff_assign.list_network_admins(data, org_id)
        if not uids:
            await callback.answer("Нет управляющих сети.", show_alert=True)
            return
        org = data.get("organizations", {}).get(org_id, {})
        org_name = str(org.get("name", org_id))
        entries = [
            (u, pulse_model.role_label_ru(pulse_model.ROLE_NETWORK_ADMIN), "nw")
            for u in uids
        ]
        await callback.answer()
        await callback.message.edit_text(
            staff_assign.format_remove_network_pick(org_name),
            parse_mode="HTML",
            reply_markup=staff_assign.staff_remove_keyboard(
                entries, chat_id=None, org_id=org_id
            ),
        )
        return

    if action == "rm" and len(parts) > 5:
        try:
            target_uid = int(parts[2])
            code = parts[3]
            chat_id = int(parts[4])
            org_id = parts[5]
        except (ValueError, IndexError):
            await callback.answer()
            return
        role = staff_assign.ROLE_CODES.get(code)
        if not role:
            await callback.answer()
            return
        loc_cid = None if role == pulse_model.ROLE_NETWORK_ADMIN else chat_id
        if not staff_assign.can_remove_target(
            data,
            uid,
            is_global_admin=ga,
            target_uid=target_uid,
            org_id=org_id,
            role=role,
            chat_id=loc_cid,
        ):
            await callback.answer("Нет прав.", show_alert=True)
            return
        role_label = pulse_model.role_label_ru(role)
        if role == pulse_model.ROLE_NETWORK_ADMIN:
            org = data.get("organizations", {}).get(org_id, {})
            place = str(org.get("name", org_id))
        else:
            rec = chat_record(data, chat_id) or {}
            place = str(rec.get("title", chat_id))
        await callback.answer()
        await callback.message.edit_text(
            staff_assign.format_remove_confirm(target_uid, role_label, place),
            parse_mode="HTML",
            reply_markup=staff_assign.remove_confirm_keyboard(
                target_uid, code, loc_cid, org_id
            ),
        )
        return

    if action == "rmok" and len(parts) > 5:
        try:
            target_uid = int(parts[2])
            code = parts[3]
            chat_id = int(parts[4])
            org_id = parts[5]
        except (ValueError, IndexError):
            await callback.answer()
            return
        role = staff_assign.ROLE_CODES.get(code)
        if not role:
            await callback.answer()
            return
        loc_cid = None if role == pulse_model.ROLE_NETWORK_ADMIN else chat_id
        if not staff_assign.can_remove_target(
            data,
            uid,
            is_global_admin=ga,
            target_uid=target_uid,
            org_id=org_id,
            role=role,
            chat_id=loc_cid,
        ):
            await callback.answer("Нет прав.", show_alert=True)
            return
        if staff_assign.apply_removal(
            data,
            target_uid=target_uid,
            org_id=org_id,
            role=role,
            chat_id=loc_cid,
        ):
            await save_data(data)
            await callback.answer("Доступ отозван")
            role_label = pulse_model.role_label_ru(role)
            await callback.message.edit_text(
                f"✅ Доступ отозван: <code>{target_uid}</code> · {escape(role_label)}",
                parse_mode="HTML",
            )
        else:
            await callback.answer("Не найдено", show_alert=True)
        return

    if action == "menu":
        await callback.answer()
        locs = staff_assign.assignable_locations(data, uid, is_global_admin=ga)
        nets = staff_assign.assignable_network_orgs(data, uid, is_global_admin=ga)
        await callback.message.edit_text(
            staff_assign.format_start_message(),
            parse_mode="HTML",
            reply_markup=staff_assign.locations_keyboard(locs, network_orgs=nets),
        )
        return

    if action == "loc" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not staff_assign.can_assign_at_location(
            data, uid, chat_id, is_global_admin=ga
        ):
            await callback.answer("Нет доступа к точке.", show_alert=True)
            return
        rec = chat_record(data, chat_id) or {}
        org_id = rec.get("organization_id")
        if not org_id:
            await callback.answer("Точка не привязана к организации.", show_alert=True)
            return
        roles = staff_assign.assignable_role_options(
            data, uid, is_global_admin=ga, for_network=False
        )
        if not roles:
            await callback.answer("Нет доступных ролей.", show_alert=True)
            return
        title = str(rec.get("title", chat_id))
        await callback.answer()
        await callback.message.edit_text(
            staff_assign.format_role_pick(title),
            parse_mode="HTML",
            reply_markup=staff_assign.roles_keyboard(
                roles, chat_id=chat_id, org_id=str(org_id)
            ),
        )
        return

    if action == "org" and len(parts) > 2:
        org_id = parts[2]
        if not staff_assign.can_assign_network_org(
            data, uid, org_id, is_global_admin=ga
        ):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        roles = staff_assign.assignable_role_options(
            data, uid, is_global_admin=ga, for_network=True
        )
        if not roles:
            await callback.answer("Нет доступных ролей.", show_alert=True)
            return
        org = data.get("organizations", {}).get(org_id, {})
        org_name = str(org.get("name", org_id))
        await callback.answer()
        await callback.message.edit_text(
            staff_assign.format_network_role_pick(org_name),
            parse_mode="HTML",
            reply_markup=staff_assign.roles_keyboard(roles, org_id=org_id),
        )
        return

    if action == "role" and len(parts) > 4:
        code = parts[2]
        try:
            chat_id = int(parts[3])
        except ValueError:
            await callback.answer()
            return
        org_id = parts[4]
        role = staff_assign.ROLE_CODES.get(code)
        if not role:
            await callback.answer()
            return
        loc_cid = None if role == pulse_model.ROLE_NETWORK_ADMIN else chat_id
        if role != pulse_model.ROLE_NETWORK_ADMIN and loc_cid is None:
            await callback.answer("Ошибка точки.", show_alert=True)
            return
        if role == pulse_model.ROLE_NETWORK_ADMIN:
            if not staff_assign.can_assign_network_org(
                data, uid, org_id, is_global_admin=ga
            ):
                await callback.answer("Нет доступа.", show_alert=True)
                return
        elif not staff_assign.can_assign_at_location(
            data, uid, loc_cid, is_global_admin=ga
        ):
            await callback.answer("Нет доступа к точке.", show_alert=True)
            return
        allowed = staff_assign.assignable_role_options(
            data,
            uid,
            is_global_admin=ga,
            for_network=role == pulse_model.ROLE_NETWORK_ADMIN,
        )
        if not any(c == code for c, _ in allowed):
            await callback.answer("Нет прав на эту роль.", show_alert=True)
            return
        waiting_staff_assign[uid] = {
            "org_id": org_id,
            "role": role,
            "chat_id": loc_cid,
        }
        role_label = pulse_model.role_label_ru(role)
        if role == pulse_model.ROLE_NETWORK_ADMIN:
            org = data.get("organizations", {}).get(org_id, {})
            place = str(org.get("name", org_id))
        else:
            rec = chat_record(data, chat_id) or {}
            place = str(rec.get("title", chat_id))
        await callback.answer()
        await callback.message.answer(
            staff_assign.format_await_uid(role_label, place),
            parse_mode="HTML",
        )
        return

    await callback.answer()


@dp.callback_query(F.data.startswith("tr:"))
async def training_callback_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    data = await load_data()

    if action == "sm" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        rec = chat_record(data, chat_id) or {}
        title = str(rec.get("title", chat_id))
        await callback.answer()
        await callback.message.edit_text(
            training_materials.format_staff_menu(rec, title),
            parse_mode="HTML",
            reply_markup=training_materials.staff_folders_keyboard(chat_id, rec),
        )
        return

    if action == "sf" and len(parts) > 3:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        folder_id = parts[3]
        rec = chat_record(data, chat_id) or {}
        title = str(rec.get("title", chat_id))
        await callback.answer()
        await callback.message.edit_text(
            training_materials.format_folder_files(rec, folder_id, title),
            parse_mode="HTML",
            reply_markup=training_materials.staff_files_keyboard(
                chat_id, folder_id, rec
            ),
        )
        return

    if action == "fd" and len(parts) > 3:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        file_id = parts[3]
        rec = chat_record(data, chat_id) or {}
        files = training_materials.list_files(rec)
        frow = next((f for f in files if f["id"] == file_id), None)
        if not frow:
            await callback.answer("Файл не найден", show_alert=True)
            return
        await callback.answer()
        fid = frow["file_id"]
        ftype = frow.get("file_type", "document")
        cap = f"📄 {frow.get('title', 'Материал')}"
        try:
            if ftype == "photo":
                await callback.message.answer_photo(fid, caption=cap)
            elif ftype == "video":
                await callback.message.answer_video(fid, caption=cap)
            else:
                await callback.message.answer_document(fid, caption=cap)
        except Exception as e:
            print(f"[training-send] {e}")
            await callback.message.answer("Не удалось отправить файл.")
        return

    if action == "ob":
        # start/list → сразу 1-й ролик; g → индекс; topics → темы; s → id (старые кнопки)
        sub = parts[2] if len(parts) > 2 else ""

        async def _ob_send_reel(chat_id: int, index: int, *, prefer_edit: bool) -> None:
            from aiogram.types import FSInputFile, InputMediaVideo

            path = onboarding_reels.reel_path_at(index)
            if path is None:
                await callback.answer("Ролик не найден", show_alert=True)
                return
            await callback.answer()
            caption = onboarding_reels.caption_at(index)
            kb = onboarding_reels.nav_keyboard(chat_id, index)
            media = FSInputFile(path)
            msg = callback.message
            can_edit = prefer_edit and bool(
                msg and (msg.video or getattr(msg, "animation", None))
            )
            if can_edit:
                try:
                    await msg.edit_media(
                        media=InputMediaVideo(
                            media=media,
                            caption=caption,
                            width=720,
                            height=1280,
                            supports_streaming=True,
                        ),
                        reply_markup=kb,
                    )
                    return
                except Exception as e:
                    print(f"[onboarding-reel-edit] {e}")
            try:
                await msg.answer_video(
                    media,
                    caption=caption,
                    supports_streaming=True,
                    width=720,
                    height=1280,
                    reply_markup=kb,
                )
            except Exception as e:
                print(f"[onboarding-reel-video] {e}")
                try:
                    await msg.answer_document(media, caption=caption, reply_markup=kb)
                except Exception as e2:
                    print(f"[onboarding-reel-doc] {e2}")
                    await msg.answer("Не удалось отправить ролик. Попробуйте ещё раз.")

        if sub == "noop":
            await callback.answer()
            return

        if sub in ("start", "list") and len(parts) > 3:
            try:
                chat_id = int(parts[3])
            except ValueError:
                await callback.answer()
                return
            if not await _manager_can_access_chat(data, uid, chat_id):
                await callback.answer("Нет доступа.", show_alert=True)
                return
            await _ob_send_reel(chat_id, 0, prefer_edit=False)
            return

        if sub == "g" and len(parts) > 4:
            try:
                chat_id = int(parts[3])
                index = int(parts[4])
            except ValueError:
                await callback.answer()
                return
            if not await _manager_can_access_chat(data, uid, chat_id):
                await callback.answer("Нет доступа.", show_alert=True)
                return
            if index < 0 or index >= onboarding_reels.catalog_len():
                await callback.answer("Ролик не найден", show_alert=True)
                return
            prefer_edit = bool(
                callback.message
                and (callback.message.video or getattr(callback.message, "animation", None))
            )
            await _ob_send_reel(chat_id, index, prefer_edit=prefer_edit)
            return

        if sub == "topics" and len(parts) > 3:
            try:
                chat_id = int(parts[3])
            except ValueError:
                await callback.answer()
                return
            cur = 0
            if len(parts) > 4:
                try:
                    cur = int(parts[4])
                except ValueError:
                    cur = 0
            if not await _manager_can_access_chat(data, uid, chat_id):
                await callback.answer("Нет доступа.", show_alert=True)
                return
            await callback.answer()
            text = onboarding_reels.format_topics_menu(cur)
            kb = onboarding_reels.topics_keyboard(chat_id, cur)
            msg = callback.message
            if msg and msg.text is not None:
                try:
                    await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
                    return
                except Exception as e:
                    print(f"[onboarding-topics-edit] {e}")
            await msg.answer(text, parse_mode="HTML", reply_markup=kb)
            return

        if sub == "s" and len(parts) > 4:
            try:
                chat_id = int(parts[3])
            except ValueError:
                await callback.answer()
                return
            reel_id = parts[4]
            if not await _manager_can_access_chat(data, uid, chat_id):
                await callback.answer("Нет доступа.", show_alert=True)
                return
            idx = onboarding_reels.reel_index(reel_id)
            if idx is None:
                await callback.answer("Ролик не найден", show_alert=True)
                return
            await _ob_send_reel(chat_id, idx, prefer_edit=False)
            return

        await callback.answer()
        return

    if action == "mm" and len(parts) > 2:
        if not await _manager_can_access_chat(data, uid, int(parts[2])):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        chat_id = int(parts[2])
        rec = chat_record(data, chat_id) or {}
        title = str(rec.get("title", chat_id))
        await callback.answer()
        text = onboarding_reels.enrich_manager_menu_text(
            training_materials.format_manager_menu(rec, title)
        )
        kb = onboarding_reels.patch_manager_menu_keyboard(
            training_materials.manager_menu_keyboard(chat_id, rec),
            chat_id,
        )
        # С видео-сообщения edit_text не работает — шлём новое
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    if action == "mfold" and len(parts) > 3:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        folder_id = parts[3]
        rec = chat_record(data, chat_id) or {}
        title = str(rec.get("title", chat_id))
        folder = next(
            (f for f in training_materials.list_folders(rec) if f["id"] == folder_id),
            None,
        )
        if not folder:
            await callback.answer("Папка не найдена", show_alert=True)
            return
        await callback.answer()
        await callback.message.edit_text(
            training_materials.format_folder_files(rec, folder_id, title),
            parse_mode="HTML",
            reply_markup=training_materials.manager_folder_keyboard(chat_id, folder_id),
        )
        return

    if action == "mfadd" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        waiting_training_folder[uid] = chat_id
        await callback.answer()
        await callback.message.answer(
            "Название новой <b>папки</b> (2–60 символов):",
            parse_mode="HTML",
        )
        return

    if action == "mup" and len(parts) > 3:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        folder_id = parts[3]
        waiting_training_upload[uid] = {"chat_id": chat_id, "folder_id": folder_id}
        await callback.answer()
        await callback.message.answer(
            "Отправьте <b>файл</b> (документ, фото или видео).\n"
            "Подпись к файлу = название в списке.",
            parse_mode="HTML",
        )
        return

    if action == "mdel" and len(parts) > 3:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        folder_id = parts[3]
        rec = chat_record(data, chat_id) or {}
        training_materials.delete_folder(rec, folder_id)
        await save_data(data)
        title = str(rec.get("title", chat_id))
        await callback.answer("Папка удалена")
        text = onboarding_reels.enrich_manager_menu_text(
            training_materials.format_manager_menu(rec, title)
        )
        kb = onboarding_reels.patch_manager_menu_keyboard(
            training_materials.manager_menu_keyboard(chat_id, rec),
            chat_id,
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    await callback.answer()


@dp.callback_query(F.data.startswith("rm:"))
async def reminders_callback_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    data = await load_data()

    if len(parts) < 3:
        await callback.answer()
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return
    if not await _manager_can_access_chat(data, uid, chat_id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    rec = chat_record(data, chat_id)
    if not rec:
        await callback.answer("Точка не найдена.", show_alert=True)
        return
    title = str(rec.get("title", chat_id))

    if action == "p":
        await callback.answer()
        await callback.message.edit_text(
            reminders_ui.format_panel(rec, title),
            parse_mode="HTML",
            reply_markup=reminders_ui.panel_keyboard(chat_id),
        )
        return

    if action == "s" and len(parts) > 3:
        try:
            slot = int(parts[3])
        except ValueError:
            await callback.answer()
            return
        if slot not in (0, 1):
            await callback.answer()
            return
        await callback.answer()
        label = "первого" if slot == 0 else "второго"
        await callback.message.edit_text(
            f"<b>⏰ {escape(title)}</b>\n\nВыберите время <b>{label}</b> напоминания:",
            parse_mode="HTML",
            reply_markup=reminders_ui.slot_presets_keyboard(chat_id, slot),
        )
        return

    if action == "t" and len(parts) > 4:
        try:
            slot = int(parts[3])
        except ValueError:
            await callback.answer()
            return
        t = reminders_ui.parse_preset_hhmm(parts[4])
        if not t or slot not in (0, 1):
            await callback.answer("Неверное время", show_alert=True)
            return
        _put_reminder_time(rec, slot, t)
        await save_data(data)
        await callback.answer(f"Сохранено {t}")
        await callback.message.edit_text(
            reminders_ui.format_panel(rec, title),
            parse_mode="HTML",
            reply_markup=reminders_ui.panel_keyboard(chat_id),
        )
        return

    if action == "d" and len(parts) > 3:
        try:
            slot = int(parts[3])
        except ValueError:
            await callback.answer()
            return
        _pop_reminder_slot(rec, slot)
        await save_data(data)
        await callback.answer("Убрано")
        await callback.message.edit_text(
            reminders_ui.format_panel(rec, title),
            parse_mode="HTML",
            reply_markup=reminders_ui.panel_keyboard(chat_id),
        )
        return

    if action == "c" and len(parts) > 3:
        try:
            slot = int(parts[3])
        except ValueError:
            await callback.answer()
            return
        waiting_reminder_custom[uid] = {"chat_id": chat_id, "slot": slot}
        await callback.answer()
        label = "первого" if slot == 0 else "второго"
        await callback.message.answer(
            f"Напишите время <b>{label}</b> напоминания в формате "
            f"<code>ЧЧ:ММ</code> (например <code>22:15</code>).",
            parse_mode="HTML",
        )
        return

    if action == "tz":
        await callback.answer()
        await callback.message.edit_text(
            f"<b>🌍 Часовой пояс</b> · {escape(title)}\n\n"
            f"Сейчас: <code>{escape(str(rec.get('timezone') or DEFAULT_TZ))}</code>",
            parse_mode="HTML",
            reply_markup=reminders_ui.timezone_keyboard(chat_id),
        )
        return

    if action == "z" and len(parts) > 3:
        iana = reminders_ui.tz_iana(parts[3])
        if not iana:
            await callback.answer("Неизвестный пояс", show_alert=True)
            return
        rec["timezone"] = iana
        await save_data(data)
        await callback.answer(iana)
        await callback.message.edit_text(
            reminders_ui.format_panel(rec, title),
            parse_mode="HTML",
            reply_markup=reminders_ui.panel_keyboard(chat_id),
        )
        return

    await callback.answer()


@dp.callback_query(F.data.startswith("cb:"))
async def chef_buttons_callback(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    chat_id: int | None = None
    if action == "cfg" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            pass
    elif action in ("t", "ed", "del") and len(parts) > 3:
        try:
            chat_id = int(parts[2])
        except ValueError:
            pass
    elif action == "add" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            pass
    if chat_id is None:
        chat_id = await _resolve_manager_problem_chat(data, uid)
    if chat_id is None:
        await callback.answer(
            f"Сначала откройте «{pulse_model.BTN_SIGNALS}» и выберите точку.",
            show_alert=True,
        )
        return
    manager_problem_chat[uid] = chat_id

    if action == "cfg":
        await callback.answer()
        await _refresh_chef_buttons_panel(
            uid=uid, chat_id=chat_id, reply_target=callback.message
        )
        return

    if action == "t" and len(parts) > 2:
        code = parts[3] if len(parts) > 3 else parts[2]
        ok, err = chef_survey.toggle_enabled(data, chat_id, code)
        if not ok:
            await callback.answer(err or "Ошибка", show_alert=True)
            return
        await save_data(data)
        await callback.answer()
        await _refresh_chef_buttons_panel(
            uid=uid, chat_id=chat_id, edit_target=callback.message
        )
        return

    if action == "del" and len(parts) > 2:
        code = parts[3] if len(parts) > 3 else parts[2]
        ok, err = chef_survey.delete_custom(data, chat_id, code)
        if not ok:
            await callback.answer(err or "Ошибка", show_alert=True)
            return
        await save_data(data)
        await callback.answer("Удалено")
        await _refresh_chef_buttons_panel(
            uid=uid, chat_id=chat_id, edit_target=callback.message
        )
        return

    if action == "ed" and len(parts) > 2:
        code = parts[3] if len(parts) > 3 else parts[2]
        waiting_chef_button_edit[uid] = ("edit", chat_id, code)
        await callback.answer()
        await callback.message.answer(
            "Новый текст кнопки кухни (2–40 символов):",
            parse_mode="HTML",
        )
        return

    if action == "add":
        if chef_survey.count_enabled(chef_survey.get_buttons(data, chat_id)) >= chef_survey.MAX_ENABLED:
            await callback.answer(
                f"Сначала выключите кнопку — максимум {chef_survey.MAX_ENABLED} активных.",
                show_alert=True,
            )
            return
        waiting_chef_button_edit[uid] = ("add", chat_id, "")
        await callback.answer()
        await callback.message.answer(
            "Текст новой кнопки кухни (2–40 символов):",
            parse_mode="HTML",
        )
        return

    await callback.answer()


@dp.callback_query(F.data.startswith("chef_sv:"))
async def chef_survey_callback(callback: CallbackQuery) -> None:
    if callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not (
        is_global_admin(uid)
        or pulse_model.has_chef_access(data, uid)
        or pulse_model.has_ops_staff_access(data, uid)
    ):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    code = (callback.data or "").replace("chef_sv:", "", 1)
    chat_id = user_chef_survey_chat.get(uid)
    if chat_id is None:
        await callback.answer(
            f"Сначала нажмите «{pulse_model.BTN_RATE_SHIFT}» и выберите точку.",
            show_alert=True,
        )
        return
    if not await _ops_can_access_chat(data, uid, chat_id):
        await callback.answer("Нет доступа к точке.", show_alert=True)
        return

    rec = chat_record(data, chat_id) or {}
    title = str(rec.get("title", chat_id))
    org_id = org_id_for_restaurant_chat(data, chat_id)

    await callback.message.edit_reply_markup(reply_markup=None)

    if code == "skip":
        user_chef_survey_chat.pop(uid, None)
        waiting_chef_survey_comment.pop(uid, None)
        await callback.answer()
        await callback.message.answer(
            "Спасибо! Если появится что отметить — всегда можно вернуться.",
            reply_markup=pulse_model.chef_menu_reply_markup(),
        )
        return

    allowed = chef_survey.valid_codes(data, chat_id)
    if code not in allowed:
        await callback.answer()
        return

    if code == "comment":
        waiting_chef_survey_comment[uid] = chat_id
        await callback.answer()
        await callback.message.answer("Опишите, что было сложным на смене ✍️")
        return

    await log_feedback_event(
        {
            "event": "chef_shift",
            "user_id": uid,
            "restaurant_chat_id": chat_id,
            "restaurant_label": title,
            "organization_id": org_id,
            "problem": code,
            "department": chef_survey.DEPARTMENT_KITCHEN,
        }
    )
    user_chef_survey_chat.pop(uid, None)
    labels = chef_survey.labels_map(data, chat_id)
    label = labels.get(code, code)
    await callback.answer()
    await callback.message.answer(
        f"Записали: <b>{escape(label)}</b> · {escape(title)}",
        parse_mode="HTML",
        reply_markup=pulse_model.chef_menu_reply_markup(),
    )


@dp.callback_query(F.data.startswith("ops:"))
async def ops_callback_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "stopick" and len(parts) > 2:
        if not (
            is_global_admin(uid)
            or pulse_model.has_ops_staff_access(data, uid)
        ):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        await _show_stop_list_status(callback.message, uid, chat_id)
        return

    if action == "stopadd" and len(parts) > 2:
        if not (
            is_global_admin(uid)
            or pulse_model.has_ops_staff_access(data, uid)
        ):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        await _show_stop_list_append(callback.message, uid, chat_id)
        return

    if action == "bcpick" and len(parts) > 2:
        if not (
            is_global_admin(uid)
            or pulse_model.has_manager_access(data, uid)
        ):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        rec = chat_record(data, chat_id) or {}
        waiting_ops_broadcast[uid] = chat_id
        await callback.message.answer(
            f"<b>📣 Шефу и менеджерам</b> · {escape(str(rec.get('title', chat_id)))}\n\n"
            "Напишите текст — его получат менеджеры и шеф точки в личку.",
            parse_mode="HTML",
        )
        return

    if action == "clpick" and len(parts) > 2:
        if not await _manager_can_access_chat(data, uid, int(parts[2])):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        await callback.answer()
        rec = chat_record(data, chat_id) or {}
        title = escape(str(rec.get("title", chat_id)))
        await callback.message.answer(
            f"<b>{pulse_model.BTN_CHECKLISTS}</b> · {title}\n\n"
            "Настройте пункты — менеджер отмечает их перед планом и закрытием.",
            parse_mode="HTML",
            reply_markup=shift_checklists.checklist_menu_keyboard(chat_id),
        )
        return

    if action == "bpick" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        rec = chat_record(data, chat_id) or {}
        waiting_evening_backdate[uid] = {"chat_id": chat_id, "step": "date"}
        await callback.message.answer(
            f"<b>Закрытие за дату</b> · {escape(str(rec.get('title', chat_id)))}\n\n"
            "Отправьте дату смены: <code>ДД.ММ.ГГГГ</code> или <code>ГГГГ-ММ-ДД</code>.",
            parse_mode="HTML",
        )
        return

    if action == "trpick" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        await _show_training_manager_menu(callback.message, uid, chat_id)
        return

    if action == "rmpick" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        await _show_reminders_panel(callback.message, uid, chat_id)
        return

    if action in ("svpick", "stview") and len(parts) > 2:
        if not (
            is_global_admin(uid)
            or pulse_model.has_chef_access(data, uid)
            or pulse_model.has_ops_staff_access(data, uid)
        ):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _ops_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        if action == "svpick":
            await _show_chef_survey(callback.message, uid, chat_id)
        else:
            await _show_stop_current(callback.message, uid, chat_id)
        return

    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    if action in ("mpick", "epick", "spick", "mplanpick") and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if action == "mplanpick":
            if not await _manager_can_access_chat(data, uid, chat_id):
                await callback.answer("Нет доступа.", show_alert=True)
                return
            await callback.answer()
            rec = chat_record(data, chat_id) or {}
            mk = monthly_ops.month_key_from_date(
                datetime.now(get_tz(rec.get("timezone", DEFAULT_TZ))).date()
            )
            waiting_monthly_plan[uid] = {"chat_id": chat_id, "month_key": mk}
            await callback.message.answer(
                f"<b>План на месяц</b> · {escape(str(rec.get('title', chat_id)))}\n"
                f"<i>{escape(monthly_ops.month_label_ru(mk, genitive=True))}</i>\n\n"
                "Введите целевую <b>выручку на месяц</b> (число).",
                parse_mode="HTML",
            )
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        if action == "mpick":
            await _show_morning_status(callback.message, uid, chat_id)
        elif action == "epick":
            await _show_evening_status(callback.message, uid, chat_id)
        else:
            waiting_staff_out[uid] = chat_id
            await callback.message.answer(
                "Зафиксировать <b>уход сотрудника</b> с точки.\n"
                "Напишите комментарий (роль/смена, без ФИО) или «—».",
                parse_mode="HTML",
            )
        return

    if action == "mpub" and len(parts) > 2:
        chat_id = int(parts[2])
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        data = await load_data()
        rec = chat_record(data, chat_id) or {}
        day = (
            parts[3]
            if len(parts) > 3 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[3])
            else ops_day.today_key(rec.get("timezone", DEFAULT_TZ))
        )
        result, detail = await _post_morning_to_group(data, chat_id, day)
        await save_data(data)
        toast = {
            "ok": "Опубликовано",
            "incomplete": "Сначала чек-листы утра и план",
            "already_posted": "План уже в чате",
            "send_failed": "Не удалось отправить в группу",
        }.get(result, "Ошибка публикации")
        await callback.answer(toast, show_alert=result in ("send_failed", "incomplete"))
        if result == "ok":
            await callback.message.answer("✅ План дня опубликован в группу.")
        elif result == "send_failed":
            hint = (
                "Проверьте, что бот добавлен в группу и может писать сообщения."
            )
            if detail and is_global_admin(uid):
                hint += f"\n\n<code>{escape(detail[:300])}</code>"
            await callback.message.answer(hint, parse_mode="HTML")
        elif result == "incomplete":
            if detail == "morning_checklists":
                await callback.message.answer(
                    "Сначала отметьте утренние чек-листы "
                    f"({pulse_model.BTN_CHECKLISTS} → Официанты/Хосты/Менеджер).",
                    parse_mode="HTML",
                )
            else:
                await callback.message.answer(
                    "Черновик плана не найден. Заполните план ещё раз.",
                    parse_mode="HTML",
                )
        return

    if action == "mcont" and len(parts) > 3:
        chat_id = int(parts[2])
        day = parts[3]
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        await _show_morning_status(
            callback.message, uid, chat_id, skip_opening_gate=True
        )
        return

    if action == "econt" and len(parts) > 3:
        chat_id = int(parts[2])
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        await _show_evening_status(
            callback.message, uid, chat_id, skip_closing_gate=True
        )
        return

    if action == "epub" and len(parts) > 2:
        chat_id = int(parts[2])
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        data = await load_data()
        rec = chat_record(data, chat_id) or {}
        day = (
            parts[3]
            if len(parts) > 3 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[3])
            else _ops_evening_day(data, chat_id)
        )
        result, detail = await _post_evening_to_group(data, chat_id, day)
        await save_data(data)
        toast = {
            "ok": "Опубликовано",
            "incomplete": "Сначала закройте смену полностью",
            "already_posted": "Итог уже в чате",
            "send_failed": "Не удалось отправить в группу",
        }.get(result, "Ошибка публикации")
        await callback.answer(toast, show_alert=result in ("send_failed", "incomplete"))
        if result == "ok":
            await _send_daily_digest_to_managers(data, chat_id, day)
            await callback.message.answer("✅ Итоги дня опубликованы в группу.")
        elif result == "send_failed":
            hint = (
                "Проверьте, что бот добавлен в группу и может писать сообщения."
            )
            if detail and is_global_admin(uid):
                hint += f"\n\n<code>{escape(detail[:300])}</code>"
            await callback.message.answer(hint, parse_mode="HTML")
        elif result == "incomplete":
            if detail == "closing_checklist":
                await callback.message.answer(
                    "Сначала отметьте все пункты <b>закрытия смены</b> "
                    f"({pulse_model.BTN_CHECKLISTS} → Закрытие дня).",
                    parse_mode="HTML",
                )
            else:
                await callback.message.answer(
                    "Заполните закрытие: выручка, комментарий к итогам и "
                    f"<b>{escape(ops_day.SHIFT_SUMMARY_LABEL)}</b>.",
                    parse_mode="HTML",
                )
        return

    if action in ("medit", "eedit") and len(parts) > 2:
        chat_id = int(parts[2])
        await callback.answer()
        if action == "medit":
            waiting_ops_morning[uid] = {"chat_id": chat_id, "step": "revenue_plan", "data": {}}
            await callback.message.answer(
                "Шаг 1/3: <b>план по выручке</b> (число).", parse_mode="HTML"
            )
        else:
            waiting_ops_evening[uid] = {
                "chat_id": chat_id,
                "step": "revenue",
                "data": {},
                "day": _ops_evening_day(data, chat_id),
            }
            await callback.message.answer(
                "Шаг 1/3: <b>фактическая выручка</b>.", parse_mode="HTML"
            )
        return

    if action == "tdone" and len(parts) > 2:
        tid = parts[2]
        if ops_day.complete_task(data, tid, uid):
            await save_data(data)
            await callback.answer("Задание выполнено")
            tasks = ops_day.tasks_for_user(data, uid)
            await callback.message.answer(
                ops_day.format_tasks_list(tasks, data),
                parse_mode="HTML",
                reply_markup=ops_day.tasks_keyboard(tasks),
            )
        else:
            await callback.answer("Не найдено", show_alert=True)
        return

    if action == "anew":
        if not ops_day.can_assign_tasks(data, uid, is_global_admin=is_global_admin(uid)):
            await callback.answer("Нет прав", show_alert=True)
            return
        locs = ops_day.list_assignable_locations(
            data, uid, is_global_admin=is_global_admin(uid)
        )
        if not locs:
            await callback.answer("Нет точек", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            "<b>На какую точку?</b>",
            parse_mode="HTML",
            reply_markup=ops_day.assign_task_locations_keyboard(locs),
        )
        return

    if action == "aloc" and len(parts) > 2:
        if not ops_day.can_assign_tasks(data, uid, is_global_admin=is_global_admin(uid)):
            await callback.answer("Нет прав", show_alert=True)
            return
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if not await _manager_can_access_chat(data, uid, chat_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        waiting_task_assign[uid] = {"chat_id": chat_id}
        title = ops_day.chat_title(data, chat_id)
        await callback.answer()
        await callback.message.answer(
            f"Задание для <b>{escape(title)}</b> — текст (до 500 символов):",
            parse_mode="HTML",
        )
        return

    await callback.answer()


async def _manager_actor_label(user) -> str:
    if not user:
        return "менеджер"
    if getattr(user, "username", None):
        return f"@{user.username}"
    parts = [
        (getattr(user, "first_name", None) or "").strip(),
        (getattr(user, "last_name", None) or "").strip(),
    ]
    name = " ".join(p for p in parts if p).strip()
    return name or f"id {user.id}"


async def _apply_problem_status_change(
    message: Message,
    uid: int,
    problem_id: str,
    status: str,
    comment: str | None,
) -> None:
    data = await load_data()
    prob = await problems_pulse.update_problem_status(
        data, problem_id, status, comment
    )
    if not prob:
        await message.answer("Не удалось обновить статус темы.")
        return
    await save_data(data)
    st_ru = problems_pulse.STATUS_RU.get(status, status)
    actor = await _manager_actor_label(message.from_user)
    await message.answer(
        f"Готово: <b>{escape(prob.title)}</b> → {escape(st_ru)}\n"
        f"<i>Другие менеджеры точки уже видят это обновление.</i>",
        parse_mode="HTML",
        reply_markup=pulse_model.manager_menu_reply_markup(),
    )
    if status in (
        problems_pulse.STATUS_IN_PROGRESS,
        problems_pulse.STATUS_RESOLVED,
    ):
        await _post_problem_to_group(
            prob.restaurant_chat_id,
            problems_pulse.format_group_status_post(prob),
        )
    rec = chat_record(data, prob.restaurant_chat_id) or {}
    title = str(rec.get("title", prob.restaurant_chat_id))
    mgr_note = problems_pulse.format_manager_peer_status_update(
        prob,
        restaurant_title=title,
        actor_label=actor,
    )
    # Текст + актуальная карточка с кнопками под новый статус
    admins = _chat_managers_only(data, prob.restaurant_chat_id)
    kb = problems_pulse.problem_card_keyboard(
        prob.id, prob.status, prob.restaurant_chat_id
    )
    for mid in admins:
        if mid == uid:
            continue
        manager_problem_chat[mid] = prob.restaurant_chat_id
        try:
            await bot.send_message(
                mid,
                mgr_note,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception as e:
            print(f"[notify-manager-status] {mid}: {e}")
    manager_problem_chat[uid] = prob.restaurant_chat_id


@dp.message(F.text == pulse_model.BTN_SUPPORT, F.chat.type == "private")
async def support_button_handler(message: Message) -> None:
    """Поддержка — любой пользователь в личке (сотрудник или менеджер)."""
    uid = message.from_user.id
    if await manager_ui_for_user(uid):
        mk = pulse_model.manager_menu_reply_markup()
    else:
        data = await load_data()
        show_tr = training_materials.staff_chat_id(data, uid) is not None
        mk = pulse_model.support_only_reply_markup(show_training=show_tr)
    await message.answer(
        pulse_model.text_support(SUPPORT_USERNAME or None),
        parse_mode="HTML",
        reply_markup=mk,
        disable_web_page_preview=True,
    )


@dp.message(F.text == pulse_model.BTN_TRAINING, F.chat.type == "private")
async def training_staff_button_handler(message: Message) -> None:
    await _show_training_staff_menu(message, message.from_user.id)


@dp.message(F.document, F.chat.type == "private")
async def training_document_handler(message: Message) -> None:
    uid = message.from_user.id
    if await _audit_ingest_media(message, uid):
        return
    flow = waiting_training_upload.get(uid)
    if not flow:
        return
    doc = message.document
    if not doc:
        return
    chat_id = int(flow["chat_id"])
    folder_id = str(flow["folder_id"])
    data = await load_data()
    if not await _manager_can_access_chat(data, uid, chat_id):
        waiting_training_upload.pop(uid, None)
        await message.answer("Нет доступа.")
        return
    rec = chat_record(data, chat_id) or {}
    title = (message.caption or doc.file_name or "Материал").strip()
    ftype = "document"
    if doc.mime_type and doc.mime_type.startswith("video/"):
        ftype = "video"
    elif doc.mime_type and doc.mime_type.startswith("image/"):
        ftype = "photo"
    ok, err = training_materials.add_file(
        rec,
        folder_id=folder_id,
        title=title,
        file_id=doc.file_id,
        file_type=ftype,
        by_uid=uid,
    )
    if not ok:
        await message.answer(err or "Не удалось сохранить.")
        return
    await save_data(data)
    waiting_training_upload.pop(uid, None)
    await message.answer(
        f"✅ Файл «<b>{escape(title[:80])}</b>» добавлен.",
        parse_mode="HTML",
        reply_markup=training_materials.manager_folder_keyboard(chat_id, folder_id),
    )


@dp.message(AdminCommandsFilter())
async def admin_commands_handler(message: Message) -> None:
    uid = message.from_user.id
    parts = pulse_model.admin_commands_reference_chunks()
    mk = _manager_menu_markup(uid)
    for i, chunk in enumerate(parts):
        await message.answer(
            chunk,
            parse_mode="HTML",
            reply_markup=mk if i == len(parts) - 1 else None,
        )


@dp.message(ChefMenuFilter())
async def chef_menu_handler(message: Message) -> None:
    t = (message.text or "").strip()
    uid = message.from_user.id
    waiting_chef_stop.pop(uid, None)
    waiting_chef_survey_comment.pop(uid, None)

    if t == pulse_model.BTN_RATE_SHIFT:
        await _handle_menu_rate_shift(message, uid)
    elif t == pulse_model.BTN_FOLDER_SHIFT_STOP:
        await message.answer(
            "<b>Стоп-лист</b>\n"
            f"· <b>{pulse_model.BTN_STOP_LIST}</b> — задать или заменить (в группу)\n"
            f"· <b>{pulse_model.BTN_STOP_ADD}</b> — дописать позиции\n"
            f"· <b>{pulse_model.BTN_STOP_CURRENT}</b> — только посмотреть",
            parse_mode="HTML",
            reply_markup=pulse_model.chef_menu_stop_markup(),
        )
    elif t == pulse_model.BTN_CHEF_BACK:
        await message.answer(
            "Меню шефа.",
            reply_markup=pulse_model.chef_menu_reply_markup(),
        )
    elif t == pulse_model.BTN_STOP_LIST:
        await _handle_menu_stop_list(message, uid, mode="replace")
    elif t == pulse_model.BTN_STOP_ADD:
        await _handle_menu_stop_list(message, uid, mode="append")
    elif t == pulse_model.BTN_STOP_CURRENT:
        await _handle_menu_stop_current(message, uid)
    elif t == pulse_model.BTN_SUPPORT:
        await support_button_handler(message)


@dp.message(ManagerMenuFilter())
async def manager_menu_handler(message: Message) -> None:
    t = (message.text or "").strip()
    uid = message.from_user.id
    waiting_for_comment.discard(uid)
    user_pending_problem.pop(uid, None)
    waiting_signal_title.pop(uid, None)
    waiting_ops_morning.pop(uid, None)
    waiting_ops_evening.pop(uid, None)
    waiting_chef_stop.pop(uid, None)
    waiting_ops_broadcast.pop(uid, None)
    waiting_checklist_add.pop(uid, None)
    waiting_checklist_remark.pop(uid, None)
    waiting_chef_survey_comment.pop(uid, None)
    waiting_monthly_plan.pop(uid, None)
    waiting_staff_out.pop(uid, None)
    waiting_task_assign.pop(uid, None)
    waiting_staff_assign.pop(uid, None)
    waiting_staff_remove.pop(uid, None)
    waiting_training_folder.pop(uid, None)
    waiting_training_upload.pop(uid, None)
    waiting_evening_backdate.pop(uid, None)
    waiting_reminder_custom.pop(uid, None)
    me = await bot.get_me()
    ga = is_global_admin(uid)
    data_menu = await load_data()
    happiness_only = (not ga) and pulse_model.is_happiness_manager_only(data_menu, uid)
    show_ai_audit = ga or pulse_model.has_ai_auditor_access(data_menu, uid)
    mk_root = pulse_model.manager_menu_root_markup(
        show_inbox=ga,
        show_commands=ga,
        show_ai_audit=show_ai_audit,
        happiness_only=happiness_only,
    )

    if t in (pulse_model.BTN_AUDIT_FINISH, pulse_model.BTN_AUDIT_CANCEL, pulse_model.BTN_AI_AUDIT):
        if t == pulse_model.BTN_AI_AUDIT:
            await _offer_ai_audit(message, uid)
            return
        if t == pulse_model.BTN_AUDIT_CANCEL:
            ai_auditor.cancel_session(uid)
            await message.answer("Аудит отменён.", reply_markup=mk_root)
            return
        if t == pulse_model.BTN_AUDIT_FINISH:
            await _finish_ai_audit(message, uid)
            return

    if t == pulse_model.BTN_FOLDER_INBOX:
        if not is_global_admin(uid):
            await message.answer("Папка «Сводки» — только для главного администратора.")
            return
        data = await load_data()
        scope = await _ops_scope(data, uid)
        if not scope:
            await message.answer("Нет подключённых точек.", reply_markup=mk_root)
            return
        await message.answer(
            "<b>📁 Сводки по точкам</b>\n\n"
            "Выберите локацию — отчёты и сводки <b>по запросу</b>, "
            "без автоматической рассылки в личку.",
            parse_mode="HTML",
            reply_markup=report_pulse.admin_inbox_locations_keyboard(scope),
        )
        return

    if t == pulse_model.BTN_FOLDER_ANALYTICS:
        await message.answer(
            "<b>Аналитика</b>\nОтчёты и «Горящие вопросы» по точке.",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_analytics_markup(),
        )
        return
    if t == pulse_model.BTN_FOLDER_SHIFT:
        if happiness_only:
            await message.answer(
                "Папка «Смена» — для менеджеров точки. "
                "Вам доступны аналитика и ИИ-аудит.",
                reply_markup=mk_root,
            )
            return
        await message.answer(
            "<b>Смена</b>\n"
            f"· <b>{pulse_model.BTN_FOLDER_SHIFT_DAY}</b> — план, закрытие, чек-листы\n"
            f"· <b>{pulse_model.BTN_FOLDER_SHIFT_STOP}</b> — стоп-лист\n"
            f"· <b>{pulse_model.BTN_FOLDER_SHIFT_KITCHEN}</b> — оценка кухни\n"
            f"· <b>{pulse_model.BTN_FOLDER_SHIFT_PLAN}</b> — месяц, кадры, задания",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_shift_root_markup(),
        )
        return
    if t == pulse_model.BTN_FOLDER_SHIFT_DAY:
        await message.answer(
            "<b>День</b>\nПлан, закрытие, чек-листы, сообщение шефу и менеджерам.",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_shift_day_markup(),
        )
        return
    if t == pulse_model.BTN_FOLDER_SHIFT_STOP:
        await message.answer(
            "<b>Стоп-лист</b>\n"
            f"· <b>{pulse_model.BTN_STOP_LIST}</b> — задать или заменить\n"
            f"· <b>{pulse_model.BTN_STOP_ADD}</b> — дописать\n"
            f"· <b>{pulse_model.BTN_STOP_CURRENT}</b> — только посмотреть",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_shift_stop_markup(),
        )
        return
    if t == pulse_model.BTN_FOLDER_SHIFT_KITCHEN:
        await message.answer(
            "<b>Кухня</b>\nОценка смены кухни (напоминание шефу в личку).",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_shift_kitchen_markup(),
        )
        return
    if t == pulse_model.BTN_FOLDER_SHIFT_PLAN:
        await message.answer(
            "<b>Планирование</b>\nПлан на месяц, уход кадра, задания смене.",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_shift_plan_markup(),
        )
        return
    if t == pulse_model.BTN_SHIFT_BACK:
        await message.answer(
            "<b>Смена</b>",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_shift_root_markup(),
        )
        return
    if t == pulse_model.BTN_FOLDER_MORE:
        await message.answer(
            "<b>Настройки</b>\nПодписка, поддержка, подключение точки.",
            parse_mode="HTML",
            reply_markup=await _staff_assign_more_markup(uid),
        )
        return
    if t == pulse_model.BTN_STAFF_ASSIGN:
        await _show_staff_assign_menu(message, uid)
        return
    if t == pulse_model.BTN_STAFF_REMOVE:
        await _show_staff_remove_menu(message, uid)
        return
    if t == pulse_model.BTN_TRAINING_MGR:
        data = await load_data()
        scope = await _ops_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:trpick", title="Материалы"
        )
        if chat_id is not None:
            await _show_training_manager_menu(message, uid, chat_id)
        return
    if t == pulse_model.BTN_MENU_HOME:
        await message.answer(
            "Главное меню Pulse Team.",
            reply_markup=mk_root,
        )
        return

    # Менеджер по счастью — только аналитика / аудит / материалы / ещё
    if happiness_only and t not in (
        pulse_model.BTN_FOLDER_ANALYTICS,
        pulse_model.BTN_REPORT,
        pulse_model.BTN_SIGNALS,
        pulse_model.BTN_AI_AUDIT,
        pulse_model.BTN_TRAINING_MGR,
        pulse_model.BTN_FOLDER_MORE,
        pulse_model.BTN_SUBSCRIPTION,
        pulse_model.BTN_SUPPORT,
        pulse_model.BTN_MENU_HOME,
        pulse_model.BTN_AUDIT_FINISH,
        pulse_model.BTN_AUDIT_CANCEL,
    ):
        await message.answer(
            "Эта функция для менеджеров точки. Вам доступны аналитика и ИИ-аудит.",
            reply_markup=mk_root,
        )
        return

    if t == pulse_model.BTN_REPORT:
        data = await load_data()
        scope = report_pulse.chat_scope_for_user(
            data, uid, is_global_admin=is_global_admin(uid)
        )
        if not scope:
            await message.answer(
                "Нет подключённых точек для отчёта.",
                reply_markup=pulse_model.manager_menu_reply_markup(),
            )
            return
        if len(scope) > 1:
            await message.answer(
                "<b>Отчёт</b>\n\nВыберите точку:",
                parse_mode="HTML",
                reply_markup=report_pulse.report_location_keyboard(
                    scope, include_all=is_global_admin(uid)
                ),
            )
        else:
            user_report_pick[uid] = scope[0][0]
            await message.answer(
                "<b>Отчёт</b>\n\nЧто показать?",
                parse_mode="HTML",
                reply_markup=report_pulse.report_department_keyboard(scope[0][0]),
            )
    elif t == pulse_model.BTN_SUBSCRIPTION:
        data = await load_data()
        if is_global_admin(uid) and not pulse_model.manager_profiles(data, uid):
            orgs = data.get("organizations", {})
            if not orgs:
                text = "Организаций пока нет. Создайте: <code>/create_org Название</code>"
            else:
                parts_sub = ["<b>Подписки (глобальный админ)</b>\n"]
                for oid, org in sorted(orgs.items(), key=lambda x: x[0]):
                    parts_sub.append(
                        f"• <code>{escape(str(oid))}</code> — <b>{escape(str(org.get('name', oid)))}</b> "
                        f"· <code>{escape(str(org.get('subscription', pulse_model.SUB_ACTIVE)))}</code>\n"
                    )
                text = "\n".join(parts_sub)
        else:
            text = pulse_model.text_subscription_status(data, uid)
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_reply_markup(),
        )
    elif t == pulse_model.BTN_CONNECT:
        data = await load_data()
        import miniapp_ops as _mops

        guide = _mops.connect_guide(
            data,
            uid,
            is_global_admin=is_global_admin(uid),
            bot_username=me.username or "",
        )
        await message.answer(
            pulse_model.text_connect_point(
                me.username, org_commands=guide.get("commands") or []
            ),
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_reply_markup(),
            disable_web_page_preview=True,
        )
    elif t == pulse_model.BTN_REMINDERS:
        data = await load_data()
        scope = report_pulse.chat_scope_for_user(
            data, uid, is_global_admin=is_global_admin(uid)
        )
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:rmpick", title="Напоминания"
        )
        if chat_id is not None:
            await _show_reminders_panel(message, uid, chat_id)
    elif t == pulse_model.BTN_SIGNALS:
        await cmd_problems(message)
    elif t == pulse_model.BTN_DAY_PLAN:
        data = await load_data()
        scope = await _ops_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:mpick", title="План дня"
        )
        if chat_id is not None:
            await _show_morning_status(message, uid, chat_id)
    elif t == pulse_model.BTN_DAY_CLOSE:
        data = await load_data()
        scope = await _ops_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:epick", title="Закрытие дня"
        )
        if chat_id is not None:
            await _show_evening_status(message, uid, chat_id)
    elif t == pulse_model.BTN_DAY_CLOSE_PAST:
        data = await load_data()
        scope = await _ops_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:bpick", title="Закрыть за дату"
        )
        if chat_id is not None:
            waiting_evening_backdate[uid] = {"chat_id": chat_id, "step": "date"}
            rec = chat_record(data, chat_id) or {}
            await message.answer(
                f"<b>Закрытие за дату</b> · {escape(str(rec.get('title', chat_id)))}\n\n"
                "Отправьте дату смены: <code>ДД.ММ.ГГГГ</code> или <code>ГГГГ-ММ-ДД</code>.\n"
                "<i>Можно закрыть прошлый день, если забыли открыть план — "
                "утренний план для той даты не обязателен.</i>",
                parse_mode="HTML",
            )
    elif t == pulse_model.BTN_STOP_LIST:
        await _handle_menu_stop_list(message, uid, mode="replace")
    elif t == pulse_model.BTN_STOP_ADD:
        await _handle_menu_stop_list(message, uid, mode="append")
    elif t == pulse_model.BTN_STOP_CURRENT:
        await _handle_menu_stop_current(message, uid)
    elif t == pulse_model.BTN_RATE_SHIFT:
        await _handle_menu_rate_shift(message, uid)
    elif t == pulse_model.BTN_OPS_BROADCAST:
        data_bc = await load_data()
        if not (ga or pulse_model.has_manager_access(data_bc, uid)):
            await message.answer("Доступно менеджерам точки.")
            return
        await _handle_menu_ops_broadcast(message, uid)
    elif t == pulse_model.BTN_CHECKLISTS:
        data = await load_data()
        scope = await _ops_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:clpick", title="Чек-листы смены"
        )
        if chat_id is not None:
            rec = chat_record(data, chat_id) or {}
            await message.answer(
                f"<b>{pulse_model.BTN_CHECKLISTS}</b> · "
                f"{escape(str(rec.get('title', chat_id)))}\n\n"
                "Настройте утренние чек-листы (официанты/хосты/менеджер) и закрытие.",
                parse_mode="HTML",
                reply_markup=shift_checklists.checklist_menu_keyboard(chat_id),
            )
    elif t == pulse_model.BTN_MONTH_PLAN:
        data = await load_data()
        scope = await _ops_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:mplanpick", title="План на месяц"
        )
        if chat_id is not None:
            rec = chat_record(data, chat_id) or {}
            mk = monthly_ops.month_key_from_date(
                datetime.now(get_tz(rec.get("timezone", DEFAULT_TZ))).date()
            )
            waiting_monthly_plan[uid] = {"chat_id": chat_id, "month_key": mk}
            await message.answer(
                f"<b>План на месяц</b> · {escape(str(rec.get('title', chat_id)))}\n"
                f"<i>{escape(monthly_ops.month_label_ru(mk, genitive=True))}</i>\n\n"
                "Введите целевую <b>выручку на месяц</b> (число, например 18000000).",
                parse_mode="HTML",
            )
    elif t == pulse_model.BTN_STAFF_OUT:
        data = await load_data()
        scope = await _ops_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:spick", title="Уход кадра"
        )
        if chat_id is not None:
            waiting_staff_out[uid] = chat_id
            await message.answer(
                "Зафиксировать <b>уход сотрудника</b>.\n"
                "Комментарий (роль/смена, без ФИО) или «—».",
                parse_mode="HTML",
            )
    elif t == pulse_model.BTN_TASKS:
        data = await load_data()
        tasks = ops_day.tasks_for_user(data, uid)
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        extra_rows: list[list] = []
        if ops_day.can_assign_tasks(data, uid, is_global_admin=is_global_admin(uid)):
            extra_rows.append(
                [
                    InlineKeyboardButton(
                        text="➕ Назначить задание",
                        callback_data="ops:anew",
                    )
                ]
            )
        kb = ops_day.tasks_keyboard(tasks)
        if extra_rows:
            merged = (kb.inline_keyboard if kb else []) + extra_rows
            kb = InlineKeyboardMarkup(inline_keyboard=merged)
        await message.answer(
            ops_day.format_tasks_list(tasks, data),
            parse_mode="HTML",
            reply_markup=kb,
        )


@dp.callback_query(F.data.startswith("adm:"))
async def admin_inbox_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    if not is_global_admin(uid):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    data = await load_data()
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "loc" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        rec = chat_record(data, chat_id) or {}
        title = escape(str(rec.get("title", chat_id)))
        await callback.answer()
        await callback.message.answer(
            f"<b>📁 {title}</b>\n\nЧто показать?",
            parse_mode="HTML",
            reply_markup=report_pulse.admin_inbox_point_keyboard(chat_id),
        )
        return

    if len(parts) < 3:
        await callback.answer()
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return
    rec = chat_record(data, chat_id) or {}
    tz_name = str(rec.get("timezone", DEFAULT_TZ))

    if action == "digest":
        await callback.answer("Собираю сводку…")
        text = await _admin_digest_for_chat(data, chat_id)
        await callback.message.answer(text, parse_mode="HTML")
        return

    if action == "cal":
        await callback.answer()
        user_report_pick[uid] = str(chat_id)
        await callback.message.answer(
            "<b>Отчёт за месяц</b>\n\nВыберите месяц:",
            parse_mode="HTML",
            reply_markup=report_pulse.report_calendar_month_keyboard(tz_name),
        )
        return

    if action == "rep3w":
        await callback.answer("Собираю отчёт…")
        user_report_pick[uid] = str(chat_id)
        result = await report_pulse.build_reports_for_manager(
            data,
            uid,
            report_pulse.PERIOD_MONTH,
            is_global_admin=True,
            tz_name=tz_name,
            jsonl_path=FEEDBACK_LOG_PATH,
            selected_chat=str(chat_id),
        )
        await _send_report_chunks(
            callback.message,
            uid,
            result.parts,
            offer_pdf=True,
            export_ctx={
                "period": report_pulse.PERIOD_MONTH,
                "pick": str(chat_id),
                "dept": chef_survey.DEPARTMENT_FLOOR,
                "messages": result.messages,
                "events": result.events,
                "problem_labels": result.problem_labels,
            },
        )
        return

    if action == "repick":
        await callback.answer()
        user_report_pick[uid] = str(chat_id)
        await callback.message.answer(
            "<b>Отчёт</b>\n\nЧто показать?",
            parse_mode="HTML",
            reply_markup=report_pulse.report_department_keyboard(chat_id),
        )
        return

    await callback.answer()


@dp.callback_query(F.data.startswith("report_r:"))
async def report_location_handler(callback: CallbackQuery) -> None:
    if callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа к отчётам.", show_alert=True)
        return
    pick = (callback.data or "").split(":", 1)[-1]
    if pick == "__skip__":
        await callback.answer("Смотрите полный список чатов: /admin", show_alert=True)
        return
    scope = report_pulse.chat_scope_for_user(
        data, uid, is_global_admin=is_global_admin(uid)
    )
    if pick != "all" and not any(str(cid) == pick for cid, _ in scope):
        await callback.answer("Точка не найдена.", show_alert=True)
        return
    if pick == "all" and not is_global_admin(uid):
        await callback.answer("Доступно только главному администратору.", show_alert=True)
        return
    user_report_pick[uid] = pick
    await callback.answer()
    dept_cid = pick if pick != "all" else (scope[0][0] if scope else pick)
    await callback.message.answer(
        "<b>Отчёт</b>\n\nЧто показать?",
        parse_mode="HTML",
        reply_markup=report_pulse.report_department_keyboard(dept_cid),
    )


@dp.callback_query(F.data.startswith("report_d:"))
async def report_department_handler(callback: CallbackQuery) -> None:
    if callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа к отчётам.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    dept = parts[1]
    pick = parts[2]
    if dept not in (
        chef_survey.DEPARTMENT_FLOOR,
        chef_survey.DEPARTMENT_KITCHEN,
        chef_survey.DEPARTMENT_ALL,
    ):
        await callback.answer()
        return
    scope = report_pulse.chat_scope_for_user(
        data, uid, is_global_admin=is_global_admin(uid)
    )
    if pick != "all" and not any(str(cid) == pick for cid, _ in scope):
        await callback.answer("Точка не найдена.", show_alert=True)
        return
    user_report_pick[uid] = pick
    user_report_dept[uid] = dept
    await callback.answer()
    dept_title = chef_survey.department_title(dept)
    await callback.message.answer(
        f"<b>Отчёт · {escape(dept_title)}</b>\n\nВыберите период:",
        parse_mode="HTML",
        reply_markup=report_pulse.report_period_keyboard(),
    )


@dp.callback_query(F.data.startswith("report_p:"))
async def report_period_handler(callback: CallbackQuery) -> None:
    if callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа к отчётам.", show_alert=True)
        return
    period = (callback.data or "").split(":", 1)[-1]
    scope = report_pulse.chat_scope_for_user(
        data, uid, is_global_admin=is_global_admin(uid)
    )
    pick = user_report_pick.get(uid)
    if len(scope) > 1 and not pick:
        await callback.answer(
            "Сначала нажмите «Отчёт» и выберите точку.",
            show_alert=True,
        )
        return
    if len(scope) == 1:
        pick = scope[0][0]

    if period == report_pulse.PERIOD_CALENDAR:
        await callback.answer()
        tz_name = DEFAULT_TZ
        if pick:
            rec = chat_record(data, int(pick)) or {}
            tz_name = str(rec.get("timezone", DEFAULT_TZ))
        await callback.message.answer(
            "<b>Отчёт за календарный месяц</b>\n\nВыберите месяц:",
            parse_mode="HTML",
            reply_markup=report_pulse.report_calendar_month_keyboard(tz_name),
        )
        return

    if period not in (
        report_pulse.PERIOD_SHIFT,
        report_pulse.PERIOD_WEEK,
        report_pulse.PERIOD_MONTH,
    ):
        await callback.answer()
        return

    dept = user_report_dept.get(uid, chef_survey.DEPARTMENT_FLOOR)
    await callback.answer("Собираю отчёт…")
    try:
        result = await report_pulse.build_reports_for_manager(
            data,
            uid,
            period,
            is_global_admin=is_global_admin(uid),
            tz_name=DEFAULT_TZ,
            jsonl_path=FEEDBACK_LOG_PATH,
            selected_chat=pick,
            department=dept,
        )
    except Exception as e:
        print(f"[report-on-demand] uid={uid} period={period} pick={pick!r}", repr(e))
        await callback.message.answer(
            "Не удалось собрать отчёт. Попробуйте ещё раз или напишите в поддержку.",
            reply_markup=_manager_menu_markup(uid),
        )
        return
    user_report_pick.pop(uid, None)
    user_report_dept.pop(uid, None)
    await _send_report_chunks(
        callback.message,
        uid,
        result.parts,
        offer_pdf=True,
        export_ctx={
            "period": period,
            "pick": pick,
            "dept": dept,
            "messages": result.messages,
            "events": result.events,
            "problem_labels": result.problem_labels,
        },
    )


@dp.callback_query(F.data == "report_pdf:export")
async def report_pdf_export_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    ctx = user_report_export_ctx.get(uid)
    if not ctx:
        await callback.answer(
            "Сначала запросите отчёт — кнопка PDF появится под сводкой.",
            show_alert=True,
        )
        return
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer("Готовлю PDF…")
    period = ctx.get("period", report_pulse.PERIOD_WEEK)
    pick = ctx.get("pick")
    dept = ctx.get("dept", chef_survey.DEPARTMENT_FLOOR)
    month_key = ctx.get("month_key")
    messages = ctx.get("messages")
    events = ctx.get("events")
    problem_labels = ctx.get("problem_labels")
    try:
        if month_key:
            result, _kb = await report_pulse.build_calendar_month_reports(
                data,
                uid,
                month_key,
                is_global_admin=is_global_admin(uid),
                tz_name=DEFAULT_TZ,
                jsonl_path=FEEDBACK_LOG_PATH,
                selected_chat=pick,
            )
            messages = result.messages
            events = result.events
            problem_labels = result.problem_labels
            plabel = month_key
        elif not messages:
            result = await report_pulse.build_reports_for_manager(
                data,
                uid,
                period,
                is_global_admin=is_global_admin(uid),
                tz_name=DEFAULT_TZ,
                jsonl_path=FEEDBACK_LOG_PATH,
                selected_chat=pick,
                department=dept,
            )
            messages = result.messages
            events = result.events
            problem_labels = result.problem_labels
            plabel = report_pulse.PERIOD_TITLES.get(period, period)
        else:
            plabel = (
                month_key
                if month_key
                else report_pulse.PERIOD_TITLES.get(period, period)
            )
    except Exception as e:
        print(f"[report-pdf] uid={uid}", repr(e))
        await callback.message.answer("Не удалось собрать PDF.")
        return
    if not messages:
        await callback.message.answer("Нет данных для PDF.")
        return
    scope = report_pulse.chat_scope_for_user(
        data, uid, is_global_admin=is_global_admin(uid)
    )
    title = "Pulse Team"
    if pick and pick != "all":
        for cid, t in scope:
            if str(cid) == str(pick):
                title = t
                break
    elif scope:
        title = scope[0][1]
    scope_key = f"{pick or 'all'}:{period}:{month_key or ''}"
    comment_period = (
        report_pulse.PERIOD_CALENDAR if month_key else period
    )
    comments_html = ""
    if events:
        comments_html = report_pulse.build_pdf_comments_html(
            events,
            period=comment_period,
            scope_key=scope_key,
            problem_labels=problem_labels,
        )
    try:
        pdf_bytes = pdf_report.build_pdf_bytes(
            messages,
            title=f"Pulse Team — {title}",
            period_label=plabel,
            comments_html=comments_html,
        )
        fname = pdf_report.pdf_filename(title, plabel)
    except RuntimeError as e:
        code = str(e)
        if code == "fpdf2_missing":
            msg = (
                "PDF недоступен: на сервере не установлен fpdf2. "
                "Передеплойте бота после обновления requirements.txt."
            )
        elif code == "font_missing":
            msg = (
                "PDF недоступен: на сервере нет шрифта assets/DejaVuSans.ttf "
                "(нужен для кириллицы). Закоммитьте папку assets и передеплойте."
            )
        else:
            msg = "PDF временно недоступен."
        print(f"[report-pdf-build] uid={uid}", repr(e))
        await callback.message.answer(msg)
        return
    except Exception as e:
        print(f"[report-pdf-build] uid={uid}", repr(e))
        await callback.message.answer(
            f"Не удалось собрать PDF: {type(e).__name__}. Смотрите логи Railway."
        )
        return
    from aiogram.types import BufferedInputFile

    await callback.message.answer_document(
        BufferedInputFile(pdf_bytes, filename=fname),
        caption="📄 Сводка для руководства — можно переслать.",
    )


@dp.callback_query(F.data.startswith("report_cal:"))
async def report_calendar_month_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    month_key = (callback.data or "").split(":", 1)[-1]
    pick = user_report_pick.get(uid)
    scope = report_pulse.chat_scope_for_user(
        data, uid, is_global_admin=is_global_admin(uid)
    )
    if len(scope) > 1 and not pick:
        await callback.answer("Сначала выберите точку.", show_alert=True)
        return
    if len(scope) == 1:
        pick = scope[0][0]
    await callback.answer("Собираю месячный отчёт…")
    try:
        result, kb = await report_pulse.build_calendar_month_reports(
            data,
            uid,
            month_key,
            is_global_admin=is_global_admin(uid),
            tz_name=DEFAULT_TZ,
            jsonl_path=FEEDBACK_LOG_PATH,
            selected_chat=pick,
        )
    except Exception as e:
        print(f"[report-calendar] uid={uid} month={month_key}", repr(e))
        await callback.message.answer(
            "Не удалось собрать отчёт за месяц.",
            reply_markup=_manager_menu_markup(uid),
        )
        return
    dept = user_report_dept.get(uid, chef_survey.DEPARTMENT_FLOOR)
    user_report_pick.pop(uid, None)
    user_report_dept.pop(uid, None)
    await _send_report_chunks(
        callback.message,
        uid,
        result.parts,
        reply_markup=kb,
        offer_pdf=True,
        export_ctx={
            "period": report_pulse.PERIOD_CALENDAR,
            "pick": pick,
            "dept": dept,
            "month_key": month_key,
            "messages": result.messages,
            "events": result.events,
            "problem_labels": result.problem_labels,
        },
    )


@dp.callback_query(F.data.startswith("report_mplan:"))
async def report_monthly_plan_handler(callback: CallbackQuery) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    data = await load_data()
    if not (is_global_admin(uid) or pulse_model.has_manager_access(data, uid)):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    seg = (callback.data or "").split(":")
    if len(seg) < 3:
        await callback.answer()
        return
    month_key = seg[1]
    try:
        chat_id = int(seg[2])
    except ValueError:
        await callback.answer()
        return
    if not await _manager_can_access_chat(data, uid, chat_id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    waiting_monthly_plan[uid] = {"chat_id": chat_id, "month_key": month_key}
    rec = chat_record(data, chat_id) or {}
    await callback.message.answer(
        f"<b>План на месяц</b> · {escape(str(rec.get('title', chat_id)))}\n"
        f"<i>{escape(monthly_ops.month_label_ru(month_key, genitive=True))}</i>\n\n"
        "Введите целевую <b>выручку на месяц</b> (число).",
        parse_mode="HTML",
    )


@dp.message(F.text)
async def comment_handler(message: Message) -> None:
    if message.chat.type != "private":
        return
    if not message.text or message.text.startswith("/"):
        return
    user_id = message.from_user.id
    text_in = message.text.strip()

    if ai_auditor.get_active(user_id):
        if text_in in (
            pulse_model.BTN_AUDIT_FINISH,
            pulse_model.BTN_AUDIT_CANCEL,
            pulse_model.BTN_AI_AUDIT,
            pulse_model.BTN_MENU_HOME,
        ):
            pass  # обработает ManagerMenuFilter / ниже не дублируем
        elif await _audit_ingest_media(message, user_id):
            return

    if user_id in waiting_task_assign:
        assign = waiting_task_assign.pop(user_id)
        data = await load_data()
        if not ops_day.can_assign_tasks(
            data, user_id, is_global_admin=is_global_admin(user_id)
        ):
            await message.answer("Нет прав.")
            return
        body = text_in[:500]
        if len(body) < 3:
            waiting_task_assign[user_id] = assign
            await message.answer("Минимум 3 символа.")
            return
        chat_id = int(assign["chat_id"])
        title = ops_day.chat_title(data, chat_id)
        created = ops_day.assign_tasks_to_location(
            data,
            from_uid=user_id,
            chat_id=chat_id,
            text=body,
        )
        await save_data(data)
        await message.answer(
            f"✅ Задание для <b>{escape(title)}</b> назначено.",
            reply_markup=pulse_model.manager_menu_reply_markup(),
            parse_mode="HTML",
        )
        for task in created:
            try:
                await bot.send_message(
                    int(task["to_uid"]),
                    f"📌 <b>Задание · {escape(title)}</b>\n\n{escape(body)}",
                    parse_mode="HTML",
                )
            except Exception as e:
                print(f"[task-notify] {e}")
        return

    if user_id in waiting_checklist_remark:
        flow = waiting_checklist_remark.pop(user_id)
        note = text_in.strip()
        if note == "—":
            data = await load_data()
            chat_id = int(flow["chat_id"])
            kind = flow["kind"]
            day = flow["day"]
            if not await _ops_can_access_chat(data, user_id, chat_id):
                await message.answer("Нет доступа.")
                return
            rec = chat_record(data, chat_id) or {}
            await message.answer(
                shift_checklists.format_checklist_prompt(
                    rec, str(rec.get("title", chat_id)), day, kind
                ),
                parse_mode="HTML",
                reply_markup=shift_checklists.run_checklist_keyboard(
                    rec, chat_id, day, kind
                ),
            )
            return
        if len(note) < 3 or len(note) > 500:
            waiting_checklist_remark[user_id] = flow
            await message.answer("Замечание: от 3 до 500 символов, или «—» для отмены.")
            return
        chat_id = int(flow["chat_id"])
        kind = flow["kind"]
        day = flow["day"]
        data = await load_data()
        if not await _ops_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        rec = chat_record(data, chat_id) or {}
        ok, err = shift_checklists.set_remark(rec, day, kind, user_id, note)
        if not ok:
            waiting_checklist_remark[user_id] = flow
            await message.answer(err or "Ошибка")
            return
        await save_data(data)
        await message.answer(
            "✅ Замечание сохранено.",
            parse_mode="HTML",
            reply_markup=shift_checklists.run_checklist_keyboard(
                rec, chat_id, day, kind
            ),
        )
        await message.answer(
            shift_checklists.format_checklist_prompt(
                rec, str(rec.get("title", chat_id)), day, kind
            ),
            parse_mode="HTML",
        )
        return

    if user_id in waiting_staff_assign:
        raw = text_in.replace(" ", "")
        if not raw.isdigit():
            await message.answer(
                "Нужен числовой Telegram ID (команда <code>/myid</code> у человека).",
                parse_mode="HTML",
            )
            return
        await _apply_staff_assign_from_state(message, user_id, int(raw))
        return

    if user_id in waiting_training_folder:
        chat_id = waiting_training_folder.pop(user_id)
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        rec = chat_record(data, chat_id) or {}
        ok, err, _fid = training_materials.add_folder(rec, text_in)
        if not ok:
            waiting_training_folder[user_id] = chat_id
            await message.answer(err or "Не удалось создать папку.")
            return
        await save_data(data)
        title = str(rec.get("title", chat_id))
        kb = onboarding_reels.patch_manager_menu_keyboard(
            training_materials.manager_menu_keyboard(chat_id, rec),
            chat_id,
        )
        await message.answer(
            f"✅ Папка «<b>{escape(text_in.strip()[:60])}</b>» создана.\n\n"
            + onboarding_reels.enrich_manager_menu_text(
                training_materials.format_manager_menu(rec, title)
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    if user_id in waiting_reminder_custom:
        flow = waiting_reminder_custom.pop(user_id)
        chat_id = int(flow["chat_id"])
        slot = int(flow["slot"])
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        t = parse_hhmm(text_in)
        if not t:
            waiting_reminder_custom[user_id] = flow
            await message.answer("Нужен формат <code>ЧЧ:ММ</code>, например <code>22:15</code>.", parse_mode="HTML")
            return
        rec = chat_record(data, chat_id)
        if not rec:
            await message.answer("Точка не найдена.")
            return
        _put_reminder_time(rec, slot, t)
        await save_data(data)
        title = str(rec.get("title", chat_id))
        await message.answer(
            f"✅ Время сохранено: <b>{escape(t)}</b>",
            parse_mode="HTML",
        )
        await _show_reminders_panel(message, user_id, chat_id)
        return

    if user_id in waiting_evening_backdate:
        flow = waiting_evening_backdate.pop(user_id)
        chat_id = int(flow["chat_id"])
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        rec = chat_record(data, chat_id) or {}
        tz_name = str(rec.get("timezone") or DEFAULT_TZ)
        day = _parse_backdate_text(text_in, tz_name)
        if not day:
            waiting_evening_backdate[user_id] = flow
            await message.answer(
                "Не понял дату. Пример: <code>15.06.2025</code> или <code>2025-06-15</code>.",
                parse_mode="HTML",
            )
            return
        try:
            from zoneinfo import ZoneInfo

            d = date.fromisoformat(day)
            today_d = datetime.now(ZoneInfo(tz_name)).date()
            if d > today_d:
                waiting_evening_backdate[user_id] = flow
                await message.answer("Дата не может быть в будущем.")
                return
            if (today_d - d).days > 62:
                waiting_evening_backdate[user_id] = flow
                await message.answer("Можно закрыть смену не старше 62 дней.")
                return
        except ValueError:
            waiting_evening_backdate[user_id] = flow
            await message.answer("Некорректная дата.")
            return
        evening = ops_day.get_evening(rec, day)
        if ops_day.evening_complete(evening):
            met = ops_day.PLAN_MET_RU.get(str(evening.get("plan_met")), "—")
            rev = ops_day.format_money(
                ops_day.parse_amount(str(evening.get("revenue") or ""))
            )
            await message.answer(
                f"За <b>{escape(day)}</b> уже есть закрытие: {escape(met)} · {escape(rev)}.\n"
                "Можно изменить через «Закрытие дня» → редактировать.",
                parse_mode="HTML",
            )
            return
        await _show_evening_status(
            message, user_id, chat_id, skip_closing_gate=True, day=day
        )
        return

    if user_id in waiting_staff_out:
        chat_id = waiting_staff_out.pop(user_id)
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        rec = chat_record(data, chat_id) or {}
        note = text_in if text_in not in ("—", "-") else ""
        await log_feedback_event(
            {
                "event": "staff_departure",
                "user_id": user_id,
                "restaurant_chat_id": chat_id,
                "restaurant_label": rec.get("title"),
                "organization_id": rec.get("organization_id"),
                "note": note,
            }
        )
        await message.answer(
            "Зафиксировано. Учтётся в отчёте (без публикации в группу).",
            reply_markup=pulse_model.manager_menu_reply_markup(),
        )
        return

    if user_id in waiting_monthly_plan:
        flow = waiting_monthly_plan.pop(user_id)
        chat_id = int(flow["chat_id"])
        month_key = str(flow.get("month_key") or "")
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        amount = ops_day.parse_amount(text_in)
        if not amount or amount <= 0:
            waiting_monthly_plan[user_id] = flow
            await message.answer("Введите число — план по выручке на месяц.")
            return
        monthly_ops.save_monthly_plan(
            data,
            chat_id,
            month_key=month_key,
            revenue_plan=amount,
            by_uid=user_id,
        )
        await save_data(data)
        rec = chat_record(data, chat_id) or {}
        label = monthly_ops.month_label_ru(month_key, genitive=True)
        await message.answer(
            f"✅ План на {escape(label)}: <b>{escape(ops_day.format_money(amount))}</b>\n"
            f"· {escape(str(rec.get('title', chat_id)))}",
            parse_mode="HTML",
            reply_markup=_manager_menu_markup(user_id),
        )
        return

    if user_id in waiting_chef_stop:
        flow = waiting_chef_stop.pop(user_id)
        chat_id = int(flow["chat_id"])
        mode = flow.get("mode", "replace")
        day = flow.get("day") or ops_day.today_key(DEFAULT_TZ)
        data = await load_data()
        if not await _ops_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        body = text_in.strip()
        if len(body) < 2:
            waiting_chef_stop[user_id] = flow
            await message.answer("Минимум 2 символа.")
            return
        rec = chat_record(data, chat_id) or {}
        day = flow.get("day") or ops_day.today_key(rec.get("timezone", DEFAULT_TZ))
        stop = ops_day.get_chef_stop(rec, day)
        existing = str(stop.get("text") or "").strip() if ops_day.chef_stop_has_content(stop) else ""
        if mode == "append":
            combined = f"{existing}\n{body}" if existing else body
            if len(combined) > 2000:
                waiting_chef_stop[user_id] = flow
                await message.answer(
                    f"Слишком длинно (макс. 2000). Сейчас {len(existing)} символов."
                )
                return
            was_set = bool(existing)
            ops_day.append_chef_stop(
                data, chat_id, day=day, addition=body, by_uid=user_id
            )
            tag = "дополнен" if was_set else "опубликован"
        else:
            if len(body) > 2000:
                waiting_chef_stop[user_id] = flow
                await message.answer("Слишком длинно (макс. 2000 символов).")
                return
            was_set = ops_day.chef_stop_has_content(stop)
            ops_day.save_chef_stop(
                data, chat_id, day=day, text=body, by_uid=user_id
            )
            tag = "обновлён" if was_set else "опубликован"
        result, detail = await _post_chef_stop_to_group(data, chat_id, day)
        await save_data(data)
        title = escape(str(rec.get("title", chat_id)))
        if result == "ok":
            mk = (
                _manager_menu_markup(user_id)
                if await manager_ui_for_user(user_id)
                else pulse_model.chef_menu_reply_markup()
            )
            await message.answer(
                f"✅ Стоп-лист {tag} · {title}\nСообщение отправлено в группу.",
                reply_markup=mk,
                parse_mode="HTML",
            )
        else:
            waiting_chef_stop[user_id] = flow
            hint = "Не удалось отправить в группу. Проверьте, что бот может писать в чат."
            if detail and is_global_admin(user_id):
                hint += f"\n\n<code>{escape(detail[:300])}</code>"
            await message.answer(hint, parse_mode="HTML")
        return

    if user_id in waiting_checklist_add:
        kind, chat_id = waiting_checklist_add.pop(user_id)
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        rec = chat_record(data, chat_id) or {}
        ok, err = shift_checklists.add_template_item(rec, kind, text_in)
        if not ok:
            waiting_checklist_add[user_id] = (kind, chat_id)
            await message.answer(err or "Не удалось добавить.")
            return
        await save_data(data)
        await message.answer(
            f"✅ Пункт добавлен · {escape(shift_checklists.kind_title(kind))}",
            parse_mode="HTML",
        )
        cfg = shift_checklists.format_config_message(
            rec, str(rec.get("title", chat_id)), kind
        )
        await message.answer(
            cfg,
            parse_mode="HTML",
            reply_markup=shift_checklists.build_config_keyboard(rec, chat_id, kind),
        )
        return

    if user_id in waiting_ops_broadcast:
        chat_id = waiting_ops_broadcast.pop(user_id)
        data = await load_data()
        if not (
            is_global_admin(user_id)
            or pulse_model.has_manager_access(data, user_id)
        ):
            await message.answer("Нет доступа.")
            return
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа к точке.")
            return
        text = text_in.strip()
        if len(text) < 2:
            waiting_ops_broadcast[user_id] = chat_id
            await message.answer("Минимум 2 символа.")
            return
        if len(text) > 1500:
            waiting_ops_broadcast[user_id] = chat_id
            await message.answer("Слишком длинно (макс. 1500 символов).")
            return
        sent, total = await _send_ops_broadcast(data, chat_id, text, user_id)
        rec = chat_record(data, chat_id) or {}
        title = escape(str(rec.get("title", chat_id)))
        await message.answer(
            f"✅ Отправлено <b>{sent}</b> из <b>{total}</b> · {title}",
            parse_mode="HTML",
            reply_markup=_manager_menu_markup(user_id),
        )
        return

    if user_id in waiting_ops_morning:
        flow = waiting_ops_morning[user_id]
        chat_id = int(flow["chat_id"])
        step = flow.get("step", "revenue_plan")
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            waiting_ops_morning.pop(user_id, None)
            await message.answer("Нет доступа.")
            return
        rec = chat_record(data, chat_id) or {}
        day = ops_day.today_key(rec.get("timezone", DEFAULT_TZ))
        d = flow.setdefault("data", {})
        if step == "revenue_plan":
            amount = ops_day.parse_amount(text_in)
            if not amount or amount <= 0:
                await message.answer("Введите число — план по выручке (например 850000).")
                return
            d["revenue_plan"] = amount
            flow["step"] = "shift_headcount"
            await message.answer(
                "Шаг 2/3: <b>сколько человек на смене</b> сегодня? (число)",
                parse_mode="HTML",
            )
            return
        if step == "shift_headcount":
            try:
                hc = int(re.sub(r"[^\d]", "", text_in))
            except ValueError:
                hc = 0
            if hc <= 0:
                await message.answer("Укажите число людей на смене (например 8).")
                return
            d["shift_headcount"] = hc
            flow["step"] = "roster"
            await message.answer(
                "Шаг 3/3: <b>расстановка</b> (или «—»).", parse_mode="HTML"
            )
            return
        if step == "roster":
            d["roster"] = text_in
            waiting_ops_morning.pop(user_id, None)
            ops_day.save_morning_draft(
                data,
                chat_id,
                day=day,
                revenue_plan=int(d["revenue_plan"]),
                shift_headcount=int(d["shift_headcount"]),
                roster=d.get("roster", ""),
                by_uid=user_id,
            )
            await save_data(data)
            await message.answer(
                "✅ План сохранён.\n\n"
                "Нажмите «📣 Опубликовать в чат сейчас», чтобы отправить в группу.",
                reply_markup=ops_day.morning_actions_keyboard(chat_id, day, posted=False),
            )
            preview = ops_day.format_morning_group_post(
                str(rec.get("title", chat_id)),
                revenue_plan=int(d["revenue_plan"]),
                shift_headcount=int(d["shift_headcount"]),
                chef_stop_text=ops_day.chef_stop_text_for_day(rec, day),
                roster=d.get("roster", ""),
            )
            await message.answer(preview, parse_mode="HTML")
            return

    if user_id in waiting_ops_evening:
        flow = waiting_ops_evening[user_id]
        chat_id = int(flow["chat_id"])
        step = flow.get("step", "revenue")
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            waiting_ops_evening.pop(user_id, None)
            await message.answer("Нет доступа.")
            return
        rec = chat_record(data, chat_id) or {}
        day = flow.get("day") or _ops_evening_day(data, chat_id)
        d = flow.setdefault("data", {})
        if step == "revenue":
            amount = ops_day.parse_amount(text_in)
            if amount is None:
                await message.answer("Введите выручку числом (например 920000).")
                return
            d["revenue"] = text_in
            flow["step"] = "comment"
            morning = ops_day.get_morning(rec, day)
            plan_amt = ops_day._morning_revenue_plan(morning)
            hint = ""
            if plan_amt:
                hint = f"\n<i>План: {escape(ops_day.format_money(plan_amt))}</i>"
            await message.answer(
                f"Шаг 2/3: комментарий к итогам для группы (или «—»).{hint}",
                parse_mode="HTML",
            )
            return
        if step == "comment":
            d["comment"] = text_in
            flow["step"] = "shift_summary"
            await message.answer(
                f"Шаг 3/3: <b>{escape(ops_day.SHIFT_SUMMARY_LABEL)}</b>\n"
                f"<i>Обязательно, {ops_day.SHIFT_SUMMARY_MIN_LEN}–{ops_day.SHIFT_SUMMARY_MAX_LEN} символов. "
                "Только для руководства — официанты не увидят.</i>",
                parse_mode="HTML",
            )
            return
        if step == "shift_summary":
            ok, err = ops_day.shift_summary_valid(text_in)
            if not ok:
                await message.answer(err or "Ошибка")
                return
            d["shift_summary"] = text_in.strip()
            waiting_ops_evening.pop(user_id, None)
            ops_day.save_evening_draft(
                data,
                chat_id,
                day=day,
                revenue=d.get("revenue", ""),
                comment=d.get("comment", "—"),
                shift_summary=d["shift_summary"],
                by_uid=user_id,
            )
            await save_data(data)
            evening = ops_day.get_evening(
                chat_record(data, chat_id) or {}, day
            )
            rev = ops_day.format_money(
                ops_day.parse_amount(str(evening.get("revenue") or ""))
            )
            met = evening.get("plan_met")
            if met:
                met_s = ops_day.PLAN_MET_RU.get(str(met), "—")
                pct = evening.get("revenue_pct")
                pct_s = f" ({pct}%)" if pct is not None else ""
                tail = f"{escape(met_s)}{pct_s}"
            else:
                tail = (
                    f"выручка {escape(rev)} · "
                    "<i>утренний план не задан — % не посчитан</i>"
                )
            await message.answer(
                f"✅ Закрытие сохранено: {tail}\n\n"
                "<i>Описание смены — только для руководства. "
                "В группу уйдут цифры и комментарий к итогам.</i>\n\n"
                "Нажмите «📣 Опубликовать итог в чат», чтобы отправить в группу.",
                reply_markup=ops_day.evening_actions_keyboard(
                    chat_id, day, posted=False
                ),
                parse_mode="HTML",
            )
            return

    if user_id in waiting_signal_title:
        chat_id = waiting_signal_title.pop(user_id)
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        title = message.text.strip()
        if len(title) < 3 or len(title) > 50:
            waiting_signal_title[user_id] = chat_id
            await message.answer("Название: от 3 до 50 символов.")
            return
        rec = chat_record(data, chat_id) or {}
        await problems_pulse.create_manual_problem(
            data,
            chat_id,
            rec.get("organization_id"),
            title,
        )
        await save_data(data)
        await message.answer(f"Тема «<b>{escape(title)}</b>» добавлена.", parse_mode="HTML")
        await _show_problems_for_manager(message, user_id, chat_id)
        return

    if user_id in waiting_chef_survey_comment:
        chat_id = waiting_chef_survey_comment.pop(user_id)
        data = await load_data()
        if not await _ops_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        body = text_in.strip()
        if len(body) < 2:
            waiting_chef_survey_comment[user_id] = chat_id
            await message.answer("Минимум 2 символа.")
            return
        rec = chat_record(data, chat_id) or {}
        title = str(rec.get("title", chat_id))
        org_id = org_id_for_restaurant_chat(data, chat_id)
        await log_feedback_event(
            {
                "event": "chef_shift",
                "user_id": user_id,
                "restaurant_chat_id": chat_id,
                "restaurant_label": title,
                "organization_id": org_id,
                "comment": body,
                "department": chef_survey.DEPARTMENT_KITCHEN,
            }
        )
        user_chef_survey_chat.pop(user_id, None)
        mk = (
            pulse_model.manager_menu_reply_markup()
            if await manager_ui_for_user(user_id)
            else pulse_model.chef_menu_reply_markup()
        )
        await message.answer(
            f"✅ Комментарий записан · {escape(title)}",
            parse_mode="HTML",
            reply_markup=mk,
        )
        return

    if user_id in waiting_chef_button_edit:
        mode, chat_id, code = waiting_chef_button_edit.pop(user_id)
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        text = message.text.strip()
        if mode == "add":
            ok, err = chef_survey.add_custom(data, chat_id, text)
        else:
            ok, err = chef_survey.update_label(data, chat_id, code, text)
        if not ok:
            await message.answer(err or "Не удалось сохранить.")
            return
        await save_data(data)
        await _refresh_chef_buttons_panel(
            uid=user_id, chat_id=chat_id, reply_target=message
        )
        return

    if user_id in waiting_button_edit:
        mode, chat_id, code = waiting_button_edit.pop(user_id)
        data = await load_data()
        if not await _manager_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        text = message.text.strip()
        if mode == "add":
            ok, err = survey_buttons.add_custom(data, chat_id, text)
        else:
            ok, err = survey_buttons.update_label(data, chat_id, code, text)
        if not ok:
            await message.answer(err or "Не удалось сохранить.")
            return
        await problems_pulse.sync_problem_titles_from_buttons(data, chat_id)
        await save_data(data)
        await _refresh_buttons_panel(uid=user_id, chat_id=chat_id, reply_target=message)
        return

    if user_id in manager_problem_pending:
        pid, status = manager_problem_pending.pop(user_id)
        await _apply_problem_status_change(
            message, user_id, pid, status, message.text.strip()
        )
        return

    if user_id not in waiting_for_comment:
        return

    pending_voice_comment.pop(user_id, None)
    await _save_shift_comment(message, user_id, message.text or "")


async def _save_shift_comment(
    message: Message,
    user_id: int,
    comment: str,
    *,
    comment_original: str | None = None,
) -> None:
    data = await load_data()
    restaurant = restaurant_label_for_log(data, user_id)
    rest_chat = user_linked_chat.get(user_id)
    org_id = org_id_for_restaurant_chat(data, rest_chat)
    problem_code = user_last_problem_code.get(user_id) or user_pending_problem.get(
        user_id
    )
    user_pending_problem.pop(user_id, None)
    dept = survey_department_for_user(data, user_id)
    entry: dict = {
        "event": "comment",
        "user_id": user_id,
        "restaurant_chat_id": rest_chat,
        "restaurant_label": restaurant,
        "organization_id": org_id,
        "comment": comment,
        "department": dept,
    }
    if (
        comment_original
        and comment_original.strip()
        and comment_original.strip() != comment.strip()
    ):
        entry["comment_original"] = comment_original.strip()
    if problem_code:
        entry["problem"] = problem_code
    await log_feedback_event(entry)

    waiting_for_comment.discard(user_id)
    pending_voice_comment.pop(user_id, None)
    finish_private_flow(user_id)
    await answer_private_flow_end(message, user_id, "Спасибо за честную обратную связь ❤️")


@dp.message(F.voice | F.audio | F.video_note, F.chat.type == "private")
async def private_voice_comment(message: Message) -> None:
    user_id = message.from_user.id
    if await _audit_ingest_media(message, user_id):
        return
    if user_id not in waiting_for_comment:
        return
    file_id = None
    filename = "voice.ogg"
    if message.voice:
        file_id = message.voice.file_id
        filename = "voice.ogg"
    elif message.audio:
        file_id = message.audio.file_id
        filename = message.audio.file_name or "audio.mp3"
    elif message.video_note:
        file_id = message.video_note.file_id
        filename = "video_note.mp4"
    if not file_id:
        return
    data = await load_data()
    dept = survey_department_for_user(data, user_id)
    kitchen = dept == pulse_model.CHAT_DEPT_KITCHEN
    await message.answer("🎧 Расшифровываю…")
    try:
        f = await bot.get_file(file_id)
        buf = await bot.download_file(f.file_path)
        raw = buf.read()
    except Exception as e:
        print(f"[voice-download] {e}")
        await message.answer(
            "Не удалось скачать голосовое. Напишите текстом или попробуйте ещё раз."
        )
        return
    # Кухня: автоязык; зал — подсказка ru для Whisper
    text = await ai_advisor.transcribe_voice(
        raw,
        filename=filename,
        language=None if kitchen else "ru",
    )
    if not text:
        if ai_advisor._client_or_none() is None:
            await message.answer(
                "Голос пока не расшифровывается: нужен OPENAI_API_KEY. "
                "Напишите текстом или нажмите «Пропустить»."
            )
        else:
            await message.answer(
                "Не разобрал голосовое. Напишите текстом или нажмите «Пропустить»."
            )
        return
    pending_voice_comment[user_id] = text
    hint = ""
    if kitchen:
        hint = (
            "\n\nЕсли говорили не по-русски — после «Отправить» в отчёт уйдёт "
            "русский перевод. Проверьте, что смысл верный."
        )
    await message.answer(
        f"Распознали:\n<i>{escape(text)}</i>\n\n"
        "Отправить в отзыв или записать снова?"
        f"{hint}",
        parse_mode="HTML",
        reply_markup=voice_confirm_keyboard(),
    )


@dp.callback_query(F.data.startswith("voice_c:"), lambda c: c.message.chat.type == "private")
async def voice_confirm_handler(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    action = (callback.data or "").split(":", 1)[-1]
    if user_id not in waiting_for_comment:
        pending_voice_comment.pop(user_id, None)
        await callback.answer()
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if action == "retry":
        pending_voice_comment.pop(user_id, None)
        await callback.answer()
        await callback.message.answer(
            "Запишите голосовое ещё раз или напишите текстом.",
            reply_markup=final_comment_keyboard,
        )
        return

    if action == "skip":
        pending_voice_comment.pop(user_id, None)
        await callback.answer()
        waiting_for_comment.discard(user_id)
        finish_private_flow(user_id)
        await answer_private_flow_end(
            callback.message, user_id, "Спасибо за честную обратную связь ❤️"
        )
        return

    if action != "ok":
        await callback.answer()
        return

    draft = pending_voice_comment.pop(user_id, None)
    if not draft:
        await callback.answer("Нет расшифровки — запишите голос ещё раз.", show_alert=True)
        return
    await callback.answer()
    data = await load_data()
    dept = survey_department_for_user(data, user_id)
    comment = draft
    original: str | None = None
    if dept == pulse_model.CHAT_DEPT_KITCHEN:
        await callback.message.answer("🇷🇺 Готовлю текст для отчёта…")
        translated = await ai_advisor.translate_to_russian(draft)
        if translated:
            original = draft
            comment = translated
            if translated.strip() != draft.strip():
                await callback.message.answer(
                    f"В отчёт:\n<i>{escape(translated)}</i>",
                    parse_mode="HTML",
                )
    await _save_shift_comment(
        callback.message, user_id, comment, comment_original=original
    )


async def _configure_bot_profile(username: str) -> None:
    """Описание ссылки t.me/bot + кнопка Mini App в меню чата."""
    try:
        await bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION[:120])
    except Exception as e:
        print(f"[bot-short-description] {e}")
    try:
        await bot.set_my_description(description=BOT_DESCRIPTION[:512])
    except Exception as e:
        print(f"[bot-description] {e}")

    # Mini App должен открываться с того же HTTPS, где крутится /api/miniapp/me
    # (обычно публичный домен Railway). Pages без API = «нет доступа».
    mini_url = os.getenv("MINIAPP_URL", "").strip()
    if not mini_url:
        railway = (
            os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
            or os.getenv("RAILWAY_STATIC_URL", "").strip()
        )
        if railway:
            if not railway.startswith("http"):
                railway = "https://" + railway
            mini_url = railway.rstrip("/") + "/"
    if not mini_url:
        print(
            "[miniapp-menu] MINIAPP_URL не задан — кнопка меню не ставится. "
            "Укажите HTTPS Railway (статика+API), не один только Pages."
        )
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Приложение",
                web_app=WebAppInfo(url=mini_url),
            )
        )
        print(f"[miniapp-menu] {mini_url}")
    except Exception as e:
        print(f"[miniapp-menu] {e}")


async def main() -> None:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if dsn:
        await db_pulse.init_db(dsn)
    else:
        print("[postgres] DATABASE_URL не задан — события только в feedback_log.jsonl")
    asyncio.create_task(scheduler_loop())
    me = await bot.get_me()
    print("Бот:", me.username, "| ADMIN_IDS:", sorted(ADMIN_IDS))
    await _configure_bot_profile(me.username or "")

    # HTTP: Mini App static + /api/miniapp/me (Railway PORT или MINIAPP_PORT)
    if os.getenv("MINIAPP_HTTP", "1").strip() not in ("0", "false", "no"):
        asyncio.create_task(
            miniapp_api.start_miniapp_server(
                bot_token=TOKEN,
                load_data=load_data,
                is_global_admin_fn=is_global_admin,
                bot_username=me.username or "",
                jsonl_path=FEEDBACK_LOG_PATH,
                save_data=save_data,
                resolve_username=_resolve_telegram_username,
            )
        )

    iiko_key = os.getenv("IIKO_API_KEY", "").strip()
    if iiko_key:

        async def _on_iiko_survey(entry, nudge):
            await log_feedback_event(entry)
            if not nudge:
                return
            try:
                me2 = await bot.get_me()
                link_chat = int(nudge.get("link_chat_id") or nudge["chat_id"])
                await bot.send_message(
                    int(nudge["chat_id"]),
                    nudge["text"],
                    reply_markup=more_link_markup(link_chat, me2.username),
                    disable_web_page_preview=True,
                )
            except Exception as e:
                print("[iiko-nudge]", repr(e))

        asyncio.create_task(
            iiko_bridge.start_http_server(
                load_data,
                save_data,
                api_key=iiko_key,
                on_survey=_on_iiko_survey,
            )
        )
    else:
        print("[iiko-http] IIKO_API_KEY не задан — HTTP для кассы выключен")
    try:
        await dp.start_polling(bot)
    finally:
        await db_pulse.close_db()


if __name__ == "__main__":
    asyncio.run(main())
