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
import survey_buttons
import chef_survey
import admin_metrics
import ops_day
import monthly_ops

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
    return pulse_model.manager_menu_reply_markup(show_inbox=ga, show_commands=ga)


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
    if rest_chat is not None and entry.get("user_id") is not None:
        try:
            asyncio.create_task(_check_tariff_limit_for_chat(int(rest_chat)))
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


# user_id -> id группы, для которой сейчас проходит опрос в личке
user_linked_chat: dict[int, int] = {}
# slug из /start <slug> (для старых ссылок без привязки к чату)
user_private_slug: dict[int, str] = {}
# ждём текст комментария в личке (после «Опишите подробнее»)
waiting_for_comment: set[int] = set()
# выбранная тема проблемы до «отправить так» / уточнения текстом
user_pending_problem: dict[int, str] = {}
# выбранная точка для отчёта (chat_id или "all")
user_report_pick: dict[int, str] = {}
# зал / кухня / весь ресторан
user_report_dept: dict[int, str] = {}
# точка для опроса шефа (без ссылки из группы)
user_chef_survey_chat: dict[int, int] = {}
waiting_chef_survey_comment: dict[int, int] = {}
waiting_chef_button_edit: dict[int, tuple[str, int, str]] = {}
manager_chef_buttons_panel: dict[int, int] = {}
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
ops_pick_chat: dict[int, str] = {}  # uid -> prefix for location pick


# В группе при /start — тот же текст, что на лендинге (в личке при опросе не дублируем)
PRIVATE_WELCOME = """Привет! 👋

Этот бот помогает делать смены комфортнее и улучшать рабочие процессы в ресторане.

Здесь можно анонимно поделиться:

• впечатлением от смены
• проблемами в работе
• атмосферой в команде
• предложениями и идеями

Обратная связь помогает быстрее замечать проблемы и делать работу команды лучше ❤️

Опрос займёт меньше 30 секунд.

Политика конфиденциальности: https://www.pulseteam.online/privacy.html"""

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
    "• /link_org org_xxxx — привязать чат к организации (после <code>/create_org</code>)\n"
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
    tz_name = _chat_tz_name(data, chat_id) if chat_id is not None else DEFAULT_TZ
    hour = datetime.now(get_tz(tz_name)).hour
    if _is_evening_reminder_hour(hour):
        return "Как прошёл сегодняшний день?"
    return "Как прошёл вчерашний день на смене?"

PROBLEM_LABELS = {
    "kitchen": "медленная кухня",
    "conflict": "конфликт / напряжение",
    "staff": "нехватка персонала",
    "management": "плохая организация",
    "stress": "сильная нагрузка",
    "comment": "свой комментарий",
}

PERSONAL_LABELS = report_pulse.PERSONAL_FACTOR_LABELS


rating_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="1 ⭐", callback_data="rating_1"),
            InlineKeyboardButton(text="2 ⭐", callback_data="rating_2"),
            InlineKeyboardButton(text="3 ⭐", callback_data="rating_3"),
            InlineKeyboardButton(text="4 ⭐", callback_data="rating_4"),
            InlineKeyboardButton(text="5 ⭐", callback_data="rating_5"),
        ]
    ]
)

personal_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📚 Нехватка знаний по процессам",
                callback_data="personal_knowledge",
            )
        ],
        [
            InlineKeyboardButton(
                text="😴 Усталость / плохое состояние",
                callback_data="personal_fatigue",
            )
        ],
        [
            InlineKeyboardButton(
                text="⏱ Сложности с тайм-менеджментом",
                callback_data="personal_time_mgmt",
            )
        ],
        [
            InlineKeyboardButton(
                text="💬 Сложности в коммуникации",
                callback_data="personal_communication",
            )
        ],
        [
            InlineKeyboardButton(
                text="🎯 Потеря концентрации",
                callback_data="personal_concentration",
            )
        ],
        [InlineKeyboardButton(text="Пропустить", callback_data="personal_skip")],
    ]
)

final_comment_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="final_skip")],
    ]
)

rating5_followup_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Опишите подробнее", callback_data="rating5_more")],
        [InlineKeyboardButton(text="Пропустить", callback_data="rating5_skip")],
    ]
)

problem_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🍽 Медленная кухня",
                callback_data="problem_kitchen",
            )
        ],
        [
            InlineKeyboardButton(
                text="😤 Конфликт / напряжение",
                callback_data="problem_conflict",
            )
        ],
        [
            InlineKeyboardButton(
                text="👥 Нехватка персонала",
                callback_data="problem_staff",
            )
        ],
        [
            InlineKeyboardButton(
                text="📋 Плохая организация",
                callback_data="problem_management",
            )
        ],
        [
            InlineKeyboardButton(
                text="😓 Сильная нагрузка",
                callback_data="problem_stress",
            )
        ],
        [InlineKeyboardButton(text="Пропустить", callback_data="problem_skip")],
    ]
)


def restaurant_label_for_log(data: dict, user_id: int) -> str:
    cid = user_linked_chat.get(user_id)
    if cid is not None:
        rec = chat_record(data, cid)
        if rec:
            return rec.get("title") or str(cid)
        return str(cid)
    slug = user_private_slug.get(user_id) or data.get("private_slugs", {}).get(str(user_id))
    return slug or "неизвестно"


def finish_private_flow(user_id: int) -> None:
    user_linked_chat.pop(user_id, None)
    user_private_slug.pop(user_id, None)
    user_pending_problem.pop(user_id, None)
    waiting_for_comment.discard(user_id)


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
    rec["auto_times"] = [x for x in arr if x]


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
        await problems_pulse.sync_problems_from_period(
            data,
            chat_id,
            rec.get("organization_id"),
            jsonl_path=FEEDBACK_LOG_PATH,
            tz_name=rec.get("timezone", DEFAULT_TZ),
            days=problems_pulse.SIGNALS_SYNC_DAYS,
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
        parts = await report_pulse.build_reports_for_manager(
            data,
            mid,
            report_pulse.PERIOD_MONTH,
            is_global_admin=is_global_admin(mid),
            tz_name=tz_name,
            jsonl_path=FEEDBACK_LOG_PATH,
            selected_chat=cid,
        )
        if not parts:
            continue
        try:
            await bot.send_message(
                mid,
                intro + parts[0],
                parse_mode="HTML",
            )
            for chunk in parts[1:]:
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


async def _post_morning_to_group(
    data: dict, chat_id: int, day: str
) -> tuple[str, str | None]:
    """(ok|incomplete|already_posted|send_failed, detail)"""
    rec = chat_record(data, chat_id) or {}
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
) -> None:
    mk = reply_markup if reply_markup is not None else _manager_menu_markup(uid)
    for i, chunk in enumerate(parts):
        try:
            await target.answer(
                chunk,
                parse_mode="HTML",
                reply_markup=mk if i == len(parts) - 1 else None,
            )
        except Exception as e:
            print(f"[report-send] uid={uid}", repr(e))
            await target.answer(
                chunk[:3500],
                reply_markup=mk if i == len(parts) - 1 else None,
            )


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


async def _show_morning_status(message: Message, uid: int, chat_id: int) -> None:
    data = await load_data()
    rec = chat_record(data, chat_id) or {}
    tz_name = rec.get("timezone", DEFAULT_TZ)
    day = ops_day.today_key(tz_name)
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
            "Отправьте <b>новый текст</b> — сразу опубликуем в группу."
        )
    else:
        body = (
            f"<b>Стоп-лист</b> · {title}\n"
            f"Дедлайн: <b>{deadline}</b>\n\n"
            "Введите стоп-лист на сегодня — сразу уйдёт в группу."
        )
    waiting_chef_stop[uid] = {"chat_id": chat_id, "day": day}
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


async def _show_evening_status(message: Message, uid: int, chat_id: int) -> None:
    data = await load_data()
    rec = chat_record(data, chat_id) or {}
    tz_name = rec.get("timezone", DEFAULT_TZ)
    day = _ops_evening_day(data, chat_id)
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
            "Шаг 1/2: <b>фактическая выручка</b> за смену (число).\n"
            "<i>План выполнен или нет — посчитаем сами по утреннему плану.</i>",
            parse_mode="HTML",
        )


def _problem_keyboard_for_user(data: dict, user_id: int) -> InlineKeyboardMarkup:
    chat_id = user_linked_chat.get(user_id)
    if chat_id is None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        rows = [
            [
                InlineKeyboardButton(
                    text=b["label"][:64],
                    callback_data=f"problem_{b['code']}"[:64],
                )
            ]
            for b in survey_buttons.enabled_buttons(survey_buttons._copy_defaults())
        ]
        rows.append(
            [InlineKeyboardButton(text="Пропустить", callback_data="problem_skip")]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return survey_buttons.build_problem_keyboard(data, chat_id)


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
        linked = decode_start_chat(arg)
        if linked is not None:
            user_linked_chat[uid] = linked
            user_private_slug.pop(uid, None)
            data = await load_data()
            await message.answer(
                "Здесь можно ответить анонимно — выберите оценку ниже.",
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
            "Здесь можно ответить анонимно — выберите оценку ниже.",
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
                f"<b>{pulse_model.BTN_COMMANDS}</b> — полная инструкция по командам."
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
            f"• <b>{pulse_model.BTN_STOP_LIST}</b> — стоп-лист сразу в группу\n"
            f"• <b>{pulse_model.BTN_STOP_CURRENT}</b> — посмотреть актуальный стоп",
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
        "Если бот пишет «нет доступа» к /admin, добавьте эту строку в <code>.env</code>:\n"
        f"<code>ADMIN_IDS={uid}</code>\n\n"
        "(несколько id через запятую без пробела)",
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
            "/link_org org_xxxx — привязать этот чат к организации (глобальный админ или админ чата)\n"
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
            "<b>/del_org</b> org_id — удалить организацию (отвязать точки)\n"
            "<b>/set_subscription</b> org_id active|grace|suspended — пауза по оплате\n"
            "<b>/set_tariff</b> chat_id t10|t25|t40|enterprise — тариф точки\n"
            "<b>/metrics</b> — уникальные пользователи и тарифы по точкам\n"
            "<b>/tariff_history</b> chat_id — история смен тарифа\n"
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
            f"Ваш Telegram ID: <code>{uid}</code>\n"
            "Добавьте в файл <code>.env</code> рядом с ботом строку (можно несколько id через запятую):\n"
            f"<code>ADMIN_IDS={uid}</code>\n"
            "и перезапустите бота.",
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
                    f"• <code>{escape(str(mid))}</code> — менеджер точки "
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
        f"В группе точки выполните <code>/link_org {escape(oid)}</code> (от имени админа чата или вас).",
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
    if len(parts) < 4:
        await message.answer(
            "Формат:\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; network</code> — вся сеть\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; location &lt;chat_id&gt;</code> — менеджер точки\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; chef &lt;chat_id&gt;</code> — шеф (стоп-лист)\n\n"
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
    elif mode in ("location", "chef"):
        if len(parts) < 5:
            await message.answer("Укажите chat_id группы (число, часто отрицательное).")
            return
        try:
            loc_cid = str(int(parts[4]))
        except ValueError:
            await message.answer("chat_id должен быть целым числом (id группы из /admin).")
            return
        role = (
            pulse_model.ROLE_CHEF
            if mode == "chef"
            else pulse_model.ROLE_LOCATION_ADMIN
        )
        pulse_model.set_manager_binding(data, target_uid, org_id, role, [loc_cid])
    else:
        await message.answer(
            "Режим: <code>network</code>, <code>location</code> или <code>chef</code>.",
            parse_mode="HTML",
        )
        return
    await save_data(data)
    await message.answer(
        f"Готово: пользователь <code>{target_uid}</code> привязан к <code>{escape(org_id)}</code> как <b>{escape(mode)}</b>.",
        parse_mode="HTML",
    )


@dp.message(Command("link_org"))
async def cmd_link_org(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Команду пишут в групповом чате точки.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Например: <code>/link_org org_a1b2c3d4</code> (id из <code>/orgs</code> в личке у админа).",
            parse_mode="HTML",
        )
        return
    org_id = parts[1].strip()
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Эту команду могут выполнить админы чата или глобальный админ бота.")
        return
    data = await load_data()
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
    await save_data(data)
    await message.answer(
        f"Этот чат привязан к организации <code>{escape(org_id)}</code>. "
        "Напоминания и отчёты пойдут в рамках этой сети.",
        parse_mode="HTML",
    )


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
                "Алиасы: 10, 25, 40, 9900 и т.д.",
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
        # Кнопка поддержки должна быть доступна всем пользователям в личке.
        extra["reply_markup"] = pulse_model.support_only_reply_markup()
    await message.answer(text, **extra)


async def prompt_personal_factors(message: Message) -> None:
    await message.answer(
        "<b>Личные факторы смены</b> — как вы повлияли на смену\n\n"
        "Выберите пункт или «Пропустить».",
        parse_mode="HTML",
        reply_markup=personal_keyboard,
    )


async def prompt_final_comment(message: Message, user_id: int) -> None:
    waiting_for_comment.add(user_id)
    await message.answer(
        "Последний шаг: <b>свободный комментарий</b> к смене одним сообщением "
        "или «Пропустить».",
        parse_mode="HTML",
        reply_markup=final_comment_keyboard,
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

                # Понедельник 10:00 — синхронизация проблем + дайджест в группу + отчёт менеджерам
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

            for k in list(sent_map.keys()):
                parts = k.split("|")
                if len(parts) >= 3 and parts[2] < today:
                    del sent_map[k]
                    changed = True

            if changed:
                await save_data(data)
        except Exception as e:
            print(f"[scheduler] {e}")
        await asyncio.sleep(45)


@dp.callback_query(F.data.startswith("rating_"), lambda c: c.message.chat.type != "private")
async def rating_wrong_chat(callback: CallbackQuery) -> None:
    await callback.answer(
        "Оценку нужно пройти в личке: нажмите «Рассказать в личке» в последнем сообщении бота.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("problem_"), lambda c: c.message.chat.type != "private")
async def problem_wrong_chat(callback: CallbackQuery) -> None:
    await callback.answer("Продолжите в личке с ботом по ссылке из чата.", show_alert=True)


@dp.callback_query(
    F.data.startswith("personal_")
    | F.data.startswith("final_")
    | F.data.startswith("rating5_"),
    lambda c: c.message.chat.type != "private",
)
async def personal_wrong_chat(callback: CallbackQuery) -> None:
    await callback.answer("Продолжите в личке с ботом по ссылке из чата.", show_alert=True)


@dp.callback_query(F.data.startswith("rating_"), lambda c: c.message.chat.type == "private")
async def rating_handler(callback: CallbackQuery) -> None:
    rating = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    data = await load_data()
    restaurant = restaurant_label_for_log(data, user_id)
    rest_chat = user_linked_chat.get(user_id)

    print("------------")
    print(f"LOG rating user={user_id} rest={restaurant} val={rating}")
    print("------------")
    await log_feedback_event(
        {
            "event": "rating",
            "user_id": user_id,
            "restaurant_chat_id": rest_chat,
            "restaurant_label": restaurant,
            "organization_id": org_id_for_restaurant_chat(data, rest_chat),
            "rating": rating,
            "department": chef_survey.DEPARTMENT_FLOOR,
        }
    )

    await callback.message.edit_reply_markup(reply_markup=None)

    if rating == 5:
        await callback.message.answer(
            "Спасибо! Оценка <b>5</b> сохранена ⭐",
            parse_mode="HTML",
        )
        await callback.message.answer(
            "Можете коротко описать, что было хорошо на смене:",
            reply_markup=rating5_followup_keyboard,
        )
    else:
        data = await load_data()
        await callback.message.answer(
            "Что повлияло на смену? Можно несколько кнопок или <b>Пропустить</b>.",
            parse_mode="HTML",
            reply_markup=_problem_keyboard_for_user(data, user_id),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("rating5_"), lambda c: c.message.chat.type == "private")
async def rating5_followup_handler(callback: CallbackQuery) -> None:
    action = callback.data.replace("rating5_", "")
    user_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)

    if action == "more":
        waiting_for_comment.add(user_id)
        await callback.message.answer("Опишите подробнее ✍️")
        await callback.answer()
        return

    if action == "skip":
        finish_private_flow(user_id)
        await callback.answer()
        await answer_private_flow_end(
            callback.message, user_id, "Спасибо за обратную связь ❤️"
        )
        return

    await callback.answer()


@dp.callback_query(F.data.startswith("problem_"), lambda c: c.message.chat.type == "private")
async def problem_handler(callback: CallbackQuery) -> None:
    action = callback.data.replace("problem_", "")
    user_id = callback.from_user.id
    data = await load_data()
    restaurant = restaurant_label_for_log(data, user_id)
    rest_chat = user_linked_chat.get(user_id)
    org_id = org_id_for_restaurant_chat(data, rest_chat)

    await callback.message.edit_reply_markup(reply_markup=None)

    if action == "skip":
        user_pending_problem.pop(user_id, None)
        waiting_for_comment.discard(user_id)
        await callback.answer()
        await prompt_personal_factors(callback.message)
        return

    allowed = (
        survey_buttons.valid_codes(data, rest_chat)
        if rest_chat is not None
        else {b["code"] for b in survey_buttons.DEFAULT_BUTTONS if b.get("enabled")}
    )
    if action not in allowed:
        await callback.answer()
        return

    await log_feedback_event(
        {
            "event": "problem",
            "user_id": user_id,
            "restaurant_chat_id": rest_chat,
            "restaurant_label": restaurant,
            "organization_id": org_id,
            "problem": action,
            "department": chef_survey.DEPARTMENT_FLOOR,
        }
    )
    user_pending_problem.pop(user_id, None)
    waiting_for_comment.discard(user_id)
    await callback.answer()
    await prompt_personal_factors(callback.message)


@dp.callback_query(
    F.data.startswith("personal_"),
    lambda c: c.message.chat.type == "private",
)
async def personal_handler(callback: CallbackQuery) -> None:
    action = callback.data.replace("personal_", "")
    user_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)

    if action == "skip":
        await callback.answer()
        await prompt_final_comment(callback.message, user_id)
        return

    if action not in PERSONAL_LABELS:
        await callback.answer()
        return

    data = await load_data()
    restaurant = restaurant_label_for_log(data, user_id)
    rest_chat = user_linked_chat.get(user_id)
    org_id = org_id_for_restaurant_chat(data, rest_chat)
    await log_feedback_event(
        {
            "event": "personal_factor",
            "user_id": user_id,
            "restaurant_chat_id": rest_chat,
            "restaurant_label": restaurant,
            "organization_id": org_id,
            "problem": action,
            "department": chef_survey.DEPARTMENT_FLOOR,
        }
    )
    label = PERSONAL_LABELS.get(action, action)
    await callback.answer()
    await callback.message.answer(f"Записали: <b>{escape(label)}</b>", parse_mode="HTML")
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
            "incomplete": "Сначала заполните план",
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
            await callback.message.answer(
                "Черновик плана не найден. Заполните план ещё раз.",
                parse_mode="HTML",
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
            "incomplete": "Сначала введите выручку",
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
            await callback.message.answer(
                "Черновик закрытия не найден. Заполните выручку ещё раз "
                "или напишите мне — проверим настройки сервера.",
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
                "Шаг 1/2: <b>фактическая выручка</b>.", parse_mode="HTML"
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
    await message.answer(
        f"Готово: <b>{escape(prob.title)}</b> → {escape(st_ru)}",
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
    mgr_note = (
        f"🔥 <b>Обновление темы</b> — {escape(str(rec.get('title', prob.restaurant_chat_id)))}\n\n"
        f"{problems_pulse.format_problem_card(prob)}"
    )
    await _notify_managers_problem_report(
        data, prob.restaurant_chat_id, mgr_note, exclude_uid=uid
    )
    manager_problem_chat[uid] = prob.restaurant_chat_id


@dp.message(F.text == pulse_model.BTN_SUPPORT, F.chat.type == "private")
async def support_button_handler(message: Message) -> None:
    """Поддержка — любой пользователь в личке (сотрудник или менеджер)."""
    uid = message.from_user.id
    if await manager_ui_for_user(uid):
        mk = pulse_model.manager_menu_reply_markup()
    else:
        mk = pulse_model.support_only_reply_markup()
    await message.answer(
        pulse_model.text_support(SUPPORT_USERNAME or None),
        parse_mode="HTML",
        reply_markup=mk,
        disable_web_page_preview=True,
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
        data = await load_data()
        scope = await _chef_stop_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:svpick", title="Оценить смену"
        )
        if chat_id is not None:
            await _show_chef_survey(message, uid, chat_id)
    elif t == pulse_model.BTN_STOP_LIST:
        data = await load_data()
        scope = await _chef_stop_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:stopick", title="Стоп-лист"
        )
        if chat_id is not None:
            await _show_stop_list_status(message, uid, chat_id)
    elif t == pulse_model.BTN_STOP_CURRENT:
        data = await load_data()
        scope = await _chef_stop_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:stview", title="Актуальный стоп"
        )
        if chat_id is not None:
            await _show_stop_current(message, uid, chat_id)
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
    waiting_monthly_plan.pop(uid, None)
    waiting_staff_out.pop(uid, None)
    waiting_task_assign.pop(uid, None)
    me = await bot.get_me()
    ga = is_global_admin(uid)
    mk_root = pulse_model.manager_menu_root_markup(show_inbox=ga, show_commands=ga)

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
        await message.answer(
            "<b>Смена</b>\nСтоп-лист, план утром, закрытие вечером, кадры, задания.",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_shift_markup(),
        )
        return
    if t == pulse_model.BTN_FOLDER_MORE:
        await message.answer(
            "<b>Настройки</b>\nПодписка, поддержка, подключение точки.",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_more_markup(),
        )
        return
    if t == pulse_model.BTN_MENU_HOME:
        await message.answer(
            "Главное меню Pulse Team.",
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
        await message.answer(
            pulse_model.text_connect_point(me.username),
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_reply_markup(),
            disable_web_page_preview=True,
        )
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
    elif t == pulse_model.BTN_STOP_LIST:
        data = await load_data()
        scope = await _chef_stop_scope(data, uid)
        chat_id = await _ops_pick_chat_or_ask(
            message, uid, scope, prefix="ops:stopick", title="Стоп-лист"
        )
        if chat_id is not None:
            await _show_stop_list_status(message, uid, chat_id)
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
        parts_rep = await report_pulse.build_reports_for_manager(
            data,
            uid,
            report_pulse.PERIOD_MONTH,
            is_global_admin=True,
            tz_name=tz_name,
            jsonl_path=FEEDBACK_LOG_PATH,
            selected_chat=str(chat_id),
        )
        await _send_report_chunks(callback.message, uid, parts_rep)
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
        parts = await report_pulse.build_reports_for_manager(
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
    await _send_report_chunks(callback.message, uid, parts)


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
        parts, kb = await report_pulse.build_calendar_month_reports(
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
    await _send_report_chunks(callback.message, uid, parts, reply_markup=kb)


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
        day = flow.get("day") or ops_day.today_key(DEFAULT_TZ)
        data = await load_data()
        if not await _ops_can_access_chat(data, user_id, chat_id):
            await message.answer("Нет доступа.")
            return
        body = text_in.strip()
        if len(body) < 2:
            waiting_chef_stop[user_id] = flow
            await message.answer("Минимум 2 символа. Введите стоп-лист.")
            return
        if len(body) > 2000:
            waiting_chef_stop[user_id] = flow
            await message.answer("Слишком длинно (макс. 2000 символов).")
            return
        rec = chat_record(data, chat_id) or {}
        day = flow.get("day") or ops_day.today_key(rec.get("timezone", DEFAULT_TZ))
        was_set = ops_day.chef_stop_has_content(ops_day.get_chef_stop(rec, day))
        ops_day.save_chef_stop(
            data, chat_id, day=day, text=body, by_uid=user_id
        )
        result, detail = await _post_chef_stop_to_group(data, chat_id, day)
        await save_data(data)
        title = escape(str(rec.get("title", chat_id)))
        if result == "ok":
            tag = "обновлён" if was_set else "опубликован"
            mk = (
                pulse_model.manager_menu_reply_markup()
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
                f"Шаг 2/2: комментарий к итогам (или «—»).{hint}",
                parse_mode="HTML",
            )
            return
        if step == "comment":
            d["comment"] = text_in
            waiting_ops_evening.pop(user_id, None)
            ops_day.save_evening_draft(
                data,
                chat_id,
                day=day,
                revenue=d.get("revenue", text_in),
                comment=d.get("comment", text_in),
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

    data = await load_data()
    restaurant = restaurant_label_for_log(data, user_id)
    comment = message.text
    rest_chat = user_linked_chat.get(user_id)
    org_id = org_id_for_restaurant_chat(data, rest_chat)
    user_pending_problem.pop(user_id, None)
    await log_feedback_event(
        {
            "event": "comment",
            "user_id": user_id,
            "restaurant_chat_id": rest_chat,
            "restaurant_label": restaurant,
            "organization_id": org_id,
            "comment": comment,
            "department": chef_survey.DEPARTMENT_FLOOR,
        }
    )

    waiting_for_comment.discard(user_id)
    finish_private_flow(user_id)
    await answer_private_flow_end(message, user_id, "Спасибо за честную обратную связь ❤️")


async def main() -> None:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if dsn:
        await db_pulse.init_db(dsn)
    else:
        print("[postgres] DATABASE_URL не задан — события только в feedback_log.jsonl")
    asyncio.create_task(scheduler_loop())
    me = await bot.get_me()
    print("Бот:", me.username, "| ADMIN_IDS:", sorted(ADMIN_IDS))
    try:
        await dp.start_polling(bot)
    finally:
        await db_pulse.close_db()


if __name__ == "__main__":
    asyncio.run(main())
