"""
Бот обратной связи для группы стажёров.

• Стажёры: 6 тем про обучение на смене + свой комментарий (в личке, анонимно для чата).
• Менеджеры: отчёты, подписка, поддержка, подключение точки (как в основном Pulse).
• Данные: bot_data_stazher.json + feedback_stazher.jsonl

Запуск: TRAINEE_BOT_TOKEN, TRAINEE_ADMIN_IDS в .env / .env.stazher
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env.stazher")
except ImportError:
    pass

import pulse_model
import report_pulse
import report_stazher

_data_root = Path(
    os.getenv("STAZHER_DATA_DIR", "").strip() or Path(__file__).resolve().parent
)
_data_root.mkdir(parents=True, exist_ok=True)

DATA_PATH = _data_root / "bot_data_stazher.json"
FEEDBACK_LOG_PATH = _data_root / "feedback_stazher.jsonl"
DATA_LOCK = asyncio.Lock()
LOG_LOCK = asyncio.Lock()
DEFAULT_TZ = "Europe/Moscow"
_MSK = timezone(timedelta(hours=3))

# Код → полная формулировка (отчёт и подтверждение в личке)
TRAIN_LABELS: dict[str, str] = report_stazher.TRAIN_TOPIC_LABELS

waiting_for_comment: set[int] = set()
user_linked_chat: dict[int, int] = {}
user_report_pick: dict[int, str] = {}


def get_tz(tz_name: str | None = None) -> timezone | ZoneInfo:
    name = (tz_name or DEFAULT_TZ).strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return _MSK if name == DEFAULT_TZ else _MSK


def _parse_admin_ids() -> set[int]:
    raw = os.getenv("TRAINEE_ADMIN_IDS", os.getenv("STAZHER_ADMIN_IDS", "")).strip()
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError:
                pass
    return out


ADMIN_IDS = _parse_admin_ids()
ADMINS_BY_ABS = {abs(x) for x in ADMIN_IDS}
SUPPORT_USERNAME = os.getenv("STAZHER_SUPPORT_USERNAME", os.getenv("SUPPORT_USERNAME", "")).strip()
TOKEN = (
    os.getenv("TRAINEE_BOT_TOKEN", "").strip()
    or os.getenv("STAZHER_BOT_TOKEN", "").strip()
)
if not TOKEN:
    raise SystemExit("Задайте TRAINEE_BOT_TOKEN в .env / .env.stazher")
if not ADMIN_IDS:
    raise SystemExit("Задайте TRAINEE_ADMIN_IDS — ваш Telegram id")

bot = Bot(token=TOKEN)
dp = Dispatcher()


class ManagerMenuFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if message.chat.type != "private" or not message.text:
            return False
        if message.text.strip() not in pulse_model.MANAGER_MENU_BUTTONS:
            return False
        data = await load_data()
        uid = message.from_user.id
        return is_global_admin(uid) or pulse_model.has_manager_access(data, uid)


def is_global_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS or abs(user_id) in ADMINS_BY_ABS:
        return True
    return False


async def manager_ui_for_user(user_id: int) -> bool:
    data = await load_data()
    return is_global_admin(user_id) or pulse_model.has_manager_access(data, user_id)


async def load_data() -> dict:
    async with DATA_LOCK:
        if not DATA_PATH.exists():
            data = pulse_model.default_data()
            DATA_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return data
        try:
            data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = pulse_model.default_data()
        if pulse_model.migrate_in_place(data):
            DATA_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return data


async def save_data(data: dict) -> None:
    async with DATA_LOCK:
        DATA_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def org_id_for_chat(data: dict, chat_id: int | None) -> str | None:
    if chat_id is None:
        return None
    return pulse_model.chat_organization_id(data, chat_id)


async def log_event(entry: dict) -> None:
    row = {**entry, "ts": datetime.now(get_tz()).isoformat(timespec="seconds")}
    line = json.dumps(row, ensure_ascii=False) + "\n"
    async with LOG_LOCK:
        with FEEDBACK_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    print("[stazher]", row.get("event"), row.get("topic"), row.get("user_id"))


def encode_start_chat(chat_id: int) -> str:
    b = base64.urlsafe_b64encode(str(chat_id).encode()).decode().rstrip("=")
    return f"c{b}"


def decode_start_chat(token: str) -> int | None:
    if not token.startswith("c") or len(token) < 2:
        return None
    tail = token[1:]
    pad = (4 - len(tail) % 4) % 4
    try:
        return int(base64.urlsafe_b64decode(tail + "=" * pad).decode())
    except Exception:
        return None


def chat_title(data: dict, chat_id: int | None) -> str:
    if chat_id is None:
        return "неизвестно"
    rec = data.get("chats", {}).get(str(chat_id))
    if rec:
        return rec.get("title") or str(chat_id)
    return str(chat_id)


def finish_flow(user_id: int) -> None:
    user_linked_chat.pop(user_id, None)
    waiting_for_comment.discard(user_id)


train_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📋 Не рассказали до смены",
                callback_data="train_prep",
            )
        ],
        [
            InlineKeyboardButton(
                text="👀 Не показали на практике",
                callback_data="train_demo",
            )
        ],
        [
            InlineKeyboardButton(
                text="❓ Непонятно, что от меня ждут",
                callback_data="train_unclear",
            )
        ],
        [
            InlineKeyboardButton(
                text="🧑‍🏫 Наставник не успевал",
                callback_data="train_mentor",
            )
        ],
        [
            InlineKeyboardButton(
                text="⚡ Слишком много нового за смену",
                callback_data="train_overload",
            )
        ],
        [
            InlineKeyboardButton(
                text="🏃 Учили «на бегу», без паузы",
                callback_data="train_on_the_fly",
            )
        ],
        [
            InlineKeyboardButton(
                text="💬 Свой комментарий",
                callback_data="train_comment",
            )
        ],
    ]
)

skip_comment_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="extra_skip")],
    ]
)

PRIVATE_WELCOME = """Привет! 👋

Бот <b>обратной связи по обучению</b> для стажёров.

Анонимно отметьте, чего не хватало на смене — так видно, где усилить наставничество и подготовку. Займёт меньше минуты."""

GROUP_JOIN = (
    "Привет! 👋 Бот <b>обучения стажёров</b> в этом чате.\n\n"
    "Напоминания с кнопкой <b>в личку</b> — ответы только у бота, в группе их не видно.\n\n"
    "<b>Админам чата:</b>\n"
    "• /settime 22:00 — напоминание\n"
    "• /send_now — сейчас\n"
    "• /link_org org_xxxx — привязка к организации\n"
    "• /stazher_help — справка"
)

GROUP_REMINDER = (
    "📋 <b>Как прошла смена с точки зрения обучения?</b> "
    "Ответ в личке — анонимно для чата."
)

PROMPT_TOPICS = (
    "Чего <b>не хватало</b> на смене, чтобы учиться и работать увереннее?\n"
    "Выберите самое близкое или «Свой комментарий»."
)


def shift_link_markup(chat_id: int, username: str) -> InlineKeyboardMarkup:
    url = f"https://t.me/{username}?start={encode_start_chat(chat_id)}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Ответить в личке 📋", url=url)]
        ]
    )


def parse_hhmm(s: str) -> str | None:
    s = s.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", s):
        return None
    h, m = s.split(":")
    hi, mi = int(h), int(m)
    if 0 <= hi <= 23 and 0 <= mi <= 59:
        return f"{hi:02d}:{mi:02d}"
    return None


async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("creator", "administrator")
    except Exception:
        return False


async def send_topics_prompt(message: Message) -> None:
    await message.answer(
        PROMPT_TOPICS,
        parse_mode="HTML",
        reply_markup=train_keyboard,
    )


async def answer_flow_end(message: Message, user_id: int, text: str) -> None:
    extra: dict = {}
    if await manager_ui_for_user(user_id):
        extra["reply_markup"] = pulse_model.manager_menu_reply_markup()
    await message.answer(text, **extra)


async def post_reminder(chat_id: int) -> None:
    data = await load_data()
    oid = pulse_model.chat_organization_id(data, chat_id)
    if oid and pulse_model.is_org_billing_blocked(data, oid):
        print(f"[stazher-skip-suspended] chat={chat_id} org={oid}")
        return
    me = await bot.get_me()
    await bot.send_message(
        chat_id,
        GROUP_REMINDER,
        parse_mode="HTML",
        reply_markup=shift_link_markup(chat_id, me.username),
        disable_web_page_preview=True,
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
            "title": chat.title or cid,
            "type": chat.type,
            "added_at": prev.get("added_at")
            or datetime.now(get_tz()).isoformat(timespec="seconds"),
            "auto_times": prev.get("auto_times", []),
            "timezone": prev.get("timezone", DEFAULT_TZ),
            "active": True,
            "organization_id": prev.get("organization_id"),
        }
        chats[cid].pop("removed_at", None)
        await save_data(data)
        try:
            await bot.send_message(chat.id, GROUP_JOIN, parse_mode="HTML")
        except Exception:
            pass

    if new_st in ("left", "kicked"):
        if cid in chats:
            chats[cid]["removed_at"] = datetime.now(get_tz()).isoformat(timespec="seconds")
            chats[cid]["active"] = False
        await save_data(data)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.chat.type in ("group", "supergroup"):
        me = await bot.get_me()
        await message.answer(PRIVATE_WELCOME, parse_mode="HTML")
        await message.answer(
            "Кнопка откроет личку — ответ привяжется к этой группе стажёров.",
            reply_markup=shift_link_markup(message.chat.id, me.username),
            disable_web_page_preview=True,
        )
        return

    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        linked = decode_start_chat(parts[1].strip())
        if linked is not None:
            user_linked_chat[uid] = linked
            waiting_for_comment.discard(uid)
            await message.answer(
                "Ответ только здесь, в личке.",
                reply_markup=pulse_model.remove_reply_markup(),
            )
            await send_topics_prompt(message)
            return

    if await manager_ui_for_user(uid):
        await message.answer(
            "Обучение стажёров — меню ниже.\n"
            "Отклик по смене — из <b>группы стажёров</b> (кнопка «Ответить в личке»).",
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_reply_markup(),
        )
    else:
        await message.answer(
            "Зайдите в <b>группу стажёров</b> и нажмите «Ответить в личке» в напоминании бота.",
            parse_mode="HTML",
            reply_markup=pulse_model.remove_reply_markup(),
        )


@dp.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    await message.answer(
        f"Ваш Telegram ID: <code>{uid}</code>\n"
        f"<code>TRAINEE_ADMIN_IDS={uid}</code>",
        parse_mode="HTML",
    )


@dp.message(Command("stazher_help", "help"))
async def cmd_help(message: Message) -> None:
    if message.chat.type in ("group", "supergroup"):
        await message.answer(
            "<b>Группа стажёров</b>\n"
            "/settime 22:00 · /times · /deltime 22:00\n"
            "/timezone Europe/Moscow · /send_now\n"
            "/link_org org_xxxx · /start",
            parse_mode="HTML",
        )
        return
    if is_global_admin(message.from_user.id):
        await message.answer(
            "<b>/admin</b> · <b>/orgs</b> · <b>/create_org</b> · <b>/link_manager</b>\n"
            "<b>/set_subscription</b> org_id active|grace|suspended\n"
            "В группе: <code>/link_org</code>",
            parse_mode="HTML",
        )
    elif await manager_ui_for_user(message.from_user.id):
        await message.answer(
            "Меню: отчёт, подписка, поддержка, как подключить точку.\n"
            "Стажёры отвечают из группы по кнопке «в личку».",
            parse_mode="HTML",
        )
    else:
        await message.answer("Ответ — из группы стажёров по кнопке «Ответить в личке».")


@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if message.chat.type != "private" or not is_global_admin(message.from_user.id):
        return
    data = await load_data()
    chats = data.get("chats", {})
    lines = [f"Организаций: {len(data.get('organizations', {}))} · чатов: {len(chats)}\n"]
    for cid, info in sorted(chats.items(), key=lambda x: x[0]):
        title = info.get("title", cid)
        active = "✓" if info.get("active", True) and not info.get("removed_at") else "✗"
        times = ", ".join(info.get("auto_times", [])) or "—"
        oid = info.get("organization_id") or "—"
        lines.append(
            f"{active} <code>{escape(str(cid))}</code>\n"
            f"   {escape(str(title))}\n"
            f"   org: <code>{escape(str(oid))}</code> · авто: {escape(times)}"
        )
    await message.answer("\n".join(lines) if len(lines) > 1 else "Чатов пока нет.", parse_mode="HTML")


@dp.message(Command("orgs"))
async def cmd_orgs(message: Message) -> None:
    if message.chat.type != "private" or not is_global_admin(message.from_user.id):
        return
    data = await load_data()
    orgs = data.get("organizations", {})
    if not orgs:
        await message.answer("<code>/create_org Название</code>", parse_mode="HTML")
        return
    lines = ["<b>Организации</b>\n"]
    for oid, org in sorted(orgs.items()):
        lines.append(
            f"• <code>{escape(str(oid))}</code> — <b>{escape(str(org.get('name', oid)))}</b> "
            f"· <code>{escape(str(org.get('subscription', pulse_model.SUB_ACTIVE)))}</code>"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("create_org"))
async def cmd_create_org(message: Message) -> None:
    if message.chat.type != "private" or not is_global_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("<code>/create_org Название</code>", parse_mode="HTML")
        return
    data = await load_data()
    oid = pulse_model.create_organization(data, parts[1].strip())
    await save_data(data)
    await message.answer(
        f"Создано: <b>{escape(parts[1].strip())}</b>\n<code>{escape(oid)}</code>\n"
        f"В группе: <code>/link_org {escape(oid)}</code>",
        parse_mode="HTML",
    )


@dp.message(Command("link_manager"))
async def cmd_link_manager(message: Message) -> None:
    if message.chat.type != "private" or not is_global_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 4:
        await message.answer(
            "<code>/link_manager ID org_id network</code>\n"
            "<code>/link_manager ID org_id location CHAT_ID</code>",
            parse_mode="HTML",
        )
        return
    try:
        target_uid = int(parts[1])
    except ValueError:
        await message.answer("Первый аргумент — Telegram id.")
        return
    org_id, mode = parts[2], parts[3].lower()
    data = await load_data()
    if org_id not in data.get("organizations", {}):
        await message.answer("Нет такой организации.")
        return
    if mode == "network":
        pulse_model.set_manager_binding(
            data, target_uid, org_id, pulse_model.ROLE_NETWORK_ADMIN, None
        )
    elif mode == "location":
        if len(parts) < 5:
            await message.answer("Укажите chat_id группы.")
            return
        pulse_model.set_manager_binding(
            data,
            target_uid,
            org_id,
            pulse_model.ROLE_LOCATION_ADMIN,
            [str(int(parts[4]))],
        )
    else:
        await message.answer("Режим: network или location.")
        return
    await save_data(data)
    await message.answer(f"Готово: <code>{target_uid}</code> → <code>{escape(org_id)}</code>", parse_mode="HTML")


@dp.message(Command("link_org"))
async def cmd_link_org(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Команду пишут в группе стажёров.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("<code>/link_org org_xxxx</code>", parse_mode="HTML")
        return
    org_id = parts[1].strip()
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Нужны права админа чата.")
        return
    data = await load_data()
    if org_id not in data.get("organizations", {}):
        await message.answer("Нет такой организации.")
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
    await message.answer(f"Чат привязан к <code>{escape(org_id)}</code>.", parse_mode="HTML")


@dp.message(Command("set_subscription"))
async def cmd_set_subscription(message: Message) -> None:
    if message.chat.type != "private" or not is_global_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "<code>/set_subscription org_id active|grace|suspended</code>",
            parse_mode="HTML",
        )
        return
    oid, state = parts[1].strip(), parts[2].strip().lower()
    if state not in (pulse_model.SUB_ACTIVE, pulse_model.SUB_GRACE, pulse_model.SUB_SUSPENDED):
        await message.answer("Статусы: active, grace, suspended.")
        return
    data = await load_data()
    org = data.get("organizations", {}).get(oid)
    if not org:
        await message.answer("Нет организации.")
        return
    org["subscription"] = state
    await save_data(data)
    await message.answer(f"<code>{escape(oid)}</code> → <b>{escape(state)}</b>", parse_mode="HTML")


@dp.message(Command("settime"))
async def cmd_settime(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("/settime 22:00")
        return
    t = parse_hhmm(parts[1])
    if not t:
        await message.answer("Формат ЧЧ:ММ")
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Нужны права админа чата.")
        return
    data = await load_data()
    cid = str(message.chat.id)
    rec = data.setdefault("chats", {}).setdefault(
        cid,
        {"title": message.chat.title or cid, "auto_times": [], "timezone": DEFAULT_TZ, "active": True},
    )
    times = rec.setdefault("auto_times", [])
    if t not in times:
        times.append(t)
        times.sort()
    await save_data(data)
    await message.answer(f"Напоминание в <b>{t}</b>", parse_mode="HTML")


@dp.message(Command("times"))
async def cmd_times(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    data = await load_data()
    rec = data.get("chats", {}).get(str(message.chat.id))
    if not rec:
        await message.answer("Чат не в базе.")
        return
    times = ", ".join(rec.get("auto_times", [])) or "не задано"
    await message.answer(f"<b>{times}</b> · TZ: <code>{rec.get('timezone', DEFAULT_TZ)}</code>", parse_mode="HTML")


@dp.message(Command("deltime"))
async def cmd_deltime(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return
    t = parse_hhmm(parts[1])
    if not t:
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        return
    data = await load_data()
    rec = data.get("chats", {}).get(str(message.chat.id))
    if rec and t in rec.get("auto_times", []):
        rec["auto_times"].remove(t)
        await save_data(data)
        await message.answer(f"Время {t} убрано.")


@dp.message(Command("timezone"))
async def cmd_timezone(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("/timezone Europe/Moscow")
        return
    tz_name = parts[1].strip()
    try:
        ZoneInfo(tz_name)
    except Exception:
        await message.answer("Пример: Europe/Moscow")
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        return
    data = await load_data()
    cid = str(message.chat.id)
    data.setdefault("chats", {}).setdefault(
        cid, {"title": message.chat.title or cid, "auto_times": [], "timezone": tz_name, "active": True}
    )["timezone"] = tz_name
    await save_data(data)
    await message.answer(f"Часовой пояс: {tz_name}")


@dp.message(Command("send_now", "send"))
async def cmd_send(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        mk = (
            pulse_model.manager_menu_reply_markup()
            if await manager_ui_for_user(message.from_user.id)
            else pulse_model.remove_reply_markup()
        )
        await message.answer(
            "<b>/send_now</b> — в группе стажёров (админ чата).",
            parse_mode="HTML",
            reply_markup=mk,
        )
        return
    uid = message.from_user.id
    if not (is_global_admin(uid) or await is_chat_admin(message.chat.id, uid)):
        await message.answer("Только для админов чата.")
        return
    data = await load_data()
    oid = pulse_model.chat_organization_id(data, message.chat.id)
    if oid and pulse_model.is_org_billing_blocked(data, oid):
        await message.answer("Подписка приостановлена — напоминания не отправляем.", parse_mode="HTML")
        return
    await post_reminder(message.chat.id)


@dp.callback_query(F.data.startswith("train_"), lambda c: c.message.chat.type != "private")
async def train_wrong_chat(callback: CallbackQuery) -> None:
    await callback.answer("Откройте личку по кнопке из группы.", show_alert=True)


@dp.callback_query(F.data.startswith("train_"), lambda c: c.message.chat.type == "private")
async def train_handler(callback: CallbackQuery) -> None:
    topic = callback.data.replace("train_", "")
    user_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)

    if topic not in TRAIN_LABELS:
        await callback.answer()
        return

    data = await load_data()
    rest_chat = user_linked_chat.get(user_id)
    org_id = org_id_for_chat(data, rest_chat)

    if topic == "comment":
        waiting_for_comment.add(user_id)
        await callback.message.answer(
            "Опишите <b>своими словами</b>, чего не хватало на смене для обучения.",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await log_event(
        {
            "event": report_stazher.EVENT_TOPIC,
            "topic": topic,
            "problem": topic,
            "user_id": user_id,
            "restaurant_chat_id": rest_chat,
            "restaurant_label": chat_title(data, rest_chat),
            "organization_id": org_id,
            "topic_label": TRAIN_LABELS[topic],
        }
    )
    await callback.answer()
    await callback.message.answer(
        f"Записали: <b>{escape(TRAIN_LABELS[topic])}</b>\n\n"
        "Можете дополнить комментарием или «Пропустить».",
        parse_mode="HTML",
        reply_markup=skip_comment_keyboard,
    )
    waiting_for_comment.add(user_id)


@dp.callback_query(F.data == "extra_skip", lambda c: c.message.chat.type == "private")
async def extra_skip(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    await callback.message.edit_reply_markup(reply_markup=None)
    finish_flow(user_id)
    await callback.answer()
    await answer_flow_end(
        callback.message, user_id, "Спасибо! Это поможет улучшить обучение стажёров ❤️"
    )


@dp.message(ManagerMenuFilter())
async def manager_menu_handler(message: Message) -> None:
    t = (message.text or "").strip()
    uid = message.from_user.id
    waiting_for_comment.discard(uid)
    me = await bot.get_me()
    data = await load_data()

    if t == pulse_model.BTN_REPORT:
        scope = report_pulse.chat_scope_for_user(
            data, uid, is_global_admin=is_global_admin(uid)
        )
        if not scope:
            await message.answer(
                "Нет групп для отчёта.",
                reply_markup=pulse_model.manager_menu_reply_markup(),
            )
            return
        if is_global_admin(uid) and len(scope) > 1:
            await message.answer(
                "<b>Отчёт по обучению</b>\n\nВыберите группу:",
                parse_mode="HTML",
                reply_markup=report_pulse.report_location_keyboard(scope, include_all=True),
            )
        else:
            if len(scope) == 1:
                user_report_pick[uid] = scope[0][0]
            await message.answer(
                "<b>Отчёт по обучению</b>\n\nВыберите период:",
                parse_mode="HTML",
                reply_markup=report_pulse.report_period_keyboard(),
            )
    elif t == pulse_model.BTN_SUBSCRIPTION:
        if is_global_admin(uid) and not pulse_model.manager_profiles(data, uid):
            orgs = data.get("organizations", {})
            if not orgs:
                text = "Организаций нет. <code>/create_org Название</code>"
            else:
                parts_sub = ["<b>Подписки</b>\n"]
                for oid, org in sorted(orgs.items()):
                    parts_sub.append(
                        f"• <code>{escape(str(oid))}</code> — "
                        f"<code>{escape(str(org.get('subscription', pulse_model.SUB_ACTIVE)))}</code>"
                    )
                text = "\n".join(parts_sub)
        else:
            text = pulse_model.text_subscription_status(data, uid)
        await message.answer(
            text, parse_mode="HTML", reply_markup=pulse_model.manager_menu_reply_markup()
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
            pulse_model.text_connect_point(me.username).replace(
                "официантов/смены", "стажёров"
            ).replace("Оценки сотрудников", "Отклики стажёров"),
            parse_mode="HTML",
            reply_markup=pulse_model.manager_menu_reply_markup(),
            disable_web_page_preview=True,
        )


@dp.callback_query(F.data.startswith("report_r:"))
async def report_location_handler(callback: CallbackQuery) -> None:
    if callback.message.chat.type != "private":
        await callback.answer()
        return
    uid = callback.from_user.id
    if not is_global_admin(uid):
        await callback.answer("Только для глобального админа.", show_alert=True)
        return
    pick = (callback.data or "").split(":", 1)[-1]
    if pick == "__skip__":
        await callback.answer("Список чатов: /admin", show_alert=True)
        return
    data = await load_data()
    scope = report_pulse.chat_scope_for_user(data, uid, is_global_admin=True)
    if pick != "all" and not any(str(cid) == pick for cid, _ in scope):
        await callback.answer("Группа не найдена.", show_alert=True)
        return
    user_report_pick[uid] = pick
    await callback.answer()
    await callback.message.answer(
        "<b>Отчёт по обучению</b>\n\nПериод:",
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
        await callback.answer("Нет доступа.", show_alert=True)
        return
    period = (callback.data or "").split(":", 1)[-1]
    if period not in (
        report_pulse.PERIOD_SHIFT,
        report_pulse.PERIOD_WEEK,
        report_pulse.PERIOD_MONTH,
    ):
        await callback.answer()
        return
    pick = user_report_pick.pop(uid, None)
    if is_global_admin(uid) and pick is None:
        await callback.answer("Сначала выберите группу.", show_alert=True)
        return
    await callback.answer("Собираю отчёт…")
    parts = await report_stazher.build_reports_for_manager(
        data,
        uid,
        period,
        is_global_admin=is_global_admin(uid),
        tz_name=DEFAULT_TZ,
        jsonl_path=FEEDBACK_LOG_PATH,
        selected_chat=pick,
    )
    mk = pulse_model.manager_menu_reply_markup()
    for i, chunk in enumerate(parts):
        await callback.message.answer(
            chunk, parse_mode="HTML", reply_markup=mk if i == len(parts) - 1 else None
        )


@dp.message(F.text)
async def text_handler(message: Message) -> None:
    if message.chat.type != "private" or not message.text or message.text.startswith("/"):
        return
    user_id = message.from_user.id
    if user_id not in waiting_for_comment:
        return

    data = await load_data()
    rest_chat = user_linked_chat.get(user_id)
    comment = message.text.strip()
    if not comment:
        return

    await log_event(
        {
            "event": report_stazher.EVENT_COMMENT,
            "user_id": user_id,
            "restaurant_chat_id": rest_chat,
            "restaurant_label": chat_title(data, rest_chat),
            "organization_id": org_id_for_chat(data, rest_chat),
            "comment": comment,
        }
    )
    finish_flow(user_id)
    await answer_flow_end(
        message, user_id, "Спасибо за честную обратную связь ❤️"
    )


async def scheduler_loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            data = await load_data()
            today = date.today().isoformat()
            sent_map = data.setdefault("last_auto_sent", {})
            changed = False
            for cid, info in list(data.get("chats", {}).items()):
                if info.get("removed_at") or info.get("active") is False:
                    continue
                oid = info.get("organization_id")
                if oid and pulse_model.is_org_billing_blocked(data, oid):
                    continue
                tz = get_tz(info.get("timezone", DEFAULT_TZ))
                hm = datetime.now(tz).strftime("%H:%M")
                for t in info.get("auto_times", []):
                    if t != hm:
                        continue
                    key = f"{cid}|{t}|{today}"
                    if sent_map.get(key):
                        continue
                    try:
                        await post_reminder(int(cid))
                        sent_map[key] = True
                        changed = True
                    except Exception as e:
                        print(f"[stazher-auto-fail] {cid}: {e}")
            for k in list(sent_map):
                parts = k.split("|")
                if len(parts) >= 3 and parts[2] < today:
                    del sent_map[k]
                    changed = True
            if changed:
                await save_data(data)
        except Exception as e:
            print(f"[stazher-scheduler] {e}")
        await asyncio.sleep(45)


async def main() -> None:
    asyncio.create_task(scheduler_loop())
    me = await bot.get_me()
    print("Стажёр-бот:", me.username, "| admins:", sorted(ADMIN_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
