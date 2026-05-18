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


async def manager_ui_for_user(user_id: int) -> bool:
    data = await load_data()
    return is_global_admin(user_id) or pulse_model.has_manager_access(data, user_id)


def is_global_admin(user_id: int) -> bool:
    """Доступ: точное совпадение id или совпадение по модулю (на случай разного знака в .env)."""
    if user_id in ADMIN_IDS:
        return True
    if abs(user_id) in ADMINS_BY_ABS:
        return True
    return False


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


async def log_feedback_event(entry: dict) -> None:
    """Событие для аналитики: user_id + ресторан (чат) + тип. Сбой записи не должен блокировать опрос."""
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


# В группе при /start — тот же текст, что на лендинге (в личке при опросе не дублируем)
PRIVATE_WELCOME = """Привет! 👋

Этот бот помогает делать смены комфортнее и улучшать рабочие процессы в ресторане.

Здесь можно анонимно поделиться:

• впечатлением от смены
• проблемами в работе
• атмосферой в команде
• предложениями и идеями

Обратная связь помогает быстрее замечать проблемы и делать работу команды лучше ❤️

Опрос займёт меньше 30 секунд."""

# Личка: /start без ссылки из чата — коротко, без длинного приветствия
PRIVATE_START_NO_LINK = (
    "Чтобы оценить смену, зайдите в групповой чат ресторана и нажмите там "
    "«Оценить смену в личке»."
)

# Сообщение в группу при подключении бота — «как раньше»: тёплое, по-человечески
GROUP_JOIN_WELCOME = (
    "Привет! 👋 Рады быть в этом чате.\n\n"
    "Этот бот помогает делать смены комфортнее и улучшать рабочие процессы в ресторане. "
    "Для нас <b>каждый такой чат — отдельная точка</b> (свой «ресторан» в системе).\n\n"
    "Здесь можно <b>анонимно</b> делиться впечатлением от смены, проблемами, атмосферой и идеями — "
    "обратная связь помогает быстрее замечать сложности и беречь команду ❤️\n\n"
    "<b>Как это устроено:</b> мы будем присылать короткие напоминания с <b>кнопкой в личку</b> — "
    "оценка и текст идут <b>только в диалоге с ботом</b>.\n\n"
    "<b>Для администраторов чата:</b>\n"
    "• /settime 22:00 — когда присылать напоминание\n"
    "• /times — расписание\n"
    "• /deltime 22:00 — убрать время\n"
    "• /timezone Europe/Moscow — часовой пояс\n"
    "• /send или /send_now — отправить напоминание сейчас\n"
    "• /link_org org_xxxx — привязать чат к организации (после <code>/create_org</code>)\n"
    "• /smena_help — краткая справка по командам"
)

# /send и авто-напоминание — коротко, без длинного приветствия (оно на /start и при входе бота)
GROUP_REMINDER_TEXT = (
    "✨ <b>Расскажи о своем рабочем дне</b> — анонимно, до 15 секунд. "
    "Нажмите кнопку: оценка только <b>в личке</b> с ботом ❤️"
)

PROBLEM_LABELS = {
    "kitchen": "медленная кухня",
    "conflict": "конфликт / напряжение",
    "staff": "нехватка персонала",
    "management": "плохая организация",
    "stress": "сильная нагрузка",
    "comment": "свой комментарий",
}


def problem_followup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Опишите подробнее", callback_data="problem_more")],
            [InlineKeyboardButton(text="Пропустить", callback_data="problem_send")],
        ]
    )


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
        [
            InlineKeyboardButton(
                text="💬 Поделитесь с нами",
                callback_data="problem_comment",
            )
        ],
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
                    text="Оценить смену в личке ❤️",
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
            await message.answer(
                "Оценка только здесь, в личке. Выберите рейтинг ниже.",
                reply_markup=pulse_model.remove_reply_markup(),
            )
            await message.answer(
                "Как прошла смена сегодня?",
                reply_markup=rating_keyboard,
            )
            return

        user_private_slug[uid] = arg
        data = await load_data()
        data.setdefault("private_slugs", {})[str(uid)] = arg
        await save_data(data)
        await message.answer(
            "Оценка только здесь, в личке. Выберите рейтинг ниже.",
            reply_markup=pulse_model.remove_reply_markup(),
        )
        await message.answer(
            "Как прошла смена сегодня?",
            reply_markup=rating_keyboard,
        )
        return

    if await manager_ui_for_user(uid):
        await message.answer(
            "Пульс смен — меню ниже.\n"
            "Оценку рабочего дня по-прежнему начинайте из <b>рабочей группы</b> "
            "(кнопка «Оценить смену в личке»).",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_reply_markup(),
        )
    else:
        await message.answer(
            PRIVATE_START_NO_LINK,
            reply_markup=pulse_model.remove_reply_markup(),
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
            "/settime 22:00 — когда присылать напоминание с переходом в личку\n"
            "/times — расписание\n"
            "/deltime 22:00 — убрать время\n"
            "/timezone Europe/Moscow — часовой пояс\n"
            "/send или /send_now — напоминание в группу сейчас (со ссылкой в личку)\n"
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
            "<b>/set_subscription</b> org_id active|grace|suspended — пауза по оплате\n"
            "У менеджеров — кнопки «Отчёт», «Подписка», «Поддержка», «Как подключить точку».",
            parse_mode="HTML",
        )
    elif await manager_ui_for_user(message.from_user.id):
        await message.answer(
            "У вас есть меню снизу: отчёт, подписка, поддержка, как подключить точку.\n"
            "Оценку смены начинайте из <b>группы</b> по кнопке «в личку».",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "Если пришло напоминание из чата ресторана — откройте кнопку «в личку» там. "
            "Остальное позже добавим для менеджеров.",
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
    lines = [
        f"Организаций: {len(orgs)} · чатов в базе: {len(chats)}\n",
    ]
    for cid, info in sorted(chats.items(), key=lambda x: x[0]):
        title = info.get("title", cid)
        active = "✓" if info.get("active", True) and not info.get("removed_at") else "✗"
        times = ", ".join(info.get("auto_times", [])) or "—"
        tz = info.get("timezone", DEFAULT_TZ)
        oid = info.get("organization_id") or "—"
        lines.append(
            f"{active} id {escape(str(cid))}\n   {escape(str(title))}\n   org: <code>{escape(str(oid))}</code>\n"
            f"   авто: {escape(times)} ({escape(str(tz))})"
        )
    text = "\n".join(lines) if len(lines) > 1 else "Пока нет подключённых групп."
    await message.answer(text, parse_mode="HTML")


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
    await message.answer("\n".join(lines), parse_mode="HTML")


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
    if len(parts) < 4:
        await message.answer(
            "Формат:\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; network</code> — вся сеть\n"
            "<code>/link_manager &lt;telegram_id&gt; &lt;org_id&gt; location &lt;chat_id&gt;</code> — только эта группа\n\n"
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
    elif mode == "location":
        if len(parts) < 5:
            await message.answer("Для location укажите chat_id группы (число, часто отрицательное).")
            return
        try:
            loc_cid = str(int(parts[4]))
        except ValueError:
            await message.answer("chat_id должен быть целым числом (id группы из /admin).")
            return
        pulse_model.set_manager_binding(
            data, target_uid, org_id, pulse_model.ROLE_LOCATION_ADMIN, [loc_cid]
        )
    else:
        await message.answer(
            "Режим: <code>network</code> или <code>location</code>.",
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
    arr = chats[cid].setdefault("auto_times", [])
    if t not in arr:
        arr.append(t)
        arr.sort()
    await save_data(data)
    tz_disp = escape(str(chats[cid].get("timezone", DEFAULT_TZ)))
    await message.answer(
        f"Готово. В <b>{escape(t)}</b> по времени «{tz_disp}» "
        f"бот пришлёт в этот чат короткое напоминание и кнопку <b>в личку</b>.",
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
    times_disp = escape(", ".join(times) if times else "пока не задано")
    await message.answer(
        "Когда бот присылает напоминание со ссылкой в личку:\n"
        f"<b>{times_disp}</b>\n"
        f"Часовой пояс: <code>{escape(str(tz))}</code>",
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
    await bot.send_message(
        chat_id,
        GROUP_REMINDER_TEXT,
        parse_mode="HTML",
        reply_markup=shift_link_markup(chat_id, me.username),
        disable_web_page_preview=True,
    )


async def answer_private_flow_end(message: Message, user_id: int, text: str) -> None:
    extra: dict = {}
    if await manager_ui_for_user(user_id):
        extra["reply_markup"] = pulse_model.manager_menu_reply_markup()
    await message.answer(text, **extra)


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
                    if t != hm:
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
        "Оценку нужно пройти в личке: нажмите «Оценить смену в личке» в последнем сообщении бота.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("problem_"), lambda c: c.message.chat.type != "private")
async def problem_wrong_chat(callback: CallbackQuery) -> None:
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
        }
    )

    await callback.message.edit_reply_markup(reply_markup=None)

    if rating == 5:
        await answer_private_flow_end(callback.message, user_id, "Спасибо за обратную связь ❤️")
        finish_private_flow(user_id)
    else:
        await callback.message.answer(
            "Что повлияло на вашу оценку?",
            reply_markup=problem_keyboard,
        )
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

    if action == "more":
        if user_id not in user_pending_problem:
            await callback.answer("Сначала выберите тему из списка выше.", show_alert=True)
            return
        waiting_for_comment.add(user_id)
        await callback.message.answer("Опишите ситуацию подробнее ✍️")
        await callback.answer()
        return

    if action == "send":
        code = user_pending_problem.pop(user_id, None)
        if not code:
            await callback.answer("Сначала выберите тему из списка.", show_alert=True)
            return
        await log_feedback_event(
            {
                "event": "problem",
                "user_id": user_id,
                "restaurant_chat_id": rest_chat,
                "restaurant_label": restaurant,
                "organization_id": org_id,
                "problem": code,
            }
        )
        finish_private_flow(user_id)
        await answer_private_flow_end(
            callback.message, user_id, "Спасибо! Ваш отзыв помогает нам становиться лучше ❤️"
        )
        await callback.answer()
        return

    if action not in PROBLEM_LABELS:
        await callback.answer()
        return

    user_pending_problem[user_id] = action
    waiting_for_comment.discard(user_id)
    label = PROBLEM_LABELS.get(action, action)
    await callback.message.answer(
        f"Вы выбрали: <b>{escape(label)}</b>\n\n"
        "Можете <b>описать подробнее</b> или нажать <b>Пропустить</b> — без дополнительного текста.",
        parse_mode="HTML",
        reply_markup=problem_followup_keyboard(),
    )
    await callback.answer()


@dp.message(ManagerMenuFilter())
async def manager_menu_handler(message: Message) -> None:
    t = (message.text or "").strip()
    uid = message.from_user.id
    waiting_for_comment.discard(uid)
    user_pending_problem.pop(uid, None)
    me = await bot.get_me()
    if t == pulse_model.BTN_REPORT:
        await message.answer(
            "<b>Отчёт</b>\n\nВыберите период:",
            parse_mode="HTML",
            reply_markup=report_pulse.report_period_keyboard(),
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
    elif t == pulse_model.BTN_SUPPORT:
        await message.answer(
            pulse_model.text_support(SUPPORT_USERNAME or None),
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_reply_markup(),
            disable_web_page_preview=True,
        )
    elif t == pulse_model.BTN_CONNECT:
        await message.answer(
            pulse_model.text_connect_point(me.username),
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_reply_markup(),
            disable_web_page_preview=True,
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
    if period not in (report_pulse.PERIOD_SHIFT, report_pulse.PERIOD_WEEK, report_pulse.PERIOD_MONTH):
        await callback.answer()
        return
    await callback.answer("Собираю отчёт…")
    parts = await report_pulse.build_reports_for_manager(
        data,
        uid,
        period,
        is_global_admin=is_global_admin(uid),
        tz_name=DEFAULT_TZ,
        jsonl_path=FEEDBACK_LOG_PATH,
    )
    mk = pulse_model.manager_menu_reply_markup()
    for i, chunk in enumerate(parts):
        await callback.message.answer(
            chunk,
            parse_mode="HTML",
            reply_markup=mk if i == len(parts) - 1 else None,
        )


@dp.message(F.text)
async def comment_handler(message: Message) -> None:
    if message.chat.type != "private":
        return
    if not message.text or message.text.startswith("/"):
        return
    user_id = message.from_user.id
    if user_id not in waiting_for_comment:
        return

    data = await load_data()
    restaurant = restaurant_label_for_log(data, user_id)
    comment = message.text
    rest_chat = user_linked_chat.get(user_id)
    org_id = org_id_for_restaurant_chat(data, rest_chat)
    pending = user_pending_problem.pop(user_id, None)

    if pending:
        await log_feedback_event(
            {
                "event": "problem",
                "user_id": user_id,
                "restaurant_chat_id": rest_chat,
                "restaurant_label": restaurant,
                "organization_id": org_id,
                "problem": pending,
                "comment": comment,
            }
        )
    else:
        await log_feedback_event(
            {
                "event": "comment",
                "user_id": user_id,
                "restaurant_chat_id": rest_chat,
                "restaurant_label": restaurant,
                "organization_id": org_id,
                "comment": comment,
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
