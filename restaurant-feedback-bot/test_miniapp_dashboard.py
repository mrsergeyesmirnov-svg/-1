"""Unit tests for miniapp dashboard buckets (no asyncpg/aiohttp)."""
from __future__ import annotations

from datetime import datetime, timezone

import chef_survey
import miniapp_dashboard as md
import report_pulse


def test_dept_bucket_splits_floor_kitchen():
    now = datetime.now(timezone.utc)
    events = [
        report_pulse.EventRow(
            created_at=now,
            event_type=report_pulse.EVENT_RATING,
            rating=5,
            problem_code=None,
            comment_text=None,
            restaurant_chat_id=-1,
            restaurant_label="Hall",
            department=chef_survey.DEPARTMENT_FLOOR,
        ),
        report_pulse.EventRow(
            created_at=now,
            event_type=report_pulse.EVENT_PROBLEM,
            rating=None,
            problem_code="kitchen",
            comment_text=None,
            restaurant_chat_id=-1,
            restaurant_label="Hall",
            department=chef_survey.DEPARTMENT_FLOOR,
        ),
        report_pulse.EventRow(
            created_at=now,
            event_type=report_pulse.EVENT_RATING,
            rating=3,
            problem_code=None,
            comment_text=None,
            restaurant_chat_id=-2,
            restaurant_label="Kitchen",
            department=chef_survey.DEPARTMENT_KITCHEN,
        ),
        report_pulse.EventRow(
            created_at=now,
            event_type=report_pulse.EVENT_COMMENT,
            rating=None,
            problem_code=None,
            comment_text="Долго ждали отдачу",
            restaurant_chat_id=-2,
            restaurant_label="Kitchen",
            department=chef_survey.DEPARTMENT_KITCHEN,
        ),
    ]
    floor = md._dept_bucket(events, chef_survey.DEPARTMENT_FLOOR)
    kitchen = md._dept_bucket(events, chef_survey.DEPARTMENT_KITCHEN)
    assert floor["ratings_count"] == 1
    assert floor["avg_rating"] == 5
    assert floor["top_blockers"][0]["code"] == "kitchen"
    assert kitchen["ratings_count"] == 1
    assert kitchen["comments_count"] == 1
    assert kitchen["comments"][0]["text"].startswith("Долго")


def test_ai_teaser_mentions_hot():
    floor = {"top_blockers": [{"code": "team", "label": "Команда", "count": 3}]}
    kitchen = {"top_blockers": []}
    tip = md._ai_teaser(floor, kitchen, 2)
    assert tip["tips"]
    assert "горящ" in tip["tips"][0].lower()
