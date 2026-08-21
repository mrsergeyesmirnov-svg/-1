"""AI mentor: trends, learn-more button, no spiral footer in advice."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import ai_advisor as a
import manager_alerts as ma
import report_pulse as rp
import shift_survey


def test_strip_links_and_no_spiral_in_advice_body():
    raw = (
        "Дорогой управляющий, тест.\n\n"
        "Подробнее изучить тему «Спиральная динамика» можно здесь: "
        "https://ru.wikipedia.org/wiki/x\n"
        "Если хотите расти как управляющий — перейдите и почитайте."
    )
    clean = a.strip_links_from_advice(raw)
    assert "https://" not in clean
    assert "Спиральная динамика" not in clean
    assert "Дорогой управляющий" in clean


def test_fallback_learn_more_varies_by_theme():
    kitchen = a.fallback_learn_more("kitchen")
    assert kitchen.title
    assert kitchen.reference
    assert "Спиральная" not in kitchen.title  # кухня ≠ spiral по умолчанию
    self_lm = a.fallback_learn_more("self")
    assert self_lm.kind in ("book", "article", "wiki")


def test_learn_more_store_and_format():
    learn = a.LearnMore(
        kind="book",
        title="Цель",
        blurb="Про узкие места на отдаче.",
        reference="Элияху Голдратт — «Цель»",
    )
    token = a.store_learn_more(learn)
    got = a.get_learn_more(token)
    assert got is not None
    assert got.title == "Цель"
    html = a.format_learn_more_html(learn)
    assert "Книга" in html
    assert "Цель" in html
    kb = a.learn_more_teaser_keyboard(token)
    assert kb.inline_keyboard[0][0].callback_data == f"ai:lm:{token}"


def test_trend_to_alert():
    alert = a.trend_to_alert(
        {
            "theme_code": "kitchen",
            "title": "Отдача тормозит на пике",
            "count_estimate": 4,
            "evidence": ["долго ждали блюда", "кухня не успевала"],
            "future_risk": "гости начнут уходить, команда выгорит",
            "first_action": "разобрать пик с шефом сегодня",
        }
    )
    assert isinstance(alert, ma.ManagerAlert)
    assert alert.kind == "comment_trend"
    assert "Отдача" in alert.title
    assert len(alert.comments) == 2


def test_extract_comments():
    now = datetime.now(timezone.utc)
    events = [
        rp.EventRow(now, rp.EVENT_COMMENT, None, None, "На кухне снова завал на пике", 1, "x"),
        rp.EventRow(now, rp.EVENT_COMMENT, None, None, "Кухня не успевает отдавать", 1, "x"),
        rp.EventRow(now, rp.EVENT_RATING, 2, None, None, 1, "x"),
    ]
    comments = a.extract_comments(events)
    assert len(comments) == 2


def _comments(texts: list[str]) -> list[rp.EventRow]:
    now = datetime.now(timezone.utc)
    return [
        rp.EventRow(now, rp.EVENT_COMMENT, None, None, t, 1, "x") for t in texts
    ]


def test_keyword_trends_all_themes():
    cases = {
        "kitchen": [
            "Кухня опять не успевает на отдаче",
            "Долго ждали горячее с кухни",
            "На пике завал на раздаче снова",
        ],
        "guests": [
            "Гость устроил скандал и претензию",
            "Сложный клиент забрал все силы",
            "Жалоба от гостя по сервису",
        ],
        "processes": [
            "Полный бардак в процессах открытия",
            "Никто не сказал зоны и роли",
            "Хаос в организации закрытия",
        ],
        "self": [
            "Нет сил, выгорание после смен",
            "Устала морально, состояние тяжёлое",
            "Не вывожу, сил нет уже третий день",
        ],
        "team": [
            "Коллега орёт и унижает на смене",
            "Травля в команде продолжается",
            "Давление и конфликт со сменщиком",
        ],
    }
    for theme, texts in cases.items():
        trends = a.detect_keyword_trends(_comments(texts))
        codes = {t["theme_code"] for t in trends}
        assert theme in codes, f"expected {theme} in {codes}"


def test_button_trends_without_openai():
    now = datetime.now(timezone.utc)
    events = [
        rp.EventRow(now, rp.EVENT_PROBLEM, None, "kitchen", None, 1, "x"),
        rp.EventRow(now, rp.EVENT_PROBLEM, None, "kitchen", None, 1, "x"),
        rp.EventRow(now, rp.EVENT_PROBLEM, None, "kitchen", None, 1, "x"),
        rp.EventRow(now, rp.EVENT_PROBLEM, None, "guests", None, 1, "x"),
    ]
    trends = a.detect_button_trends(events)
    assert trends
    assert trends[0]["theme_code"] == "kitchen"
    assert trends[0]["count_estimate"] >= 3


def test_template_advice_no_footer_links():
    alert = a.trend_to_alert(
        {
            "theme_code": "guests",
            "title": "Гости давят",
            "count_estimate": 3,
            "evidence": ["сложный гость", "скандал"],
            "future_risk": "выгорание",
            "first_action": "сценарий подхвата",
        }
    )
    text = a.template_mentor_advice(alert, restaurant_title="Тест")
    assert "Рост обеих сторон" in text
    assert "https://" not in text
    assert "Спиральная" not in text


def test_build_advice_requires_openai():
    alert = ma.ManagerAlert(
        kind="comment_trend",
        code="processes",
        title="Тенденция: процессы",
        body_lines=["Повтор по процессам."],
        recommendation=ma.RECOMMENDATIONS["processes"],
        comments=["бардак на открытии"],
        priority=1,
        problem_key="processes",
    )
    text = asyncio.get_event_loop().run_until_complete(
        a.build_advice(alert, restaurant_title="Точка А")
    )
    assert text is None


def test_format_advice_pack():
    pack = a.AdvicePack(text="Дорогой управляющий, привет.", learn=None)
    html = a.format_advice_html(pack)
    assert "AI-наставник" in html
    assert "https://" not in html


def test_charged_blocker_keyboard_no_ok():
    kb = shift_survey.blocker_keyboard(positive=True)
    texts = []
    for row in kb.inline_keyboard:
        for btn in row:
            texts.append(btn.text)
            assert btn.callback_data != "blocker_ok"
    assert "✨ Нигде — всё прошло хорошо" not in texts
    assert shift_survey.blocker_prompt(positive=True) == "Что сделало смену такой?"
    normal = shift_survey.blocker_keyboard(positive=False)
    ok_present = any(
        btn.callback_data == "blocker_ok"
        for row in normal.inline_keyboard
        for btn in row
    )
    assert ok_present
