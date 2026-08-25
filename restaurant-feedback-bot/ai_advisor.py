"""
AI-наставник управляющего.

Читает анонимные комментарии, кнопки «что мешало» и оценки.
Ищет повторы по всем темам: команда, кухня, гости, процессы, состояние.
Пишет в личку управу: тенденция → к чему ведёт → что сделать → вопросы тет-а-тет
→ ссылка «почитать, если хотите расти».

Работает с OpenAI: читает комментарии и пишет живой совет.
Кнопки/ключевые слова только помогают вовремя заметить повтор.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections import Counter
from dataclasses import dataclass
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

# Каталог материалов на случай сбоя LLM: по теме несколько вариантов (книга / статья / wiki).
# Спиральная динамика — только один из вариантов для командных ценностей, не дефолт.
LEARN_CATALOG: dict[str, list[dict[str, str]]] = {
    "team": [
        {
            "kind": "book",
            "title": "Пять пороков команды",
            "blurb": "Помогает разобрать недоверие и конфликты в смене без поиска «виноватых».",
            "reference": "Патрик Ленсиони — «Пять пороков команды»",
        },
        {
            "kind": "book",
            "title": "Психологическая безопасность",
            "blurb": "Как сделать так, чтобы линия говорила о проблемах без страха.",
            "reference": "Эми Эдмондсон — «The Fearless Organization» (психологическая безопасность)",
        },
        {
            "kind": "wiki",
            "title": "Спиральная динамика",
            "blurb": "Имеет смысл, если в команде сталкиваются разные ценности и «уровни» ожиданий.",
            "reference": "https://ru.wikipedia.org/wiki/%D0%A1%D0%BF%D0%B8%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F_%D0%B4%D0%B8%D0%BD%D0%B0%D0%BC%D0%B8%D0%BA%D0%B0",
        },
        {
            "kind": "article",
            "title": "Ненасильственное общение",
            "blurb": "Практичный язык для разговоров тет-а-тет после напряжения на смене.",
            "reference": "https://ru.wikipedia.org/wiki/%D0%9D%D0%B5%D0%BD%D0%B0%D1%81%D0%B8%D0%BB%D1%8C%D1%81%D1%82%D0%B2%D0%B5%D0%BD%D0%BD%D0%BE%D0%B5_%D0%BE%D0%B1%D1%89%D0%B5%D0%BD%D0%B8%D0%B5",
        },
    ],
    "conflict": [
        {
            "kind": "book",
            "title": "Трудные разговоры",
            "blurb": "Как разобрать конфликт зал ↔ кухня без публичного суда.",
            "reference": "Дуглас Стоун, Брюс Паттон, Шейла Хин — «Трудные разговоры»",
        },
        {
            "kind": "wiki",
            "title": "Спиральная динамика",
            "blurb": "Когда конфликт — про разные ценности, а не только про «характер».",
            "reference": "https://ru.wikipedia.org/wiki/%D0%A1%D0%BF%D0%B8%D1%80%D0%B0%D0%BB%D1%8C%D0%BD%D0%B0%D1%8F_%D0%B4%D0%B8%D0%BD%D0%B0%D0%BC%D0%B8%D0%BA%D0%B0",
        },
    ],
    "kitchen": [
        {
            "kind": "book",
            "title": "Цель",
            "blurb": "Про узкие места и поток — полезно, когда отдача срывается на пике.",
            "reference": "Элияху Голдратт — «Цель» (теория ограничений)",
        },
        {
            "kind": "wiki",
            "title": "Системное мышление",
            "blurb": "Смотреть на тайминг и роли, а не на «медленных поваров».",
            "reference": "https://ru.wikipedia.org/wiki/%D0%A1%D0%B8%D1%81%D1%82%D0%B5%D0%BC%D0%BD%D0%BE%D0%B5_%D0%BC%D1%8B%D1%88%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5",
        },
        {
            "kind": "article",
            "title": "Бережливое производство",
            "blurb": "Убрать потери на раздаче и лишние движения на пике.",
            "reference": "https://ru.wikipedia.org/wiki/%D0%91%D0%B5%D1%80%D0%B5%D0%B6%D0%BB%D0%B8%D0%B2%D0%BE%D0%B5_%D0%BF%D1%80%D0%BE%D0%B8%D0%B7%D0%B2%D0%BE%D0%B4%D1%81%D1%82%D0%B2%D0%BE",
        },
    ],
    "processes": [
        {
            "kind": "book",
            "title": "Чеклист",
            "blurb": "Простые чек-листы открытия/закрытия, когда роли размыты.",
            "reference": "Атул Гаванде — «Чеклист. Как избежать глупых ошибок»",
        },
        {
            "kind": "wiki",
            "title": "Системное мышление",
            "blurb": "Чинить процесс, а не людей, когда на смене хаос.",
            "reference": "https://ru.wikipedia.org/wiki/%D0%A1%D0%B8%D1%81%D1%82%D0%B5%D0%BC%D0%BD%D0%BE%D0%B5_%D0%BC%D1%8B%D1%88%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5",
        },
    ],
    "management": [
        {
            "kind": "book",
            "title": "Чеклист",
            "blurb": "Структура смены и зоны ответственности до пика.",
            "reference": "Атул Гаванде — «Чеклист. Как избежать глупых ошибок»",
        },
        {
            "kind": "book",
            "title": "Высокоэффективный менеджмент",
            "blurb": "Как держать ясность и ритм точки без микроменеджмента.",
            "reference": "Эндрю Гроув — «Высокоэффективный менеджмент»",
        },
    ],
    "self": [
        {
            "kind": "book",
            "title": "Выгорание",
            "blurb": "Понять механизмы выгорания линии и что реально помогает восстановиться.",
            "reference": "Эмили Нагоски, Амелия Нагоски — «Выгорание»",
        },
        {
            "kind": "article",
            "title": "Профессиональный стресс (WHO)",
            "blurb": "Коротко и по делу: что считается выгоранием и зачем вмешиваться рано.",
            "reference": "https://www.who.int/news-room/questions-and-answers/item/mental-health-occupational-stress",
        },
    ],
    "stress": [
        {
            "kind": "book",
            "title": "Выгорание",
            "blurb": "Когда плотность смен бьёт по людям — как не доводить до ухода.",
            "reference": "Эмили Нагоски, Амелия Нагоски — «Выгорание»",
        },
        {
            "kind": "article",
            "title": "Профессиональный стресс (WHO)",
            "blurb": "Сверить нагрузку с признаками перегруза на смене.",
            "reference": "https://www.who.int/news-room/questions-and-answers/item/mental-health-occupational-stress",
        },
    ],
    "guests": [
        {
            "kind": "book",
            "title": "Сервис, который запоминают",
            "blurb": "Как ставить рамки сложного гостя и не оставлять официанта один на один.",
            "reference": "Уилл Гидс, Майкл Колвелл — «Unreasonable Hospitality» (неразумное гостеприимство)",
        },
        {
            "kind": "wiki",
            "title": "Управление опытом клиента",
            "blurb": "Ожидания гостя vs то, что линия реально может выдержать на пике.",
            "reference": "https://ru.wikipedia.org/wiki/%D0%A3%D0%BF%D1%80%D0%B0%D0%B2%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5_%D0%BE%D0%BF%D1%8B%D1%82%D0%BE%D0%BC_%D0%BA%D0%BB%D0%B8%D0%B5%D0%BD%D1%82%D0%B0",
        },
    ],
    "staff": [
        {
            "kind": "book",
            "title": "Пять пороков команды",
            "blurb": "Когда нехватка людей ломает доверие и ответственность на смене.",
            "reference": "Патрик Ленсиони — «Пять пороков команды»",
        },
    ],
    "rating_drop": [
        {
            "kind": "book",
            "title": "Цель",
            "blurb": "Найти узкое место, которое тянет вниз оценки смены.",
            "reference": "Элияху Голдратт — «Цель»",
        },
        {
            "kind": "wiki",
            "title": "Системное мышление",
            "blurb": "Связать падение оценок с процессом, а не с «плохими людьми».",
            "reference": "https://ru.wikipedia.org/wiki/%D0%A1%D0%B8%D1%81%D1%82%D0%B5%D0%BC%D0%BD%D0%BE%D0%B5_%D0%BC%D1%8B%D1%88%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5",
        },
    ],
    "comment_trend": [
        {
            "kind": "book",
            "title": "Пять пороков команды",
            "blurb": "Общий разбор повторов в голосе линии — через доверие и ясность.",
            "reference": "Патрик Ленсиони — «Пять пороков команды»",
        },
        {
            "kind": "wiki",
            "title": "Системное мышление",
            "blurb": "Увидеть повторяющуюся дыру в системе смены.",
            "reference": "https://ru.wikipedia.org/wiki/%D0%A1%D0%B8%D1%81%D1%82%D0%B5%D0%BC%D0%BD%D0%BE%D0%B5_%D0%BC%D1%8B%D1%88%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5",
        },
    ],
}

SYSTEM_PROMPT = """\
Ты — ментальный наставник управляющего ресторана. Цель — рост управляющего\
 и линии вместе. НЕ «накричать на смену».

Данные анонимны. Опирайся ТОЛЬКО на отзывы и отметки по ЗАДАННОЙ теме.\
 Не уходи в другие темы (если тема «команда» — не пиши про кухню и наоборот).

Верни ТОЛЬКО JSON без markdown:
{
  "signal": "1-2 предложения: что повторяется в отзывах по ЭТОЙ теме",
  "quotes": ["короткая цитата1", "цитата2"],
  "risk": "к чему приведёт за 2-4 недели, если не трогать",
  "tone": "как говорить с людьми: тон, поза, чего НЕ делать (запрет крика/публичного суда)",
  "actions": ["действие сегодня", "действие на планёрке", "проверка через 3-5 дней"],
  "questions": ["вопрос1", "вопрос2", "вопрос3"],
  "cta": "одна фраза-призыв: что сделать в ближайшие 24 часа",
  "close": "короткая поддержка роста обеих сторон"
}

Правила тет-а-тет (критично):
- Вопросы — НЕ допрос и НЕ повод «вызвать и наорать».
- В tone явно напиши: сначала безопасность и слушание, потом вопросы;\
 запрещено повышать голос, публично разбирать людей, искать виноватого.
- Цель разговора: человек сам покопался, вы оба выросли; не «задать вопросы и надавить».

actions — конкретные шаги, не «поговорите». quotes — только из данных, не выдумывай.\
 Без URL и названий книг. Русский язык.\
"""

LEARN_MORE_PROMPT = """\
Ты подбираешь ОДИН материал для роста управляющего ресторана под КОНКРЕТНУЮ проблему.

Верни ТОЛЬКО JSON без markdown:
{"kind":"book|article|wiki","title":"...","blurb":"1-2 предложения: почему именно это к этой ситуации","reference":"..."}

Правила выбора:
- kind=book: reference = «Автор — „Название“» (реальная известная книга). Без URL.
- kind=article или wiki: reference = полный https:// URL на реальный источник.
- Не повторяй одно и то же каждый раз. Варьируй книги и статьи.
- Спиральная динамика — ТОЛЬКО если проблема явно про ценности/уровни зрелости команды.\
 Не ставь её по умолчанию на буллинг, кухню, гостей или выгорание.
- Кухня/отдача → системное мышление, теория ограничений, lean.
- Процессы → чек-листы, системное мышление, операционный менеджмент.
- Выгорание/состояние → книги и статьи про burnout / occupational stress.
- Гости → hospitality / опыт гостя.
- Команда/давление → психологическая безопасность, трудные разговоры, командная динамика.
- Не выдумывай несуществующие книги и битые ссылки.
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


@dataclass
class LearnMore:
    kind: str  # book | article | wiki
    title: str
    blurb: str
    reference: str  # book citation or https URL


@dataclass
class AdvicePack:
    text: str
    learn: LearnMore | None = None
    theme_code: str | None = None


# token -> LearnMore payload (for «Подробнее» button)
_LEARN_STORE: dict[str, dict[str, Any]] = {}
_LEARN_STORE_MAX = 300


def store_learn_more(learn: LearnMore) -> str:
    token = secrets.token_urlsafe(8)[:10]
    _LEARN_STORE[token] = {
        "kind": learn.kind,
        "title": learn.title,
        "blurb": learn.blurb,
        "reference": learn.reference,
        "ts": time.time(),
    }
    if len(_LEARN_STORE) > _LEARN_STORE_MAX:
        oldest = sorted(_LEARN_STORE.items(), key=lambda x: x[1].get("ts") or 0)
        for k, _ in oldest[: len(_LEARN_STORE) - _LEARN_STORE_MAX]:
            _LEARN_STORE.pop(k, None)
    return token


def get_learn_more(token: str) -> LearnMore | None:
    raw = _LEARN_STORE.get(token)
    if not raw:
        return None
    return LearnMore(
        kind=str(raw.get("kind") or "article"),
        title=str(raw.get("title") or "Материал"),
        blurb=str(raw.get("blurb") or ""),
        reference=str(raw.get("reference") or ""),
    )


def strip_links_from_advice(text: str) -> str:
    """Убрать URL и хвосты «Подробнее изучить…» из тела совета."""
    t = re.sub(r"https?://\S+", "", text)
    t = re.sub(
        r"(?is)\n*Подробнее изучить тему.*?(?:\n|$)",
        "\n",
        t,
    )
    t = re.sub(
        r"(?is)\n*Если хотите расти как управляющий.*?(?:\n|$)",
        "\n",
        t,
    )
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def fallback_learn_more(theme_code: str | None) -> LearnMore:
    code = theme_code if theme_code in LEARN_CATALOG else "comment_trend"
    options = LEARN_CATALOG.get(code) or LEARN_CATALOG["comment_trend"]
    # ротация по часу — не всегда один и тот же материал
    idx = int(time.time() // 3600) % len(options)
    item = options[idx]
    return LearnMore(
        kind=item["kind"],
        title=item["title"],
        blurb=item["blurb"],
        reference=item["reference"],
    )


def learn_more_teaser_keyboard(token: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подробнее",
                    callback_data=f"ai:lm:{token}",
                )
            ]
        ]
    )


def format_learn_more_html(learn: LearnMore) -> str:
    kind_ru = {
        "book": "Книга",
        "article": "Статья",
        "wiki": "Справка",
    }.get(learn.kind, "Материал")
    lines = [
        f"📚 <b>{escape(kind_ru)}: {escape(learn.title)}</b>",
        "",
    ]
    if learn.blurb:
        lines.append(escape(learn.blurb))
        lines.append("")
    ref = learn.reference.strip()
    if ref.startswith("http://") or ref.startswith("https://"):
        lines.append(f'<a href="{escape(ref, quote=True)}">{escape(ref)}</a>')
    else:
        lines.append(escape(ref))
    lines.append("")
    lines.append(
        "<i>Это не обязательное чтение — для тех, кто хочет глубже разобрать "
        "именно эту проблему и вырасти в ней.</i>"
    )
    return "\n".join(lines)


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


def theme_keys(theme_code: str | None) -> tuple[str, ...]:
    if not theme_code:
        return ()
    for cl in KEYWORD_CLUSTERS:
        if cl["theme_code"] == theme_code:
            return tuple(cl.get("keys") or ())
    # близкие коды
    aliases = {
        "staff": "team",
        "conflict": "team",
        "management": "processes",
        "stress": "self",
    }
    mapped = aliases.get(theme_code)
    if mapped:
        return theme_keys(mapped)
    return ()


def comment_matches_theme(text: str, theme_code: str | None, *, problem_key: str | None = None) -> bool:
    if not text:
        return False
    low = text.casefold()
    if problem_key and problem_key.casefold() in low:
        return True
    keys = theme_keys(theme_code)
    if not keys:
        return False
    return any(k in low for k in keys)


def filter_events_for_theme(
    events: list[report_pulse.EventRow],
    theme_code: str | None,
    *,
    problem_key: str | None = None,
) -> list[report_pulse.EventRow]:
    """Оставить только события выбранной темы — чтобы совет не «уплыл» на кухню."""
    if not theme_code and not problem_key:
        return list(events)
    codes = {c for c in (theme_code, problem_key) if c}
    # синонимы кнопок
    if theme_code == "team":
        codes |= {"staff", "conflict"}
    elif theme_code == "processes":
        codes |= {"management"}
    elif theme_code == "self":
        codes |= {"stress"}
    out: list[report_pulse.EventRow] = []
    for e in events:
        if e.event_type == report_pulse.EVENT_PROBLEM and (e.problem_code or "") in codes:
            out.append(e)
            continue
        if e.event_type == report_pulse.EVENT_COMMENT:
            linked = (e.problem_code or "").strip()
            if linked and linked in codes:
                out.append(e)
                continue
            if comment_matches_theme(
                e.comment_text or "", theme_code, problem_key=problem_key
            ):
                out.append(e)
    return out


def extract_comments_for_theme(
    events: list[report_pulse.EventRow],
    theme_code: str | None,
    *,
    problem_key: str | None = None,
    limit: int = MAX_COMMENTS_IN_PROMPT,
) -> list[str]:
    scoped = filter_events_for_theme(events, theme_code, problem_key=problem_key)
    return extract_comments(scoped, limit=limit)


def theme_label(code: str | None) -> str:
    if not code:
        return "общая тема"
    return PROBLEM_RU.get(code, code)


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


async def pick_learn_more(
    *,
    theme_code: str | None,
    restaurant_title: str,
    alert: ma.ManagerAlert,
    advice_text: str,
    comments: list[str],
) -> LearnMore:
    """OpenAI подбирает книгу/статью/wiki под ситуацию; иначе — ротация из каталога."""
    client = _client_or_none()
    fallback = fallback_learn_more(theme_code)
    if client is None:
        return fallback
    payload = {
        "theme_code": theme_code or "comment_trend",
        "restaurant": restaurant_title,
        "alert_title": alert.title,
        "alert_body": re.sub(r"<[^>]+>", "", " ".join(alert.body_lines))[:400],
        "advice_excerpt": advice_text[:500],
        "comments": comments[:6],
    }
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=350,
            temperature=0.7,
            messages=[
                {"role": "system", "content": LEARN_MORE_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return fallback
        kind = str(data.get("kind") or "").strip().lower()
        if kind not in ("book", "article", "wiki"):
            return fallback
        title = str(data.get("title") or "").strip()[:120]
        blurb = str(data.get("blurb") or "").strip()[:400]
        reference = str(data.get("reference") or "").strip()[:400]
        if not title or not reference:
            return fallback
        if kind in ("article", "wiki") and not reference.startswith("http"):
            return fallback
        if kind == "book" and reference.startswith("http"):
            # книга без URL — если модель дала ссылку, оставим как article
            kind = "article"
        return LearnMore(kind=kind, title=title, blurb=blurb, reference=reference)
    except Exception as e:
        print(f"[ai-learn-more] {e}")
        return fallback


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


def parse_advice_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def render_advice_report(
    data: dict[str, Any],
    *,
    restaurant_title: str,
    theme_code: str | None,
    problem_title: str | None = None,
) -> str:
    """Telegram HTML-отчёт по отзывам: блоки, эмодзи, CTA."""
    theme = problem_title or theme_label(theme_code)
    signal = str(data.get("signal") or "").strip()
    risk = str(data.get("risk") or "").strip()
    tone = str(data.get("tone") or "").strip()
    cta = str(data.get("cta") or "").strip()
    close = str(data.get("close") or "").strip()
    quotes = data.get("quotes") or []
    actions = data.get("actions") or []
    questions = data.get("questions") or []
    if not isinstance(quotes, list):
        quotes = []
    if not isinstance(actions, list):
        actions = []
    if not isinstance(questions, list):
        questions = []

    lines = [
        "🤖 <b>Отчёт наставника</b>",
        f"📍 {escape(restaurant_title)} · тема: <b>{escape(theme)}</b>",
        "",
    ]
    if signal:
        lines.append("🔎 <b>Что повторяется</b>")
        lines.append(escape(signal))
        lines.append("")
    if quotes:
        lines.append("💬 <b>Голос смены</b> <i>(анонимно)</i>")
        for q in quotes[:4]:
            q = str(q).strip()
            if q:
                lines.append(f"• «{escape(q)}»")
        lines.append("")
    if risk:
        lines.append("⚠️ <b>Если не трогать</b>")
        lines.append(escape(risk))
        lines.append("")
    if tone:
        lines.append("🧭 <b>Как говорить</b>")
        lines.append(escape(tone))
        lines.append(
            "<i>Тет-а-тет ≠ допрос и ≠ повод наорать. Сначала безопасность, потом вопросы.</i>"
        )
        lines.append("")
    if actions:
        lines.append("✅ <b>Что сделать</b>")
        for i, a in enumerate(actions[:4], 1):
            a = str(a).strip()
            if a:
                lines.append(f"{i}. {escape(a)}")
        lines.append("")
    if questions:
        lines.append("🗣 <b>Вопросы тет-а-тет</b> <i>(спокойно, один на один)</i>")
        for q in questions[:4]:
            q = str(q).strip()
            if q:
                lines.append(f"• «{escape(q)}»")
        lines.append("")
    if cta:
        lines.append("👉 <b>Сделайте в ближайшие 24 часа</b>")
        lines.append(f"<b>{escape(cta)}</b>")
        lines.append("")
    if close:
        lines.append(escape(close))
    return "\n".join(lines).strip()


def template_report_data(
    alert: ma.ManagerAlert, *, restaurant_title: str
) -> dict[str, Any]:
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
    body = re.sub(r"<[^>]+>", "", " ".join(alert.body_lines)).strip()
    return {
        "signal": f"{alert.title or 'Повтор в отзывах'}. {body}"[:400],
        "quotes": list(alert.comments or [])[:3],
        "risk": (
            "Линия устанет молчать, сервис просядет, а вам придётся тушить "
            "последствия вместо роста."
        ),
        "tone": (
            "Один на один, спокойно, без публичного суда и без повышения голоса. "
            "Сначала скажите, что хотите понять систему, а не найти виноватого. "
            "Задать вопросы и наорать — провал: человек закроется, сигнал пропадёт."
        ),
        "actions": [
            alert.recommendation,
            "На планёрке без имён: «линия сигналит про это — найдём дыру в системе».",
            "Через 3–5 дней проверьте: стало ли меньше тех же кнопок/фраз.",
        ],
        "questions": list(cluster_q)[:4],
        "cta": "Сегодня проведите один спокойный тет-а-тет без крика и публичного разбора.",
        "close": _both_sides_for(code),
    }


def template_mentor_advice(
    alert: ma.ManagerAlert, *, restaurant_title: str
) -> str:
    return render_advice_report(
        template_report_data(alert, restaurant_title=restaurant_title),
        restaurant_title=restaurant_title,
        theme_code=alert.code,
        problem_title=alert.title,
    )


async def build_advice(
    alert: ma.ManagerAlert,
    *,
    restaurant_title: str,
    extra_context: str | None = None,
    events: list[report_pulse.EventRow] | None = None,
    lock_theme: bool = True,
) -> AdvicePack | None:
    """Совет через OpenAI + «Подробнее». lock_theme=True — только выбранная тема."""
    client = _client_or_none()
    if client is None:
        print("[ai-advisor] OPENAI_API_KEY missing — mentor advice skipped")
        return None

    theme = alert.code
    pkey = alert.problem_key or theme
    scoped_events = list(events or [])
    if lock_theme and theme and theme not in ("comment_trend", "other", "rating_drop"):
        scoped_events = filter_events_for_theme(
            scoped_events, theme, problem_key=pkey
        )

    comments = extract_comments(scoped_events) if scoped_events else []
    if not comments and alert.comments:
        # только если цитаты уже привязаны к алерту/теме
        comments = list(alert.comments)
    if not comments and not (alert.body_lines or []):
        print("[ai-advisor] no comments/context for theme — skip advice")
        return None

    if scoped_events:
        context = events_to_context(
            scoped_events, title=restaurant_title, alert=alert
        )
    else:
        context = _alert_to_context(alert, title=restaurant_title)
    if extra_context:
        context += f"\n\nДополнительно:\n{extra_context}"
    context += (
        f"\n\nЖЁСТКО: тема совета = «{theme_label(theme)}» "
        f"(code={theme or '-'}). Не пиши про другие темы.\n"
        "Верни JSON-отчёт по схеме. "
        "В tone обязательно запрети крик и публичный суд на тет-а-тет."
    )
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=900,
            temperature=0.55,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = parse_advice_json(raw)
        if parsed:
            text = render_advice_report(
                parsed,
                restaurant_title=restaurant_title,
                theme_code=theme,
                problem_title=alert.title,
            )
        else:
            # fallback: очистить сплошной текст и обернуть минимально
            clean = strip_links_from_advice(raw)
            if not clean:
                print("[ai-advisor] empty OpenAI response")
                return None
            text = (
                f"🤖 <b>Отчёт наставника</b>\n"
                f"📍 {escape(restaurant_title)} · тема: "
                f"<b>{escape(theme_label(theme))}</b>\n\n"
                f"{escape(clean)}"
            )
        learn = await pick_learn_more(
            theme_code=alert.code,
            restaurant_title=restaurant_title,
            alert=alert,
            advice_text=re.sub(r"<[^>]+>", "", text),
            comments=comments,
        )
        return AdvicePack(text=text, learn=learn, theme_code=alert.code)
    except Exception as e:
        print(f"[ai-advisor] OpenAI error: {e}")
        return None


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
) -> AdvicePack | None:
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
) -> tuple[ma.ManagerAlert | None, AdvicePack | None]:
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


def format_advice_html(advice: AdvicePack | str) -> str:
    """Текст уже в HTML (отчёт) — не экранируем повторно."""
    text = advice.text if isinstance(advice, AdvicePack) else advice
    text = (text or "").strip()
    if not text:
        return "🤖 <b>Отчёт наставника</b>"
    # Уже готовый отчёт с разметкой
    if "<b>" in text or "<i>" in text:
        if "Отчёт наставника" not in text and "AI-наставник" not in text:
            return "🤖 <b>Отчёт наставника</b>\n\n" + text
        return text
    # Plain text fallback
    lines = [line for line in text.split("\n") if line.strip()]
    out = ["🤖 <b>Отчёт наставника</b>\n"]
    for ln in lines:
        out.append(escape(ln))
    return "\n".join(out)


def teaser_learn_more_text() -> str:
    return (
        "📚 Чтобы глубже разобрать эту тему и развиваться именно в ней — "
        "нажмите «Подробнее»."
    )


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
