"""AI mentor: trends from comments, growth footer."""
from __future__ import annotations

import ai_advisor as a
import manager_alerts as ma
import report_pulse as rp
from datetime import datetime, timezone


def test_growth_footer_spiral():
    text = a.append_growth_footer("Дорогой управляющий, тест.", theme_code="team")
    assert "Спиральная динамика" in text
    assert "https://" in text
    assert "перейдите и почитайте" in text.lower() or "почитайте" in text


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
