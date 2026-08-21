"""
AI-наставник управляющего.

Читает анонимные комментарии и оценки, ищет повторы и корреляции,
пишет в личку: что заметил → к чему ведёт → что сделать → вопросы тет-а-тет
→ короткая ссылка «почитать, если хотите расти».

Не видит имён. Только цифры, темы и тексты отзывов.
"""
from __future__ import annotations

import json
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

# Минимум похожих комментариев, чтобы считать «тенденцию»
TREND_MIN_COMMENTS = 3
# Сколько цитат максимум в промпт
MAX_COMMENTS_IN_PROMPT = 20

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
    "comment_trend": "повторяющаяся тема в комментариях",
}

# Если управ хочет расти — короткая ссылка в конце совета (по теме)
GROWTH_READINGS: dict[str, tuple[str, str]] = {
    "team": (
        "Спиральная динамика (уровни ценностей в команде)",
        "https://ru.wikipedia.org/wiki/%D0%A1%D0%BF%D0%B8%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F_%D0%B4%D0%B8%D0%BD%D0%B0%D0%BC%D0%B8%D0%BA%D0%B0",
    ),
    "conflict": (
        "Спиральная динамика и конфликты ценностей",
        "https://ru.wikipedia.org/wiki/%D0%A1%D0%BF%D0%B8%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F_%D0%B4%D0%B8%D0%BD%D0%B0%D0%BC%D0%B8%D0%BA%D0%B0",
    ),
    "self": (
        "Выгорание и восстановление (WHO)",
        "https://www.who.int/news-room/questions-and-answers/item/mental-health-occupational-stress",
    ),
    "stress": (
        "Выгорание и нагрузка на смене",
        "https://www.who.int/news-room/questions-and-answers/item/mental-health-occupational-stress",
    ),
    "kitchen": (
        "Системное мышление в операционке",
        "https://ru.wikipedia.org/wiki/%D0%A1%D0%B8%D1%81%D1%82%D0%B5%D0%BC%D0%BD%D0%BE%D0%B5_%D0%BC%D1%8B%D1%88%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5",
    ),
    "processes": (
        "Системное мышление: процессы, а не люди",
        "https://ru.wikipedia.org/wiki/%D0%A1%D0%B8%D1%81%D1%82%D0%B5%D0%BC%D0%BD%D0%BE%D0%B5_%D0%BC%D1%8B%D1%88%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5",
    ),
    "management": (
        "Системное мышление: процессы, а не люди",
        "https://ru.wikipedia.org/wiki/%D0%A1%D0%B8%D1%81%D1%82%D0%B5%D0%BC%D0%BD%D0%BE%D0%B5_%D0%BC%D1%8B%D1%88%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5",
    ),
    "guests": (
        "Сервис и ожидания гостя",
        "https://ru.wikipedia.org/wiki/%D0%A3%D0%BF%D1%80%D0%B0%D0%B2%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5_%D0%BE%D0%BF%D1%8B%D1%82%D0%BE%D0%BC_%D0%BA%D0%BB%D0%B8%D0%B5%D0%BD%D1%82%D0%B0",
    ),
    "staff": (
        "Спиральная динамика (уровни ценностей в команде)",
        "https://ru.wikipedia.org/wiki/%D0%A1%D0%BF%D0%B8%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F_%D0%B4%D0%B8%D0%BD%D0%B0%D0%BC%D0%B8%D0%BA%D0%B0",
    ),
    "rating_drop": (
        "Спиральная динамика и зрелость команды",
        "https://ru.wikipedia.org/wiki/%D0%A1%D0%BF%D0%B8%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F_%D0%B4%D0%B8%D0%BD%D0%B0%D0%BC%D0%B8%D0%BA%D0%B0",
    ),
    "comment_trend": (
        "Спиральная динамика — как команды взрослеют",
        "https://ru.wikipedia.org/wiki/%D0%A1%D0%BF%D0%B8%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F_%D0%B4%D0%B8%D0%BD%D0%B0%D0%BC%D0%B8%D0%BA%D0%B0",
    ),
}

DEFAULT_READING = GROWTH_READINGS["comment_trend"]

SYSTEM_PROMPT = """\
Ты — ментальный наставник управляющего ресторана. Не дашборд и не «критика\
 сверху». Твоя цель — рост управляющего и команды вместе.

Ты видишь только анонимные данные: оценки смен, кнопки «что мешало», тексты\
 комментариев. Имён нет — ищи не «кто виноват», а повторяющиеся паттерны.

Обязательно:
1. Найди тенденцию или корреляцию (например: «тяжелые» смены + «команда» +\
 похожие формулировки в комментариях).
2. Скажи, к чему это приведёт через 2–4 недели, если не трогать\
 (выгорание, уходы, срыв сервиса, падение выручки — только логичные следствия).
3. Дай 2–3 конкретных действия. Первое — сегодня. Без «поговорите с командой»\
 в вакууме: как именно.
4. Дай 3–4 вопроса для тет-а-тет, чтобы человек сам покопался в себе\
 («Как ты…», «Что, по-твоему…»). Не допрос.
5. Закончить короткой фразой поддержки роста управляющего.

Тон: «Дорогой управляющий…» — тёплый, прямой, без пафоса и без давления.

Пиши на русском. 350–500 слов. Без markdown, без списков с маркерами —\
 живые абзацы. Не выдумывай цитаты, которых нет во входных данных.\
"""

TREND_DETECT_PROMPT = """\
Ты аналитик анонимных отзывов персонала ресторана. По списку комментариев\
 и отметок найди ПОВТОРЯЮЩИЕСЯ проблемы (одна и та же суть разными словами).

Верни ТОЛЬКО JSON-массив (без markdown), до 3 объектов:
[
  {
    "theme_code": "team|kitchen|guests|processes|self|other",
    "title": "короткое название тенденции по-русски",
    "count_estimate": 3,
    "evidence": ["короткая цитата 1", "цитата 2"],
    "future_risk": "к чему приведёт за 2–4 недели",
    "first_action": "что сделать сегодня"
  }
]

Если повторов меньше чем в 3 комментариях или всё разное — верни [].\
 Не выдумывай цитаты. Не включай имена.\
"""


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


def growth_reading_for(code: str | None) -> tuple[str, str]:
    if code and code in GROWTH_READINGS:
        return GROWTH_READINGS[code]
    return DEFAULT_READING


def extract_comments(events: list[report_pulse.EventRow], *, limit: int = MAX_COMMENTS_IN_PROMPT) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for e in events:
        if e.event_type != report_pulse.EVENT_COMMENT:
            continue
        raw = re.sub(r"\s+", " ", (e.comment_text or "").strip())
        if len(raw) < 8:
            continue
        key = raw[:90].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(raw[:280])
        if len(out) >= limit:
            break
    return out


def extract_problem_summary(events: list[report_pulse.EventRow]) -> str:
    from collections import Counter

    c: Counter[str] = Counter()
    for e in events:
        if e.event_type == report_pulse.EVENT_PROBLEM and e.problem_code:
            c[e.problem_code] += 1
    if not c:
        return "Отметок «что мешало» почти нет."
    parts = []
    for code, n in c.most_common(6):
        parts.append(f"{PROBLEM_RU.get(code, code)} — {n}")
    return "Отметки «что мешало»: " + "; ".join(parts)


def extract_rating_summary(events: list[report_pulse.EventRow]) -> str:
    vals = [
        e.rating
        for e in events
        if e.event_type == report_pulse.EVENT_RATING and e.rating is not None
    ]
    if not vals:
        return "Оценок смены за окно нет."
    avg = sum(vals) / len(vals)
    low = sum(1 for v in vals if v <= 2)
    return f"Оценок: {len(vals)}, средняя {avg:.1f}, тяжёлых (≤2): {low}."


def _alert_to_context(alert: ma.ManagerAlert, *, title: str) -> str:
    kind_ru = {
        "spike": "резкий рост",
        "new_theme": "новая повторяющаяся тема",
        "hot": "тема продолжает гореть",
        "rating_drop": "падение средней оценки",
        "improved": "улучшение",
        "comment_trend": "повтор в свободных комментариях",
    }.get(alert.kind, alert.kind)

    problem_name = PROBLEM_RU.get(alert.code or "", alert.code or "общее")
    body = " ".join(alert.body_lines)
    body = re.sub(r"<[^>]+>", "", body).strip()

    parts = [
        f"Точка: {title}",
        f"Сигнал: {kind_ru} — тема «{problem_name}»",
        f"Что зафиксировано: {body}",
    ]
    if alert.comments:
        parts.append("Анонимные цитаты сотрудников:")
        for c in alert.comments[:8]:
            parts.append(f"  – «{c}»")
    return "\n".join(parts)


def events_to_context(
    cur_events: list[report_pulse.EventRow],
    *,
    title: str,
    alert: ma.ManagerAlert | None = None,
) -> str:
    parts = [f"Точка: {title}", extract_rating_summary(cur_events), extract_problem_summary(cur_events)]
    if alert:
        parts.append("")
        parts.append(_alert_to_context(alert, title=title))
    comments = extract_comments(cur_events)
    if comments:
        parts.append("")
        parts.append(f"Все анонимные комментарии за окно ({len(comments)}):")
        for c in comments:
            parts.append(f"  – «{c}»")
    else:
        parts.append("Свободных комментариев за окно почти нет.")
    return "\n".join(parts)


def append_growth_footer(advice: str, *, theme_code: str | None) -> str:
    label, url = growth_reading_for(theme_code)
    footer = (
        f"\n\nПодробнее изучить тему «{label}» можно здесь: {url}\n"
        "Если хотите расти как управляющий — перейдите и почитайте. Это не обязательно, "
        "это для тех, кто хочет глубже."
    )
    if footer.strip() in advice:
        return advice
    return advice.rstrip() + footer


async def build_advice(
    alert: ma.ManagerAlert,
    *,
    restaurant_title: str,
    extra_context: str | None = None,
    events: list[report_pulse.EventRow] | None = None,
) -> str | None:
    client = _client_or_none()
    if client is None:
        return None
    if events:
        context = events_to_context(events, title=restaurant_title, alert=alert)
    else:
        context = _alert_to_context(alert, title=restaurant_title)
    if extra_context:
        context += f"\n\nДополнительно:\n{extra_context}"
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=900,
            temperature=0.65,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return None
        return append_growth_footer(text, theme_code=alert.code)
    except Exception as e:
        print(f"[ai-advisor] OpenAI error: {e}")
        return None


async def detect_comment_trends(
    events: list[report_pulse.EventRow],
) -> list[dict[str, Any]]:
    """LLM ищет повторы в свободных комментариях. [] если мало данных или нет ключа."""
    client = _client_or_none()
    if client is None:
        return []
    comments = extract_comments(events, limit=MAX_COMMENTS_IN_PROMPT)
    if len(comments) < TREND_MIN_COMMENTS:
        return []
    payload = {
        "ratings": extract_rating_summary(events),
        "problems": extract_problem_summary(events),
        "comments": comments,
    }
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=600,
            temperature=0.2,
            messages=[
                {"role": "system", "content": TREND_DETECT_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data[:3]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            evidence = item.get("evidence") or []
            if not isinstance(evidence, list):
                evidence = []
            out.append(
                {
                    "theme_code": str(item.get("theme_code") or "comment_trend").strip()
                    or "comment_trend",
                    "title": title[:120],
                    "count_estimate": int(item.get("count_estimate") or len(evidence) or 0),
                    "evidence": [str(x)[:180] for x in evidence[:4] if str(x).strip()],
                    "future_risk": str(item.get("future_risk") or "").strip()[:300],
                    "first_action": str(item.get("first_action") or "").strip()[:300],
                }
            )
        return out
    except Exception as e:
        print(f"[ai-trends] {e}")
        return []


def trend_to_alert(trend: dict[str, Any]) -> ma.ManagerAlert:
    code = str(trend.get("theme_code") or "comment_trend")
    title = str(trend.get("title") or "Повтор в комментариях")
    body = [
        f"В свободных отзывах линии повторяется тема: <b>{escape(title)}</b>.",
    ]
    n = int(trend.get("count_estimate") or 0)
    if n:
        body.append(f"Похожих сигналов примерно: <b>{n}</b>.")
    risk = str(trend.get("future_risk") or "").strip()
    if risk:
        body.append(f"Если не трогать: {escape(risk)}")
    action = str(trend.get("first_action") or "").strip()
    rec = action or ma.RECOMMENDATIONS.get(code, ma.RECOMMENDATIONS["rating_drop"])
    evidence = trend.get("evidence") or []
    return ma.ManagerAlert(
        kind="comment_trend",
        code=code if code in PROBLEM_RU else "comment_trend",
        title=f"Тенденция в отзывах: {title}",
        body_lines=body,
        recommendation=rec,
        comments=[str(x) for x in evidence][:4],
        priority=1,
        problem_key=code if code not in ("other", "comment_trend") else "comment_trend",
    )


async def build_advice_from_events(
    cur_events: list[report_pulse.EventRow],
    prev_events: list[report_pulse.EventRow],
    *,
    restaurant_title: str,
    data: dict[str, Any],
    chat_id: int,
) -> str | None:
    alerts = ma.detect_alerts(cur_events, prev_events, data=data, chat_id=chat_id)
    trends = await detect_comment_trends(cur_events)
    if trends:
        alert = trend_to_alert(trends[0])
    elif alerts:
        alert = alerts[0]
    else:
        # Нет порога — но если есть комментарии, всё равно даём мягкий разбор недели
        comments = extract_comments(cur_events)
        if len(comments) < 2:
            return None
        alert = ma.ManagerAlert(
            kind="comment_trend",
            code="comment_trend",
            title="Разбор голоса смены",
            body_lines=[
                "За окно нет жёсткого порога по кнопкам, но есть свободные отзывы — "
                "разберите повторы и тон.",
            ],
            recommendation="Прочитайте цитаты и отметьте, что повторяется без имён.",
            comments=comments[:5],
            priority=2,
            problem_key="comment_trend",
        )
    return await build_advice(
        alert, restaurant_title=restaurant_title, events=cur_events
    )


async def mentor_pack_for_events(
    cur_events: list[report_pulse.EventRow],
    prev_events: list[report_pulse.EventRow],
    *,
    restaurant_title: str,
    data: dict[str, Any],
    chat_id: int,
) -> tuple[ma.ManagerAlert | None, str | None]:
    """
    Возвращает (alert_для_горящего, html_совета).
    Если в комментариях нашлась тенденция — alert kind=comment_trend.
    """
    trends = await detect_comment_trends(cur_events)
    alerts = ma.detect_alerts(cur_events, prev_events, data=data, chat_id=chat_id)
    alert: ma.ManagerAlert | None = None
    if trends:
        alert = trend_to_alert(trends[0])
    elif alerts and alerts[0].kind != "improved":
        alert = alerts[0]
    if alert is None:
        return None, None
    advice = await build_advice(
        alert, restaurant_title=restaurant_title, events=cur_events
    )
    return alert, advice


def format_advice_html(advice: str) -> str:
    lines = [line for line in advice.split("\n") if line.strip()]
    out = ["🤖 <b>AI-наставник</b>\n"]
    for ln in lines:
        # Ссылки оставляем кликабельными: escape всё, потом вернуть http(s)
        esc = escape(ln)
        esc = re.sub(
            r"(https://[^\s<]+)",
            r'<a href="\1">\1</a>',
            esc,
        )
        out.append(esc)
    return "\n".join(out)


async def transcribe_voice(file_bytes: bytes, *, filename: str = "voice.ogg") -> str | None:
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
