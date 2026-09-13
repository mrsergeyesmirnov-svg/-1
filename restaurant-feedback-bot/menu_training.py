"""Adaptive menu training for restaurant staff.

Privacy invariant: learner identity is never exposed to managers. Progress is stored only
under a keyed pseudonymous hash. This module never reads or writes shift feedback answers.
Managers can manage TTK/menu sources and publication state, but cannot see named learner
progress (or a list of learners).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import io
import json
import os
import random
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import F
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from docx import Document
from openai import AsyncOpenAI
from openpyxl import load_workbook
from pypdf import PdfReader

import db_pulse
import pulse_model
import training_materials


BOT: Any = None
LOAD_DATA: Callable[[], Awaitable[dict[str, Any]]] | None = None
SAVE_DATA: Callable[[dict[str, Any]], Awaitable[None]] | None = None
IS_GLOBAL_ADMIN: Callable[[int], bool] | None = None
_CLIENT: AsyncOpenAI | None = None
_SCHEMA_LOCK = asyncio.Lock()
_SCHEMA_READY = False
_WORKER_LOCK = asyncio.Lock()

MODEL = os.getenv("MENU_TRAINING_MODEL", os.getenv("OPENAI_MODEL", "gpt-5-mini")).strip() or "gpt-5-mini"
MAX_SOURCE_CHARS = int(os.getenv("MENU_TRAINING_MAX_SOURCE_CHARS", "120000"))
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".xlsx"}


DDL = """
CREATE TABLE IF NOT EXISTS menu_training_sessions (
    id TEXT PRIMARY KEY,
    learner_hash TEXT NOT NULL,
    restaurant_chat_id BIGINT NOT NULL,
    department TEXT NOT NULL DEFAULT 'floor',
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    questions JSONB NOT NULL,
    answers JSONB NOT NULL DEFAULT '[]'::jsonb,
    current_index INTEGER NOT NULL DEFAULT 0,
    prompt_message_id BIGINT,
    score DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_menu_training_sessions_learner
    ON menu_training_sessions (learner_hash, restaurant_chat_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_menu_training_one_active
    ON menu_training_sessions (learner_hash, restaurant_chat_id) WHERE status='active';

CREATE TABLE IF NOT EXISTS menu_training_mastery (
    learner_hash TEXT NOT NULL,
    restaurant_chat_id BIGINT NOT NULL,
    dish_key TEXT NOT NULL,
    dimension TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    mastery DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_seen_at TIMESTAMPTZ,
    next_due_at TIMESTAMPTZ,
    PRIMARY KEY (learner_hash, restaurant_chat_id, dish_key, dimension)
);
CREATE INDEX IF NOT EXISTS idx_menu_training_mastery_due
    ON menu_training_mastery (learner_hash, restaurant_chat_id, next_due_at);
"""

MENU_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "is_menu_source": {"type": "boolean"},
        "document_title": {"type": "string"},
        "dishes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "ingredients": {"type": "array", "items": {"type": "string"}},
                    "allergens": {"type": "array", "items": {"type": "string"}},
                    "allergens_confirmed": {"type": "boolean"},
                    "preparation": {"type": "string"},
                    "serving": {"type": "string"},
                    "weight": {"type": "string"},
                    "sales_description": {"type": "string"},
                    "important_facts": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "name", "category", "ingredients", "allergens", "allergens_confirmed",
                    "preparation", "serving", "weight", "sales_description", "important_facts",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_menu_source", "document_title", "dishes"],
    "additionalProperties": False,
}

OPEN_GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 10},
        "correct": {"type": "boolean"},
        "feedback": {"type": "string"},
        "ideal_answer": {"type": "string"},
    },
    "required": ["score", "correct", "feedback", "ideal_answer"],
    "additionalProperties": False,
}


DIMENSION_LABELS = {
    "ingredients": "состав",
    "allergens": "аллергены",
    "sales": "продажа блюда",
    "preparation": "технология",
    "serving": "подача",
    "recognition": "узнавание блюда",
}


def configure(
    bot: Any,
    load_data: Callable[[], Awaitable[dict[str, Any]]],
    save_data: Callable[[dict[str, Any]], Awaitable[None]],
    is_global_admin: Callable[[int], bool],
) -> None:
    global BOT, LOAD_DATA, SAVE_DATA, IS_GLOBAL_ADMIN
    BOT = bot
    LOAD_DATA = load_data
    SAVE_DATA = save_data
    IS_GLOBAL_ADMIN = is_global_admin


def _ai() -> AsyncOpenAI:
    global _CLIENT
    if _CLIENT is None:
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY не настроен")
        _CLIENT = AsyncOpenAI(api_key=key)
    return _CLIENT


def _learner_hash(user_id: int) -> str:
    secret = os.getenv("MENU_TRAINING_SECRET", "").strip() or os.getenv("BOT_TOKEN", "").strip()
    if not secret:
        raise RuntimeError("MENU_TRAINING_SECRET/BOT_TOKEN не настроен")
    return hmac.new(secret.encode("utf-8"), str(user_id).encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def _dish_key(name: str) -> str:
    normalized = re.sub(r"\s+", " ", (name or "").strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def _menu_root(rec: dict[str, Any]) -> dict[str, Any]:
    root = rec.get("menu_training")
    if not isinstance(root, dict):
        root = {}
        rec["menu_training"] = root
    root.setdefault("status", "empty")
    root.setdefault("draft_dishes", [])
    root.setdefault("published_dishes", [])
    root.setdefault("version", 0)
    root.setdefault("updated_at", None)
    return root


def has_published_menu(rec: dict[str, Any]) -> bool:
    root = _menu_root(rec)
    return root.get("status") == "published" and bool(root.get("published_dishes"))


def published_dishes(rec: dict[str, Any]) -> list[dict[str, Any]]:
    if not has_published_menu(rec):
        return []
    dishes = _menu_root(rec).get("published_dishes") or []
    return [d for d in dishes if isinstance(d, dict) and d.get("name")]


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
        print("[menu-training] schema ready")
        return True


def _manager_can(data: dict[str, Any], uid: int, chat_id: int) -> bool:
    if IS_GLOBAL_ADMIN and IS_GLOBAL_ADMIN(uid):
        return True
    try:
        return str(chat_id) in pulse_model.allowed_chat_ids_for_manager(data, uid)
    except Exception:
        return False


def _staff_can(data: dict[str, Any], uid: int, chat_id: int) -> bool:
    linked = training_materials.staff_chat_id(data, uid)
    return linked is not None and int(linked) == int(chat_id)


def _department(data: dict[str, Any], chat_id: int) -> str:
    try:
        return pulse_model.chat_department(data, chat_id)
    except Exception:
        return "floor"


def _extract_text(file_name: str, payload: bytes) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".pdf":
        text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages)
    elif suffix == ".docx":
        doc = Document(io.BytesIO(payload))
        chunks = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                chunks.append("\t".join(cell.text for cell in row.cells))
        text = "\n".join(chunks)
    elif suffix == ".xlsx":
        wb = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        chunks: list[str] = []
        for ws in wb.worksheets:
            chunks.append(f"\n### Лист: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                values = [str(v).strip() for v in row if v is not None and str(v).strip()]
                if values:
                    chunks.append("\t".join(values))
        text = "\n".join(chunks)
    elif suffix == ".txt":
        text = payload.decode("utf-8", errors="replace")
    else:
        raise ValueError("Поддерживаются PDF, DOCX, XLSX и TXT")
    return text[:MAX_SOURCE_CHARS]


async def _analyse_text(file_name: str, source: str) -> dict[str, Any]:
    prompt = f"""
Разбери документ ресторана как источник знаний для обучения сотрудников.
Верни блюда/напитки только если они реально присутствуют в документе.
Ничего не придумывай и не дополняй знаниями модели.

КРИТИЧЕСКОЕ ПРАВИЛО ПО АЛЛЕРГЕНАМ:
- allergens заполняй только если аллерген явно указан в документе;
- не выводи аллергены самостоятельно из состава;
- если явного списка/пометки аллергенов нет, allergens=[] и allergens_confirmed=false.

ingredients — только ингредиенты/состав из источника.
preparation — только технология/особенности приготовления из источника.
serving — только подача из источника.
weight — только указанный вес/выход.
sales_description — краткое привлекательное описание можно переформулировать, но только из фактов источника.
important_facts — факты, которые сотруднику важно знать, только из источника.
Если это не меню, не ТТК и не карточки блюд, поставь is_menu_source=false и dishes=[].

Имя файла: {file_name}
<source>
{source}
</source>
"""
    response = await _ai().responses.create(
        model=MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "Ты методист ресторана. Работаешь строго по предоставленному источнику. "
                    "Безопасность важнее полноты: не выдумывай аллергены, состав и технологию."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "menu_analysis",
                "schema": MENU_ANALYSIS_SCHEMA,
                "strict": True,
            }
        },
    )
    result = json.loads(response.output_text)
    for dish in result.get("dishes") or []:
        dish["key"] = _dish_key(str(dish.get("name", "")))
    return result


def _merge_dishes(existing: list[dict[str, Any]], fresh: list[dict[str, Any]], source_file_id: str) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for dish in existing:
        if isinstance(dish, dict) and dish.get("key"):
            out[str(dish["key"])] = dict(dish)
    for dish in fresh:
        if not isinstance(dish, dict) or not dish.get("name"):
            continue
        row = dict(dish)
        row["key"] = row.get("key") or _dish_key(str(row["name"]))
        row["source_file_id"] = source_file_id
        out[str(row["key"])] = row
    return sorted(out.values(), key=lambda d: (str(d.get("category", "")), str(d.get("name", ""))))


async def _download_training_file(file_id: str) -> bytes:
    tg_file = await BOT.get_file(file_id)
    buf = io.BytesIO()
    await BOT.download_file(tg_file.file_path, destination=buf)
    return buf.getvalue()


async def _process_pending_file(chat_id: int, file_meta: dict[str, Any]) -> None:
    if not LOAD_DATA or not SAVE_DATA:
        return
    title = str(file_meta.get("title") or "material")
    ext = Path(title).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        data = await LOAD_DATA()
        rec = (data.get("chats") or {}).get(str(chat_id))
        if isinstance(rec, dict):
            for f in training_materials.list_files(rec):
                if f.get("id") == file_meta.get("id"):
                    f["analysis_status"] = "ignored"
                    f["analysis_error"] = "Формат не поддерживается для тестов"
            await SAVE_DATA(data)
        return

    try:
        payload = await _download_training_file(str(file_meta.get("file_id")))
        source = _extract_text(title, payload)
        if len(source.strip()) < 20:
            raise ValueError("В файле не удалось извлечь текст")
        analysis = await _analyse_text(title, source)

        data = await LOAD_DATA()
        rec = (data.get("chats") or {}).get(str(chat_id))
        if not isinstance(rec, dict):
            return
        current = next((f for f in training_materials.list_files(rec) if f.get("id") == file_meta.get("id")), None)
        if not current:
            return
        current["analysis_status"] = "done" if analysis.get("is_menu_source") else "ignored"
        current["analysis_error"] = ""
        current["analysis_title"] = str(analysis.get("document_title") or title)[:160]
        current["analysis_dishes"] = len(analysis.get("dishes") or [])
        current["analysis_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if analysis.get("is_menu_source") and analysis.get("dishes"):
            root = _menu_root(rec)
            root["draft_dishes"] = _merge_dishes(
                list(root.get("draft_dishes") or []),
                list(analysis.get("dishes") or []),
                str(current.get("id")),
            )
            root["status"] = "draft"
            root["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await SAVE_DATA(data)

        by_uid = current.get("by_uid")
        if by_uid and analysis.get("is_menu_source") and analysis.get("dishes"):
            try:
                await BOT.send_message(
                    int(by_uid),
                    "<b>🧠 ТТК разобраны</b>\n\n"
                    f"Файл: {html.escape(title)}\n"
                    f"Найдено позиций: <b>{len(analysis.get('dishes') or [])}</b>\n\n"
                    "База пока в черновике. Проверь статус и опубликуй её — только после этого тесты появятся у сотрудников.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="Проверить и опубликовать", callback_data=f"mt:m:{chat_id}"[:64])
                        ]]
                    ),
                )
            except Exception:
                pass
    except Exception as exc:
        data = await LOAD_DATA()
        rec = (data.get("chats") or {}).get(str(chat_id))
        if isinstance(rec, dict):
            for f in training_materials.list_files(rec):
                if f.get("id") == file_meta.get("id"):
                    f["analysis_status"] = "error"
                    f["analysis_error"] = str(exc)[:300]
                    f["analysis_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            await SAVE_DATA(data)
        print(f"[menu-training] analyse file chat={chat_id}: {exc!r}")


async def background_worker() -> None:
    """Analyses newly uploaded supported materials. Existing old files are untouched until reanalyse."""
    await asyncio.sleep(8)
    while True:
        try:
            if not LOAD_DATA or not SAVE_DATA or BOT is None:
                await asyncio.sleep(15)
                continue
            async with _WORKER_LOCK:
                data = await LOAD_DATA()
                pending: list[tuple[int, dict[str, Any]]] = []
                for cid, rec in (data.get("chats") or {}).items():
                    if not isinstance(rec, dict):
                        continue
                    for f in training_materials.list_files(rec):
                        if f.get("analysis_status") == "pending":
                            try:
                                pending.append((int(cid), dict(f)))
                            except Exception:
                                continue
                for chat_id, meta in pending[:3]:
                    data_now = await LOAD_DATA()
                    rec_now = (data_now.get("chats") or {}).get(str(chat_id))
                    if not isinstance(rec_now, dict):
                        continue
                    row = next((f for f in training_materials.list_files(rec_now) if f.get("id") == meta.get("id")), None)
                    if not row or row.get("analysis_status") != "pending":
                        continue
                    row["analysis_status"] = "processing"
                    await SAVE_DATA(data_now)
                    await _process_pending_file(chat_id, meta)
        except Exception as exc:
            print("[menu-training worker]", repr(exc))
        await asyncio.sleep(12)


def _manager_status_text(rec: dict[str, Any], title: str) -> str:
    root = _menu_root(rec)
    files = training_materials.list_files(rec)
    statuses: dict[str, int] = {}
    for f in files:
        s = str(f.get("analysis_status") or "not_analyzed")
        statuses[s] = statuses.get(s, 0) + 1
    draft = list(root.get("draft_dishes") or [])
    published = list(root.get("published_dishes") or [])
    confirmed = sum(1 for d in draft if d.get("allergens_confirmed"))
    parts = [
        f"<b>🧠 ТТК и тесты</b> · {html.escape(title)}",
        "",
        f"Статус базы: <b>{html.escape(str(root.get('status', 'empty')))}</b>",
        f"Черновик: <b>{len(draft)}</b> позиций",
        f"Опубликовано: <b>{len(published)}</b> позиций · версия <b>{int(root.get('version') or 0)}</b>",
        f"Аллергены явно подтверждены источником: <b>{confirmed}</b> / {len(draft) if draft else 0}",
        "",
        "Файлы: " + (", ".join(f"{k} — {v}" for k, v in sorted(statuses.items())) if statuses else "нет"),
        "",
        "<i>Прогресс конкретных сотрудников здесь намеренно не показывается: обучение не раскрывает личности менеджеру.</i>",
    ]
    return "\n".join(parts)


def _manager_status_keyboard(chat_id: int, rec: dict[str, Any]) -> InlineKeyboardMarkup:
    root = _menu_root(rec)
    rows: list[list[InlineKeyboardButton]] = []
    if root.get("draft_dishes"):
        rows.append([InlineKeyboardButton(text="✅ Опубликовать базу", callback_data=f"mt:pub:{chat_id}"[:64])])
    rows.append([InlineKeyboardButton(text="🔄 Анализировать все ТТК заново", callback_data=f"mt:rean:{chat_id}"[:64])])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _fetch_active(learner: str, chat_id: int) -> Any:
    if not await _ensure_schema():
        return None
    pool = db_pulse.pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM menu_training_sessions WHERE learner_hash=$1 AND restaurant_chat_id=$2 AND status='active' ORDER BY created_at DESC LIMIT 1",
            learner,
            chat_id,
        )


async def _mastery_map(learner: str, chat_id: int) -> dict[tuple[str, str], dict[str, Any]]:
    if not await _ensure_schema():
        return {}
    pool = db_pulse.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT dish_key,dimension,mastery,next_due_at FROM menu_training_mastery WHERE learner_hash=$1 AND restaurant_chat_id=$2",
            learner,
            chat_id,
        )
    return {
        (str(r["dish_key"]), str(r["dimension"])): {
            "mastery": float(r["mastery"] or 0),
            "next_due_at": r["next_due_at"],
        }
        for r in rows
    }


def _other_values(dishes: list[dict[str, Any]], dish_key: str, field: str) -> list[str]:
    values: list[str] = []
    for d in dishes:
        if str(d.get("key")) == dish_key:
            continue
        raw = d.get(field)
        if isinstance(raw, list):
            values.extend(str(x).strip() for x in raw if str(x).strip())
        elif raw:
            values.append(str(raw).strip())
    return list(dict.fromkeys(values))


def _question_candidates(dishes: list[dict[str, Any]], department: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    dish_names = [str(d.get("name")) for d in dishes if d.get("name")]
    for d in dishes:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        key = str(d.get("key") or _dish_key(name))
        ingredients = [str(x).strip() for x in (d.get("ingredients") or []) if str(x).strip()]
        allergens = [str(x).strip() for x in (d.get("allergens") or []) if str(x).strip()]
        if ingredients:
            out.append({
                "dish_key": key, "dish_name": name, "dimension": "ingredients", "type": "open",
                "question": f"Перечисли основные ингредиенты блюда «{name}».",
                "model_answer": ", ".join(ingredients),
                "facts": ingredients,
            })
            distractors = [x for x in _other_values(dishes, key, "ingredients") if x not in ingredients]
            if distractors:
                correct = random.choice(ingredients)
                wrong = random.sample(distractors, min(3, len(distractors)))
                if len(wrong) == 3:
                    options = wrong + [correct]
                    random.shuffle(options)
                    out.append({
                        "dish_key": key, "dish_name": name, "dimension": "ingredients", "type": "mcq",
                        "question": f"Какой ингредиент входит в «{name}»?",
                        "options": options,
                        "correct_index": options.index(correct),
                        "model_answer": correct,
                    })
        if d.get("allergens_confirmed"):
            model = ", ".join(allergens) if allergens else "Аллергены явно не указаны / отсутствуют по источнику"
            out.append({
                "dish_key": key, "dish_name": name, "dimension": "allergens", "type": "open",
                "question": f"Какие аллергены явно указаны для блюда «{name}»?",
                "model_answer": model,
                "facts": allergens,
            })
        if department == "kitchen":
            if str(d.get("preparation") or "").strip():
                out.append({
                    "dish_key": key, "dish_name": name, "dimension": "preparation", "type": "open",
                    "question": f"Расскажи технологию/ключевые особенности приготовления «{name}».",
                    "model_answer": str(d.get("preparation")),
                    "facts": [str(d.get("preparation"))],
                })
            if str(d.get("serving") or "").strip():
                out.append({
                    "dish_key": key, "dish_name": name, "dimension": "serving", "type": "open",
                    "question": f"Как должна выглядеть подача «{name}»?",
                    "model_answer": str(d.get("serving")),
                    "facts": [str(d.get("serving"))],
                })
        else:
            if str(d.get("sales_description") or "").strip():
                out.append({
                    "dish_key": key, "dish_name": name, "dimension": "sales", "type": "open",
                    "question": f"Гость спрашивает про «{name}». Дай короткое красочное продающее описание в 2–3 предложениях, не выдумывая факты.",
                    "model_answer": str(d.get("sales_description")),
                    "facts": [str(d.get("sales_description"))] + [str(x) for x in (d.get("important_facts") or [])],
                })
        if len(dish_names) >= 4 and ingredients:
            clue = random.choice(ingredients)
            wrong_names = [x for x in dish_names if x != name]
            if len(wrong_names) >= 3:
                options = random.sample(wrong_names, 3) + [name]
                random.shuffle(options)
                out.append({
                    "dish_key": key, "dish_name": name, "dimension": "recognition", "type": "mcq",
                    "question": f"В каком блюде по ТТК есть ингредиент «{clue}»?",
                    "options": options,
                    "correct_index": options.index(name),
                    "model_answer": name,
                })
    random.shuffle(out)
    return out


async def _select_questions(learner: str, chat_id: int, dishes: list[dict[str, Any]], department: str, mode: str) -> list[dict[str, Any]]:
    candidates = _question_candidates(dishes, department)
    if not candidates:
        return []
    mastery = await _mastery_map(learner, chat_id)
    now = datetime.now(timezone.utc)
    if mode == "full":
        random.shuffle(candidates)
        target = min(len(candidates), min(30, max(12, len(dishes) * 2)))
        selected = candidates[:target]
    else:
        scored: list[tuple[float, dict[str, Any]]] = []
        for q in candidates:
            row = mastery.get((q["dish_key"], q["dimension"]), {})
            m = float(row.get("mastery") or 0)
            due = row.get("next_due_at")
            due_bonus = 0.0
            if due is None or (hasattr(due, "tzinfo") and due <= now):
                due_bonus = 0.35
            scored.append((1.0 - m + due_bonus + random.random() * 0.2, q))
        scored.sort(key=lambda item: item[0], reverse=True)
        target = 1 if mode == "one" else min(7, len(scored))
        selected = [q for _, q in scored[:target]]
    random.shuffle(selected)
    return selected


async def _create_session(uid: int, chat_id: int, mode: str) -> tuple[Any | None, str | None]:
    if not LOAD_DATA:
        return None, "Сервис не настроен"
    if not await _ensure_schema():
        return None, "База обучения недоступна"
    learner = _learner_hash(uid)
    active = await _fetch_active(learner, chat_id)
    if active:
        return active, "active"
    data = await LOAD_DATA()
    rec = (data.get("chats") or {}).get(str(chat_id))
    if not isinstance(rec, dict):
        return None, "Точка не найдена"
    dishes = published_dishes(rec)
    if not dishes:
        return None, "Меню ещё не опубликовано менеджером"
    department = _department(data, chat_id)
    questions = await _select_questions(learner, chat_id, dishes, department, mode)
    if not questions:
        return None, "Пока не удалось собрать вопросы по опубликованным ТТК"
    sid = "mts_" + secrets.token_hex(8)
    pool = db_pulse.pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO menu_training_sessions
               (id,learner_hash,restaurant_chat_id,department,mode,status,questions)
               VALUES ($1,$2,$3,$4,$5,'active',$6::jsonb) RETURNING *""",
            sid, learner, chat_id, department, mode, json.dumps(questions, ensure_ascii=False),
        )
    return row, None


def _mode_title(mode: str) -> str:
    return {"one": "Один вопрос", "quick": "Тренировка", "full": "Полный тест"}.get(mode, "Тест")


async def _show_question(chat_id: int, session: Any) -> None:
    questions = _json(session["questions"], [])
    idx = int(session["current_index"] or 0)
    if idx >= len(questions):
        await _finish_session(chat_id, str(session["id"]))
        return
    q = questions[idx]
    prefix = f"<b>{html.escape(_mode_title(str(session['mode'])))}</b> · вопрос {idx + 1} из {len(questions)}\n\n"
    if q.get("type") == "mcq":
        opts = q.get("options") or []
        lines = [prefix + html.escape(str(q.get("question") or ""))]
        for i, option in enumerate(opts):
            lines.append(f"\n<b>{chr(1040+i)}.</b> {html.escape(str(option))}")
        rows = [
            [InlineKeyboardButton(text=chr(1040+i), callback_data=f"mt:a:{session['id']}:{idx}:{i}"[:64]) for i in range(len(opts))]
        ]
        sent = await BOT.send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        sent = await BOT.send_message(
            chat_id,
            prefix + html.escape(str(q.get("question") or "")) + "\n\n<i>Ответь одним сообщением.</i>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Твой ответ"),
        )
    pool = db_pulse.pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE menu_training_sessions SET prompt_message_id=$2 WHERE id=$1", str(session["id"]), int(sent.message_id))


async def _grade_open(q: dict[str, Any], answer: str) -> dict[str, Any]:
    prompt = {
        "question": q.get("question"),
        "dimension": q.get("dimension"),
        "reference_answer": q.get("model_answer"),
        "source_facts": q.get("facts") or [],
        "learner_answer": answer,
    }
    response = await _ai().responses.create(
        model=MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "Ты проверяешь знание меню ресторана. Оцени только по reference/source_facts. "
                    "Не добавляй факты от себя. Для продающего описания оцени также ясность и привлекательность, "
                    "но штрафуй за любые выдуманные свойства блюда."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        text={"format": {"type": "json_schema", "name": "open_grade", "schema": OPEN_GRADE_SCHEMA, "strict": True}},
    )
    return json.loads(response.output_text)


async def _record_mastery(learner: str, chat_id: int, q: dict[str, Any], value: float) -> None:
    pool = db_pulse.pool()
    now = datetime.now(timezone.utc)
    interval_days = 1 if value < 0.5 else (3 if value < 0.85 else 7)
    due = now + timedelta(days=interval_days)
    success = 1 if value >= 0.7 else 0
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO menu_training_mastery
               (learner_hash,restaurant_chat_id,dish_key,dimension,attempts,successes,mastery,last_seen_at,next_due_at)
               VALUES ($1,$2,$3,$4,1,$5,$6,$7,$8)
               ON CONFLICT (learner_hash,restaurant_chat_id,dish_key,dimension) DO UPDATE SET
                 attempts=menu_training_mastery.attempts+1,
                 successes=menu_training_mastery.successes+$5,
                 mastery=LEAST(1.0, GREATEST(0.0, menu_training_mastery.mastery*0.65 + $6*0.35)),
                 last_seen_at=$7,
                 next_due_at=$8""",
            learner, chat_id, str(q.get("dish_key")), str(q.get("dimension")), success, float(value), now, due,
        )


async def _accept_result(session: Any, q: dict[str, Any], result: dict[str, Any]) -> Any:
    answers = _json(session["answers"], [])
    answers.append(result)
    learner = str(session["learner_hash"])
    chat_id = int(session["restaurant_chat_id"])
    await _record_mastery(learner, chat_id, q, float(result.get("value") or 0))
    pool = db_pulse.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE menu_training_sessions SET answers=$2::jsonb,current_index=current_index+1,prompt_message_id=NULL WHERE id=$1",
            str(session["id"]), json.dumps(answers, ensure_ascii=False),
        )
        return await conn.fetchrow("SELECT * FROM menu_training_sessions WHERE id=$1", str(session["id"]))


async def _finish_session(chat_id: int, session_id: str) -> None:
    pool = db_pulse.pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM menu_training_sessions WHERE id=$1", session_id)
        if not row:
            return
        answers = _json(row["answers"], [])
        score = round(sum(float(a.get("value") or 0) for a in answers) / max(1, len(answers)) * 100, 1)
        await conn.execute(
            "UPDATE menu_training_sessions SET status='completed',score=$2,completed_at=now(),prompt_message_id=NULL WHERE id=$1",
            session_id, score,
        )
    if score >= 85:
        verdict = "✅ Отлично"
    elif score >= 70:
        verdict = "🟡 Зачёт, но есть что повторить"
    else:
        verdict = "🔁 Нужно повторить"
    await BOT.send_message(
        chat_id,
        f"<b>{verdict}</b>\n\nРезультат: <b>{score:.0f}%</b>\n"
        "Слабые места автоматически вернутся в следующих тренировках.",
        parse_mode="HTML",
    )


async def _own_progress(uid: int, chat_id: int, rec: dict[str, Any]) -> str:
    learner = _learner_hash(uid)
    if not await _ensure_schema():
        return "Прогресс временно недоступен."
    dishes = published_dishes(rec)
    pool = db_pulse.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT dish_key,dimension,mastery FROM menu_training_mastery WHERE learner_hash=$1 AND restaurant_chat_id=$2",
            learner, chat_id,
        )
        stats = await conn.fetchrow(
            "SELECT COUNT(*) total, AVG(score) avg_score, MAX(score) best FROM menu_training_sessions WHERE learner_hash=$1 AND restaurant_chat_id=$2 AND status='completed'",
            learner, chat_id,
        )
    by_dish: dict[str, list[float]] = {}
    by_dim: dict[str, list[float]] = {}
    for r in rows:
        by_dish.setdefault(str(r["dish_key"]), []).append(float(r["mastery"] or 0))
        by_dim.setdefault(str(r["dimension"]), []).append(float(r["mastery"] or 0))
    confident = 0
    learning = 0
    for d in dishes:
        vals = by_dish.get(str(d.get("key")), [])
        m = sum(vals) / len(vals) if vals else 0
        if m >= 0.8:
            confident += 1
        elif vals:
            learning += 1
    unseen = max(0, len(dishes) - confident - learning)
    all_vals = [x for vals in by_dish.values() for x in vals]
    confidence = round(sum(all_vals) / len(all_vals) * 100) if all_vals else 0
    weak = sorted(
        ((sum(vals) / len(vals), DIMENSION_LABELS.get(dim, dim)) for dim, vals in by_dim.items()),
        key=lambda x: x[0],
    )[:3]
    weak_text = ", ".join(label for _, label in weak) if weak else "пока данных мало"
    total_tests = int(stats["total"] or 0) if stats else 0
    avg = float(stats["avg_score"] or 0) if stats and stats["avg_score"] is not None else 0
    return (
        "<b>📈 Мой прогресс</b>\n\n"
        f"Уверенность по меню: <b>{confidence}%</b>\n"
        f"Уверенно: <b>{confident}</b> · в изучении: <b>{learning}</b> · не начато: <b>{unseen}</b>\n"
        f"Завершено тестов: <b>{total_tests}</b> · средний результат: <b>{avg:.0f}%</b>\n\n"
        f"Повторить в первую очередь: <b>{html.escape(weak_text)}</b>"
    )


def register(dp: Any) -> None:
    @dp.callback_query(F.data.startswith("mt:m:"))
    async def manager_menu(callback: CallbackQuery) -> None:
        try:
            chat_id = int(callback.data.split(":", 2)[2])
        except Exception:
            await callback.answer("Ошибка", show_alert=True)
            return
        data = await LOAD_DATA()
        if not _manager_can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id)) or {}
        title = str(rec.get("title", chat_id))
        await callback.answer()
        await callback.message.answer(
            _manager_status_text(rec, title),
            parse_mode="HTML",
            reply_markup=_manager_status_keyboard(chat_id, rec),
        )

    @dp.callback_query(F.data.startswith("mt:af:"))
    async def analyse_folder(callback: CallbackQuery) -> None:
        parts = callback.data.split(":", 3)
        if len(parts) != 4:
            await callback.answer("Ошибка", show_alert=True)
            return
        chat_id = int(parts[2])
        folder_id = parts[3]
        data = await LOAD_DATA()
        if not _manager_can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id))
        if not isinstance(rec, dict):
            await callback.answer("Точка не найдена", show_alert=True)
            return
        n = 0
        for f in training_materials.list_files(rec, folder_id):
            if Path(str(f.get("title") or "")).suffix.lower() in SUPPORTED_EXTENSIONS:
                f["analysis_status"] = "pending"
                f["analysis_error"] = ""
                n += 1
        await SAVE_DATA(data)
        await callback.answer(f"Поставлено в анализ: {n}", show_alert=True)

    @dp.callback_query(F.data.startswith("mt:rean:"))
    async def reanalyse_all(callback: CallbackQuery) -> None:
        chat_id = int(callback.data.split(":", 2)[2])
        data = await LOAD_DATA()
        if not _manager_can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id))
        if not isinstance(rec, dict):
            await callback.answer("Точка не найдена", show_alert=True)
            return
        n = 0
        for f in training_materials.list_files(rec):
            if Path(str(f.get("title") or "")).suffix.lower() in SUPPORTED_EXTENSIONS:
                f["analysis_status"] = "pending"
                f["analysis_error"] = ""
                n += 1
        _menu_root(rec)["draft_dishes"] = []
        await SAVE_DATA(data)
        await callback.answer(f"Запущен повторный анализ: {n} файлов", show_alert=True)

    @dp.callback_query(F.data.startswith("mt:pub:"))
    async def publish(callback: CallbackQuery) -> None:
        chat_id = int(callback.data.split(":", 2)[2])
        data = await LOAD_DATA()
        if not _manager_can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id))
        if not isinstance(rec, dict):
            await callback.answer("Точка не найдена", show_alert=True)
            return
        root = _menu_root(rec)
        draft = list(root.get("draft_dishes") or [])
        if not draft:
            await callback.answer("Черновик пуст", show_alert=True)
            return
        root["published_dishes"] = draft
        root["status"] = "published"
        root["version"] = int(root.get("version") or 0) + 1
        root["published_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await SAVE_DATA(data)
        await callback.answer("База опубликована. Тесты доступны сотрудникам.", show_alert=True)

    @dp.callback_query(F.data.startswith("mt:q:"))
    async def start_quiz(callback: CallbackQuery) -> None:
        parts = callback.data.split(":", 3)
        if len(parts) != 4:
            await callback.answer("Ошибка", show_alert=True)
            return
        mode = parts[2]
        chat_id = int(parts[3])
        if mode not in {"one", "quick", "full"}:
            await callback.answer("Неизвестный режим", show_alert=True)
            return
        data = await LOAD_DATA()
        if not _staff_can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа к материалам этой точки", show_alert=True)
            return
        await callback.answer()
        session, error = await _create_session(callback.from_user.id, chat_id, mode)
        if session is None:
            await callback.message.answer(html.escape(error or "Не удалось начать тест"), parse_mode="HTML")
            return
        if error == "active":
            await callback.message.answer(
                "У тебя уже есть незавершённый тест.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="▶️ Продолжить", callback_data=f"mt:resume:{chat_id}"[:64])
                ]]),
            )
            return
        await _show_question(callback.message.chat.id, session)

    @dp.callback_query(F.data.startswith("mt:resume:"))
    async def resume(callback: CallbackQuery) -> None:
        chat_id = int(callback.data.split(":", 2)[2])
        data = await LOAD_DATA()
        if not _staff_can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        session = await _fetch_active(_learner_hash(callback.from_user.id), chat_id)
        await callback.answer()
        if not session:
            await callback.message.answer("Незавершённых тестов нет.")
            return
        await _show_question(callback.message.chat.id, session)

    @dp.callback_query(F.data.startswith("mt:p:"))
    async def progress(callback: CallbackQuery) -> None:
        chat_id = int(callback.data.split(":", 2)[2])
        data = await LOAD_DATA()
        if not _staff_can(data, callback.from_user.id, chat_id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        rec = (data.get("chats") or {}).get(str(chat_id)) or {}
        await callback.answer()
        await callback.message.answer(await _own_progress(callback.from_user.id, chat_id, rec), parse_mode="HTML")

    @dp.callback_query(F.data.startswith("mt:a:"))
    async def answer_mcq(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 5:
            await callback.answer("Ошибка", show_alert=True)
            return
        _, _, sid, idx_s, choice_s = parts
        pool = db_pulse.pool()
        if pool is None:
            await callback.answer("База недоступна", show_alert=True)
            return
        async with pool.acquire() as conn:
            session = await conn.fetchrow("SELECT * FROM menu_training_sessions WHERE id=$1", sid)
        if not session or session["status"] != "active" or str(session["learner_hash"]) != _learner_hash(callback.from_user.id):
            await callback.answer("Вопрос уже закрыт", show_alert=True)
            return
        idx = int(idx_s)
        if int(session["current_index"] or 0) != idx:
            await callback.answer("Ответ уже принят", show_alert=True)
            return
        questions = _json(session["questions"], [])
        q = questions[idx]
        choice = int(choice_s)
        correct = choice == int(q.get("correct_index", -1))
        value = 1.0 if correct else 0.0
        await callback.answer("Верно" if correct else "Неверно")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        if correct:
            await callback.message.answer("✅ Верно")
        else:
            await callback.message.answer(f"❌ Правильный ответ: <b>{html.escape(str(q.get('model_answer') or ''))}</b>", parse_mode="HTML")
        updated = await _accept_result(session, q, {"value": value, "correct": correct, "answer": choice})
        if int(updated["current_index"] or 0) >= len(questions):
            await _finish_session(callback.message.chat.id, sid)
        else:
            await _show_question(callback.message.chat.id, updated)

    @dp.message(F.text, F.chat.type == "private")
    async def answer_open(message: Message) -> None:
        # Only consume a text message when it is a reply to the current forced-reply menu question.
        if not message.reply_to_message:
            return
        if not await _ensure_schema():
            return
        learner = _learner_hash(message.from_user.id)
        pool = db_pulse.pool()
        async with pool.acquire() as conn:
            session = await conn.fetchrow(
                "SELECT * FROM menu_training_sessions WHERE learner_hash=$1 AND status='active' ORDER BY created_at DESC LIMIT 1",
                learner,
            )
        if not session:
            return
        prompt_id = session["prompt_message_id"]
        if not prompt_id or int(message.reply_to_message.message_id) != int(prompt_id):
            return
        questions = _json(session["questions"], [])
        idx = int(session["current_index"] or 0)
        if idx >= len(questions):
            return
        q = questions[idx]
        if q.get("type") != "open":
            return
        await message.answer("Проверяю…")
        try:
            grade = await _grade_open(q, message.text.strip())
        except Exception as exc:
            await message.answer(f"Не удалось проверить ответ: {html.escape(str(exc))}. Ответ не потерян — попробуй ещё раз.", parse_mode="HTML")
            return
        value = max(0.0, min(1.0, float(grade.get("score") or 0) / 10.0))
        correct = bool(grade.get("correct"))
        feedback = html.escape(str(grade.get("feedback") or ""))
        ideal = html.escape(str(grade.get("ideal_answer") or q.get("model_answer") or ""))
        await message.answer(
            f"<b>{'✅' if correct else '🔁'} {float(grade.get('score') or 0):.0f}/10</b>\n{feedback}\n\n"
            f"<b>Сильный ответ:</b> {ideal}",
            parse_mode="HTML",
        )
        updated = await _accept_result(
            session,
            q,
            {"value": value, "correct": correct, "answer": message.text.strip(), "feedback": str(grade.get("feedback") or "")},
        )
        if int(updated["current_index"] or 0) >= len(questions):
            await _finish_session(message.chat.id, str(session["id"]))
        else:
            await _show_question(message.chat.id, updated)
