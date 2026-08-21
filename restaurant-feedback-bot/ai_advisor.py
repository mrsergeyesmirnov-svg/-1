"""
AI-советник для управляющего.

Принцип: данные смен уже собраны в ManagerAlert. AI не видит имена — только
цифры, темы и анонимные цитаты. Советует конкретно: что происходит, чем это
грозит, что сделать, как провести разговор с человеком.

Вызывается:
  1. Автоматически после push-алерта (триггер → совет в ту же личку).
  2. По команде /ai_advice в личке менеджера (за последние 72 ч по точке).
"""
from __future__ import annotations

import os
import re
from html import escape
from typing import Any

try:
    from openai import AsyncOpenAI
    _openai_available = True
except ImportError:
    _openai_available = False

import report_pulse
import manager_alerts as ma

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
_client: "AsyncOpenAI | None" = None

DEPT_LABELS = {
    "kitchen": "кухня",
    "hall": "зал",
    "floor": "зал",
}

PROBLEM_RU = {
    "kitchen": "кухня / отдача",
    "team": "команда",
    "staff": "команда / нехватка людей",
    "processes": "процессы / организация",
    "management": "процессы / организация",
    "conflict": "конфликт / напряжение",
    "stress": "высокая нагрузка",
    "self": "состояние людей на смене",
    "guests": "гости / сервис",
    "rating_drop": "падение оценок",
    "improved": "улучшение",
}


def _client_or_none() -> "AsyncOpenAI | None":
    global _client
    if not _openai_available:
        return None
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    if _client is None:
        _client = AsyncOpenAI(api_key=key)
    return _client


def _alert_to_context(alert: ma.ManagerAlert, *, title: str) -> str:
    """Сжатый факт из алерта для промпта."""
    kind_ru = {
        "spike": "резкий рост",
        "new_theme": "новая повторяющаяся тема",
        "hot": "тема продолжает гореть",
        "rating_drop": "падение средней оценки",
        "improved": "улучшение",
    }.get(alert.kind, alert.kind)

    problem_name = PROBLEM_RU.get(alert.code or "", alert.code or "общее")
    body = " ".join(line.replace("<b>", "").replace("</b>", "") for line in alert.body_lines)
    # Снять HTML-теги
    body = re.sub(r"<[^>]+>", "", body).strip()

    parts = [
        f"Точка: {title}",
        f"Сигнал: {kind_ru} — тема «{problem_name}»",
        f"Что зафиксировано: {body}",
    ]
    if alert.comments:
        parts.append("Анонимные цитаты сотрудников:")
        for c in alert.comments[:3]:
            parts.append(f"  – «{c}»")
    return "\n".join(parts)


SYSTEM_PROMPT = """\
Ты — AI-наставник для управляющих ресторана. Твоя задача: не просто \
сообщать о проблеме, а помочь управляющему её решить так, чтобы команда \
росла, а не прогибалась под давлением.

Тон: тёплый, прямой, без пафоса. Ты на стороне управляющего и команды \
одновременно. Не морализируй. Не давай банальных советов.

Структура ответа (строго, без заголовков разделов в виде текста — только \
живые абзацы):

1. Обращение по роли (Дорогой управляющий / менеджер).
2. Что я заметил — факты, без имён, с уважением к команде.
3. Почему это важно — что будет, если не отреагировать (конкретно: \
выгорание, сервис, выручка — только то, что реально вытекает).
4. Что рекомендую сделать — 2–3 конкретных действия. Первое — прямо сегодня.
5. Как провести разговор тет-а-тет — если нужен разговор с человеком, \
дай 3–4 вопроса. Вопросы должны помочь человеку самому прийти к ответу, \
не создавать ощущение допроса. Начинай с «Как ты...» или «Что, по-твоему...».
6. Один короткий итог — чего ты хочешь для управляющего и команды.

Пиши на русском. Максимум 450 слов. Без markdown-форматирования, без \
заголовков и списков — только живой текст абзацами.\
"""


async def build_advice(
    alert: ma.ManagerAlert,
    *,
    restaurant_title: str,
    extra_context: str | None = None,
) -> str | None:
    """
    Возвращает текст совета или None если OpenAI недоступен.
    Никогда не кидает исключение наружу.
    """
    client = _client_or_none()
    if client is None:
        return None
    context = _alert_to_context(alert, title=restaurant_title)
    if extra_context:
        context += f"\n\nДополнительно:\n{extra_context}"
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=700,
            temperature=0.7,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
        )
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:
        print(f"[ai-advisor] OpenAI error: {e}")
        return None


async def build_advice_from_events(
    cur_events: list[report_pulse.EventRow],
    prev_events: list[report_pulse.EventRow],
    *,
    restaurant_title: str,
    data: dict[str, Any],
    chat_id: int,
) -> str | None:
    """
    Подбирает главный алерт из событий и генерирует совет.
    Используется для /ai_advice без отдельного алерта.
    """
    alerts = ma.detect_alerts(cur_events, prev_events, data=data, chat_id=chat_id)
    if not alerts:
        return None
    # Берём самый острый
    alert = alerts[0]
    return await build_advice(alert, restaurant_title=restaurant_title)


def format_advice_html(advice: str) -> str:
    """Оборачивает совет в Telegram HTML."""
    lines = [line for line in advice.split("\n") if line.strip()]
    out = ["🤖 <b>AI-советник</b>\n"]
    out.extend(escape(ln) for ln in lines)
    return "\n".join(out)


async def transcribe_voice(file_bytes: bytes, *, filename: str = "voice.ogg") -> str | None:
    """Whisper: голос → текст. Тот же OPENAI_API_KEY. None если ключа нет или ошибка."""
    client = _client_or_none()
    if client is None:
        return None
    try:
        import io

        buf = io.BytesIO(file_bytes)
        buf.name = filename
        resp = await client.audio.transcriptions.create(
            model="whisper-1",
            file=buf,
            language="ru",
        )
        text = (getattr(resp, "text", None) or str(resp) or "").strip()
        return text or None
    except Exception as e:
        print(f"[whisper] {e}")
        return None
