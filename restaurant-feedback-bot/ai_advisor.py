"""
AI-наставник управляющего.

Читает анонимные комментарии, кнопки «что мешало» и оценки.
Ищет повторы по всем темам: команда, кухня, гости, процессы, состояние.
Пишет в личку управу: тенденция → к чему ведёт → что сделать → вопросы тет-а-тет
→ ссылка «почитать, если хотите расти».

Работает и без OpenAI (ключевые слова + шаблон), с ключом — глубже.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
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

TREND_MIN_COMMENTS = 3
TREND_MIN_BUTTONS = 3
MAX_COMMENTS_IN_PROMPT = 20

# Кластеры по всем темам опроса — срабатывают без OpenAI
KEYWORD_CLUSTERS: list[dict[str, Any]] = [
    {
        "theme_code": "team",
        "title": "Напряжение / давление в команде",
        "keys": (
            "булл", "bull", "травл", "травят", "униж", "унижает", "оскорб",
            "кричит", "крик", "орет", "орёт", "хам", "груб", "токсич",
            "зажим", "давлени", "запугив", "мобб", "конфликт", "ссор",
            "коллег", "сменщик", "не сработа", "команд",
        ),
        "future_risk": (
            "люди замолчат или уйдут; сервис просядет, конфликты станут нормой за 2–4 недели"
        ),
        "first_action": (
            "сегодня тет-а-тет без публичного разбора: "
            "«что помогает чувствовать себя в безопасности на смене»"
        ),
        "both_sides": (
            "Вы учитесь держать психологическую безопасность; "
            "линия учится говорить о напряжении без страха и без доноса."
        ),
        "questions": (
            "Как ты себя чувствуешь после таких смен?",
            "Что, по-твоему, больше всего давит в команде?",
            "Есть ли что-то, о чём сложно сказать на планёрке?",
            "Что помогло бы тебе и коллегам работать спокойнее?",
        ),
    },
    {
        "theme_code": "kitchen",
        "title": "Медленная кухня / срыв отдачи",
        "keys": (
            "отдач", "кухн", "не успева", "завал", "ждал", "ждали",
            "медленн", "раздач", "горяч", "заказ вис", "повар",
        ),
        "future_risk": (
            "зал начнёт злиться на кухню, гости — на сервис; вырастут отказы и плохие отзывы"
        ),
        "first_action": (
            "сегодня с шефом разберите один пик: где ломается тайминг и кто закрывает дыры чужими руками"
        ),
        "both_sides": (
            "Вы учитесь чинить систему отдачи, а не искать виноватых; "
            "кухня и зал учатся говорить о пике одним языком и страховать друг друга."
        ),
        "questions": (
            "Где на пике тебе больше всего не хватает рук или ясности?",
            "Что, по-твоему, чаще всего срывает отдачу?",
            "Как бы ты перестроил приоритеты на раздаче?",
            "Что должно измениться завтра, чтобы смена прошла легче?",
        ),
    },
    {
        "theme_code": "guests",
        "title": "Сложные гости / сервис под давлением",
        "keys": (
            "гост", "клиент", "жалоб", "скандал", "хам гост", "недовольн",
            "чаевы", "претенз", "конфликт с гост", "сервис",
        ),
        "future_risk": (
            "линия выгорит на сложных гостях; сервис станет формальным, вырастут негативные отзывы"
        ),
        "first_action": (
            "сегодня зафиксируйте 1–2 сценария «сложный гость»: кто подключается, какие слова можно говорить"
        ),
        "both_sides": (
            "Вы учитесь ставить рамки сервиса и подхватывать линию; "
            "сотрудники учатся не оставаться один на один со сложным гостем."
        ),
        "questions": (
            "Какой гость сегодня забрал больше всего сил?",
            "Чего тебе не хватило, чтобы закрыть ситуацию спокойно?",
            "Как, по-твоему, команда может страховать друг друга с гостями?",
            "Что помогло бы тебе не уносить смену домой?",
        ),
    },
    {
        "theme_code": "processes",
        "title": "Сломанные процессы / организация смены",
        "keys": (
            "процесс", "бардак", "хаос", "непонятн", "организац", "график",
            "зоны", "открыти", "закрыти", "никто не", "не сказали",
            "путаниц", "роль", "обязанност",
        ),
        "future_risk": (
            "ошибки начнут списывать на людей, а не на систему; смены станут тяжёлыми и конфликтными"
        ),
        "first_action": (
            "сегодня пройдите открытие или закрытие вместе с линией и выпишите 3 дыры в ролях"
        ),
        "both_sides": (
            "Вы учитесь строить понятную систему; "
            "линия учится предлагать улучшения, а не только жаловаться на хаос."
        ),
        "questions": (
            "Где тебе чаще всего непонятно, кто за что отвечает?",
            "Что, по-твоему, ломается в процессе чаще всего?",
            "Если бы ты мог упростить одну вещь на смене — что бы это было?",
            "Как мы поймём через неделю, что процесс стал лучше?",
        ),
    },
    {
        "theme_code": "self",
        "title": "Тяжёлое состояние / выгорание линии",
        "keys": (
            "выгоран", "нет сил", "сил нет", "устал", "устала", "не вывоз",
            "сломал", "плак", "депресс", "тревог", "настроен", "мораль",
            "не хочу", "тяжело мне", "состояние",
        ),
        "future_risk": (
            "тихие уходы, больничные и падение качества — люди уйдут раньше, чем скажут вслух"
        ),
        "first_action": (
            "проверьте переработки и плотность ближайших смен; тет-а-тет без фразы «соберись»"
        ),
        "both_sides": (
            "Вы учитесь слышать усталость до увольнения; "
            "человек учится просить поддержку раньше, чем сломается."
        ),
        "questions": (
            "Как ты себя чувствуешь последние несколько смен?",
            "Что больше всего забирает силы?",
            "Что, по-твоему, помогло бы тебе восстановиться?",
            "Есть ли смена или зона, после которой тебе особенно тяжело?",
        ),
    },
]

BUTTON_TREND_META: dict[str, dict[str, str]] = {
    "team": {
        "title": "Команда мешает работать",
        "future_risk": "напряжение в команде станет нормой, вырастут уходы и тихий саботаж",
        "first_action": "сегодня разберите расстановку и один конфликтный узел без публичного суда",
        "both_sides": (
            "Вы учитесь держать безопасность команды; "
            "линия учится говорить о давлении без страха."
        ),
    },
    "kitchen": {
        "title": "Кухня / отдача мешает смене",
        "future_risk": "зал и кухня разъедутся; сервис и настроение просядут",
        "first_action": "сегодня с шефом разберите пик и приоритеты отдачи",
        "both_sides": (
            "Вы чините систему отдачи; кухня и зал учатся одному языку на пике."
        ),
    },
    "guests": {
        "title": "Гости давят на линию",
        "future_risk": "линия начнёт избегать сложных столов и терять сервис",
        "first_action": "сегодня зафиксируйте сценарий подхвата сложного гостя",
        "both_sides": (
            "Вы ставите рамки и подхват; сотрудники учатся не оставаться один на один."
        ),
    },
    "processes": {
        "title": "Процессы не держат смену",
        "future_risk": "ошибки спишут на людей; хаос закрепится",
        "first_action": "сегодня пройдите открытие/закрытие и закройте 3 дыры в ролях",
        "both_sides": (
            "Вы строите ясные роли; линия предлагает улучшения, а не только жалуется."
        ),
    },
    "self": {
        "title": "Состояние людей на смене тяжёлое",
        "future_risk": "выгорание и уходы ускорятся",
        "first_action": "проверьте график и переработки; тет-а-тет с теми, кто тянет больше всех",
        "both_sides": (
            "Вы слышите усталость до ухода; человек учится просить помощь раньше."
        ),
    },
    "staff": {
        "title": "Нехватка людей",
        "future_risk": "перегруз оставшихся и новые уходы",
        "first_action": "сверьте зоны и подмены на ближайшие три смены",
        "both_sides": (
            "Вы честно закрываете дыры в штате; оставшиеся видят, что их не «дожимают» молча."
        ),
    },
    "management": {
        "title": "Организация смены хромает",
        "future_risk": "хаос станет привычным",
        "first_action": "пройдите планёрку и зоны ответственности до пика",
        "both_sides": (
            "Вы усиливаете организацию; команда получает ясность и меньше винит людей."
        ),
    },
    "conflict": {
        "title": "Конфликт / напряжение",
        "future_risk": "команда разделится, сервис просядет",
        "first_action": "тет-а-тет по конфликтным точкам без публичного разбора",
        "both_sides": (
            "Вы учитесь разбирать конфликт без суда; стороны учатся говорить по делу."
        ),
    },
    "stress": {
        "title": "Сильная нагрузка",
        "future_risk": "выгорание и ошибки на пике",
        "first_action": "пересмотрите плотность смен и переработки",
        "both_sides": (
            "Вы балансируете нагрузку; линия учится сигналить о перегрузе вовремя."
        ),
    },
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
    "comment_trend": "повторяющаяся тема в комментариях",
}

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
Ты — ментальный наставник управляющего ресторана. Цель — рост управляющего\
 и команды вместе, не «накричать на смену».

Данные анонимны. Ищи повторы и корреляции по темам: команда, кухня, гости,\
 процессы, состояние людей.

Структура ответа живыми абзацами на русском:
1. «Дорогой управляющий…» — что заметил (тенденция), без имён.
2. К чему приведёт за 2–4 недели, если не трогать.
3. 2–3 конкретных действия (первое — сегодня), чтобы выросли обе стороны.
4. 3–4 вопроса для тет-а-тет («Как ты…», «Что, по-твоему…») — не допрос.
5. Короткая фраза поддержки роста.

350–500 слов. Без markdown и маркеров. Не выдумывай цитаты.\
"""

TREND_DETECT_PROMPT = """\
Найди ПОВТОРЯЮЩИЕСЯ проблемы в анонимных отзывах персонала ресторана.\
 Темы: команда/буллинг, кухня/отдача, гости, процессы, состояние/выгорание.

Верни ТОЛЬКО JSON-массив до 3 объектов:
[{"theme_code":"team|kitchen|guests|processes|self|other","title":"...","count_estimate":3,"evidence":["..."],"future_risk":"...","first_action":"..."}]
Если повторов <3 — []. Без markdown. Без имён. Не выдумывай цитаты.\
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


def extract_comments(
    events: list[report_pulse.EventRow], *, limit: int = MAX_COMMENTS_IN_PROMPT
) -> list[str]:
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
    c: Counter[str] = Counter()
    for e in events:
        if e.event_type == report_pulse.EVENT_PROBLEM and e.problem_code:
            c[e.problem_code] += 1
    if not c:
        return "Отметок «что мешало» почти нет."
    parts = [f"{PROBLEM_RU.get(code, code)} — {n}" for code, n in c.most_common(6)]
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
        "comment_trend": "повтор в отзывах / кнопках",
    }.get(alert.kind, alert.kind)
    problem_name = PROBLEM_RU.get(alert.code or "", alert.code or "общее")
    body = re.sub(r"<[^>]+>", "", " ".join(alert.body_lines)).strip()
    parts = [
        f"Точка: {title}",
        f"Сигнал: {kind_ru} — тема «{problem_name}»",
        f"Что зафиксировано: {body}",
    ]
    if alert.comments:
        parts.append("Анонимные цитаты:")
        for c in alert.comments[:8]:
            parts.append(f"  – «{c}»")
    return "\n".join(parts)


def events_to_context(
    cur_events: list[report_pulse.EventRow],
    *,
    title: str,
    alert: ma.ManagerAlert | None = None,
) -> str:
    parts = [
        f"Точка: {title}",
        extract_rating_summary(cur_events),
        extract_problem_summary(cur_events),
    ]
    if alert:
        parts.extend(["", _alert_to_context(alert, title=title)])
    comments = extract_comments(cur_events)
    if comments:
        parts.append("")
        parts.append(f"Анонимные комментарии ({len(comments)}):")
        for c in comments:
            parts.append(f"  – «{c}»")
    return "\n".join(parts)


def append_growth_footer(advice: str, *, theme_code: str | None) -> str:
    label, url = growth_reading_for(theme_code)
    footer = (
        f"\n\nПодробнее изучить тему «{label}» можно здесь: {url}\n"
        "Если хотите расти как управляющий — перейдите и почитайте. Не обязательно, "
        "это для тех, кто хочет глубже."
    )
    if footer.strip() in advice:
        return advice
    return advice.rstrip() + footer


def detect_keyword_trends(
    events: list[report_pulse.EventRow],
) -> list[dict[str, Any]]:
    comments = extract_comments(events, limit=MAX_COMMENTS_IN_PROMPT)
    if len(comments) < TREND_MIN_COMMENTS:
        return []
    out: list[dict[str, Any]] = []
    for cluster in KEYWORD_CLUSTERS:
        hits = [c for c in comments if any(k in c.casefold() for k in cluster["keys"])]
        if len(hits) < TREND_MIN_COMMENTS:
            continue
        out.append(
            {
                "theme_code": cluster["theme_code"],
                "title": cluster["title"],
                "count_estimate": len(hits),
                "evidence": hits[:4],
                "future_risk": cluster["future_risk"],
                "first_action": cluster["first_action"],
                "questions": list(cluster.get("questions") or ()),
            }
        )
    out.sort(key=lambda x: int(x.get("count_estimate") or 0), reverse=True)
    return out


def detect_button_trends(
    events: list[report_pulse.EventRow],
) -> list[dict[str, Any]]:
    """≥3 одинаковых кнопки «что мешало» за окно — тоже триггер."""
    c: Counter[str] = Counter()
    for e in events:
        if e.event_type == report_pulse.EVENT_PROBLEM and e.problem_code:
            code = str(e.problem_code)
            if code == "ok":
                continue
            c[code] += 1
    out: list[dict[str, Any]] = []
    for code, n in c.most_common(5):
        if n < TREND_MIN_BUTTONS:
            continue
        meta = BUTTON_TREND_META.get(code) or {
            "title": PROBLEM_RU.get(code, code),
            "future_risk": "тема закрепится и начнёт бить по сервису и команде",
            "first_action": "сегодня разберите тему на короткой планёрке без поиска виноватых",
        }
        # Подтянуть цитаты с тем же problem_code, если есть
        evidence = []
        for e in events:
            if e.event_type != report_pulse.EVENT_COMMENT:
                continue
            if (e.problem_code or "") == code and (e.comment_text or "").strip():
                evidence.append(re.sub(r"\s+", " ", e.comment_text.strip())[:180])
            if len(evidence) >= 4:
                break
        if not evidence:
            evidence = extract_comments(events)[:2]
        out.append(
            {
                "theme_code": code,
                "title": meta["title"],
                "count_estimate": n,
                "evidence": evidence,
                "future_risk": meta["future_risk"],
                "first_action": meta["first_action"],
            }
        )
    return out


def _both_sides_for(code: str | None) -> str:
    if not code:
        return (
            "Вы учитесь слышать систему, а не только людей; "
            "линия учится давать сигнал раньше, чем сломается сервис."
        )
    for cl in KEYWORD_CLUSTERS:
        if cl["theme_code"] == code and cl.get("both_sides"):
            return str(cl["both_sides"])
    meta = BUTTON_TREND_META.get(code) or {}
    if meta.get("both_sides"):
        return str(meta["both_sides"])
    return (
        "Вы растёте как руководитель через ясность; "
        "команда растёт через право говорить о проблеме без наказания."
    )


def template_mentor_advice(
    alert: ma.ManagerAlert, *, restaurant_title: str
) -> str:
    theme = alert.title or "повторяющаяся тема"
    body = re.sub(r"<[^>]+>", "", " ".join(alert.body_lines)).strip()
    quotes = "\n".join(f"«{c}»" for c in (alert.comments or [])[:3])
    code = alert.code or ""
    cluster_q = ()
    for cl in KEYWORD_CLUSTERS:
        if cl["theme_code"] == code:
            cluster_q = cl.get("questions") or ()
            break
    if not cluster_q:
        cluster_q = (
            "Как ты себя чувствуешь после таких смен?",
            "Что, по-твоему, больше всего мешает?",
            "Есть ли что-то, о чём сложно сказать на планёрке?",
            "Что помогло бы тебе работать легче уже завтра?",
        )
    both = _both_sides_for(code)
    text = (
        f"Дорогой управляющий,\n\n"
        f"На точке «{restaurant_title}» появилась тенденция: {theme}. {body}\n\n"
    )
    if quotes:
        text += f"Что пишут на смене (анонимно):\n{quotes}\n\n"
    text += (
        f"Если не отреагировать, обе стороны проиграют: линия устанет молчать, "
        f"а вам придётся тушить последствия вместо роста.\n\n"
        f"Рост обеих сторон: {both}\n\n"
        f"Что сделать сегодня (конкретно):\n"
        f"1) {alert.recommendation}\n"
        f"2) На планёрке без имён: «линия сигналит про это — давайте найдём дыру в системе».\n"
        f"3) Через 3–5 дней проверьте: стало ли меньше тех же кнопок/фраз в опросе.\n\n"
        f"Если будете говорить тет-а-тет, спросите так, чтобы человек сам покопался "
        f"и вы оба выросли из разговора:\n"
        f"«{cluster_q[0]}»\n"
        f"«{cluster_q[1]}»\n"
        f"«{cluster_q[2]}»\n"
        f"«{cluster_q[3]}»\n\n"
        f"Хочу, чтобы вы росли вместе с командой — через ясность и уважение, не через крик."
    )
    return append_growth_footer(text, theme_code=alert.code)


async def build_advice(
    alert: ma.ManagerAlert,
    *,
    restaurant_title: str,
    extra_context: str | None = None,
    events: list[report_pulse.EventRow] | None = None,
) -> str | None:
    client = _client_or_none()
    if client is None:
        return template_mentor_advice(alert, restaurant_title=restaurant_title)
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
            return template_mentor_advice(alert, restaurant_title=restaurant_title)
        return append_growth_footer(text, theme_code=alert.code)
    except Exception as e:
        print(f"[ai-advisor] OpenAI error: {e}")
        return template_mentor_advice(alert, restaurant_title=restaurant_title)


async def detect_comment_trends(
    events: list[report_pulse.EventRow],
) -> list[dict[str, Any]]:
    """Кнопки + ключевые слова (+ LLM если есть ключ)."""
    button = detect_button_trends(events)
    keyword = detect_keyword_trends(events)
    base = button + keyword
    # дедуп по theme_code, берём больший count
    by_code: dict[str, dict[str, Any]] = {}
    for t in base:
        code = str(t.get("theme_code") or "comment_trend")
        prev = by_code.get(code)
        if not prev or int(t.get("count_estimate") or 0) > int(
            prev.get("count_estimate") or 0
        ):
            by_code[code] = t
    merged = list(by_code.values())
    merged.sort(key=lambda x: int(x.get("count_estimate") or 0), reverse=True)

    comments = extract_comments(events, limit=MAX_COMMENTS_IN_PROMPT)
    client = _client_or_none()
    if client is None or len(comments) < TREND_MIN_COMMENTS:
        return merged

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
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if not isinstance(data, list):
            return merged
        seen = {str(t.get("theme_code") or "") for t in merged}
        for item in data[:3]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            code = str(item.get("theme_code") or "comment_trend").strip() or "comment_trend"
            evidence = item.get("evidence") or []
            if not isinstance(evidence, list):
                evidence = []
            if code in seen:
                continue
            seen.add(code)
            merged.append(
                {
                    "theme_code": code,
                    "title": title[:120],
                    "count_estimate": int(
                        item.get("count_estimate") or len(evidence) or 0
                    ),
                    "evidence": [str(x)[:180] for x in evidence[:4] if str(x).strip()],
                    "future_risk": str(item.get("future_risk") or "").strip()[:300],
                    "first_action": str(item.get("first_action") or "").strip()[:300],
                }
            )
        merged.sort(key=lambda x: int(x.get("count_estimate") or 0), reverse=True)
        return merged
    except Exception as e:
        print(f"[ai-trends] {e}")
        return merged


def trend_to_alert(trend: dict[str, Any]) -> ma.ManagerAlert:
    code = str(trend.get("theme_code") or "comment_trend")
    title = str(trend.get("title") or "Повтор в отзывах")
    body = [
        f"В отзывах линии повторяется тема: <b>{escape(title)}</b>.",
    ]
    n = int(trend.get("count_estimate") or 0)
    if n:
        body.append(f"Похожих сигналов: <b>{n}</b>.")
    risk = str(trend.get("future_risk") or "").strip()
    if risk:
        body.append(f"Если не трогать: {escape(risk)}")
    action = str(trend.get("first_action") or "").strip()
    rec = action or ma.RECOMMENDATIONS.get(code, ma.RECOMMENDATIONS["rating_drop"])
    evidence = trend.get("evidence") or []
    return ma.ManagerAlert(
        kind="comment_trend",
        code=code if code in PROBLEM_RU else "comment_trend",
        title=f"Тенденция: {title}",
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
        comments = extract_comments(cur_events)
        if len(comments) < 2:
            return None
        alert = ma.ManagerAlert(
            kind="comment_trend",
            code="comment_trend",
            title="Разбор голоса смены",
            body_lines=[
                "Жёсткого порога нет, но есть свободные отзывы — разберите повторы.",
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
        esc = escape(ln)
        esc = re.sub(r"(https://[^\s<]+)", r'<a href="\1">\1</a>', esc)
        out.append(esc)
    return "\n".join(out)


async def transcribe_voice(
    file_bytes: bytes, *, filename: str = "voice.ogg"
) -> str | None:
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
