"""
Отчёты для бота стажёров: темы обучения + комментарии (без рейтинга звёздами).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import report_pulse

# Коды кнопок → подписи в отчёте
TRAIN_TOPIC_LABELS: dict[str, str] = {
    "prep": "не объяснили задачи и процессы до смены",
    "demo": "не показали на практике (только «делай сам»)",
    "unclear": "непонятно, что от меня ждут на смене",
    "mentor": "наставник не успевал / не был рядом",
    "overload": "за смену дали слишком много нового сразу",
    "on_the_fly": "учили «на бегу», без времени разобрать",
    "comment": "свой комментарий",
}

EVENT_TOPIC = "topic"
EVENT_COMMENT = report_pulse.EVENT_COMMENT


def _topic_stats(events: list[report_pulse.EventRow]) -> list[tuple[str, int]]:
    cnt: Counter[str] = Counter()
    for e in events:
        if e.event_type in (EVENT_TOPIC, report_pulse.EVENT_PROBLEM) and e.problem_code:
            if e.problem_code != "comment":
                cnt[e.problem_code] += 1
    return cnt.most_common()


def build_report_html_stazher(
    *,
    period: str,
    period_label: str,
    scope_title: str,
    start: datetime,
    end: datetime,
    current: list[report_pulse.EventRow],
) -> str:
    topics = _topic_stats(current)
    comment_limit = 8 if period == report_pulse.PERIOD_SHIFT else 5
    comments = report_pulse._collect_comments(current, period, limit=comment_limit)

    n_topics = sum(n for _, n in topics)
    n_comments = report_pulse._count_text_comments(current)

    date_fmt = "%d.%m.%Y %H:%M"
    lines = [
        "📋 <b>Отчёт по обучению стажёров</b>",
        f"<b>{escape(scope_title)}</b>",
        f"Период: <b>{escape(period_label)}</b>",
        f"{start.strftime(date_fmt)} — {end.strftime(date_fmt)}",
        "",
        f"Отметок по темам: <b>{n_topics}</b>",
        f"Текстовых комментариев: <b>{n_comments}</b>",
        "",
    ]

    if topics:
        lines.append("<b>Чего не хватало на сменах</b> <i>(кнопки)</i>")
        for code, n in topics[:6]:
            label = TRAIN_TOPIC_LABELS.get(code, code)
            lines.append(f"• {escape(label)} — <b>{n}</b>")
        lines.append("")
    else:
        lines.append("<i>Темы не выбирали — только комментарии или пока нет откликов.</i>\n")

    if comments:
        if period == report_pulse.PERIOD_SHIFT:
            lines.append("<b>Последние комментарии</b> <i>(анонимно)</i>")
        else:
            lines.append("<b>Яркие комментарии</b> <i>(анонимно)</i>")
        for text, plab, ts in comments:
            tag = f"<i>{escape(plab)}</i> — " if plab else ""
            when = ""
            if ts is not None and period == report_pulse.PERIOD_SHIFT:
                try:
                    when = ts.strftime("%d.%m %H:%M") + " · "
                except Exception:
                    when = ""
            lines.append(f"💬 {when}{tag}«{escape(text)}»")
        lines.append("")
    else:
        lines.append("<i>Текстовых комментариев за период нет.</i>\n")

    tips: list[str] = []
    for code, n in topics[:3]:
        label = TRAIN_TOPIC_LABELS.get(code, code)
        tips.append(f"Усилить: «{label}» — отмечено {n} раз(а).")
    for text, plab, _ in comments:
        low = text.lower()
        if any(h in low for h in report_pulse.ACTION_HINTS):
            short = text if len(text) <= 120 else text[:117] + "…"
            prefix = f"({plab}) " if plab else ""
            tips.append(f"Разобрать с командой: {prefix}{short}")
        if len(tips) >= 5:
            break
    if not tips and topics:
        code, n = topics[0]
        tips.append(
            f"Приоритет: «{TRAIN_TOPIC_LABELS.get(code, code)}» — самая частая тема ({n})."
        )

    if tips:
        lines.append("<b>На что обратить внимание</b>")
        for t in tips:
            lines.append(f"🔸 {escape(t)}")
    else:
        lines.append("<i>Рекомендации появятся, когда накопится больше откликов.</i>")

    return "\n".join(lines)


async def build_reports_for_manager(
    data: dict[str, Any],
    user_id: int,
    period: str,
    *,
    is_global_admin: bool,
    tz_name: str,
    jsonl_path: Path | None,
    selected_chat: str | None = None,
) -> list[str]:
    full_scope = report_pulse.chat_scope_for_user(
        data, user_id, is_global_admin=is_global_admin
    )
    scope = report_pulse.narrow_scope(full_scope, selected_chat)
    if not scope:
        return [
            "Нет привязанных групп для отчёта. "
            "Обратитесь к администратору или выполните <code>/link_manager</code>."
        ]

    tz = tz_name
    if scope:
        rec = data.get("chats", {}).get(scope[0][0])
        if isinstance(rec, dict) and rec.get("timezone"):
            tz = str(rec["timezone"])
    start, end, _, _, plabel = report_pulse.period_window(period, tz)
    chat_ids = [int(cid) for cid, _ in scope]
    current = await report_pulse.load_events(chat_ids, start, end, jsonl_path=jsonl_path)

    if not current:
        return [
            f"За период «{escape(plabel)}» откликов пока нет.\n\n"
            "Попросите стажёров ответить из группы (кнопка «Ответить в личке»)."
        ]

    multi = len(scope) > 1 and (selected_chat in (None, "all"))
    if not multi:
        cid, title = scope[0]
        body = build_report_html_stazher(
            period=period,
            period_label=plabel,
            scope_title=title,
            start=start,
            end=end,
            current=[
                e
                for e in current
                if e.restaurant_chat_id == int(cid) or e.restaurant_chat_id is None
            ],
        )
        return report_pulse.split_telegram_html(body)

    parts: list[str] = []
    for cid, title in scope:
        cid_i = int(cid)
        cur = [e for e in current if e.restaurant_chat_id == cid_i]
        if not cur:
            continue
        body = build_report_html_stazher(
            period=period,
            period_label=plabel,
            scope_title=title,
            start=start,
            end=end,
            current=cur,
        )
        parts.extend(report_pulse.split_telegram_html(body))
    if not parts:
        return [f"За период «{escape(plabel)}» по вашим группам нет данных."]
    return parts
