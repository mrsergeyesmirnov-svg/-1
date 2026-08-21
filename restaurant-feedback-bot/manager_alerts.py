"""
Push-алерты менеджеру: что сломалось на сменах + яркие комментарии + одно действие.

Не дашборд «когда вспомнят», а сообщение в личку, когда порог пробит.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

import problems_pulse
import report_pulse
import survey_buttons

ALERT_WINDOW_HOURS = 72
# Не слать повторно тот же тип сигнала по той же теме чаще, чем раз в N часов
ALERT_COOLDOWN_HOURS = 12
MAX_ALERTS_PER_CHAT = 2
# Пилот: раньше 3+спайк к прошлому окну — почти никогда не стреляло на живой точке
MIN_PROBLEM_MENTIONS = 2
HOT_ABSOLUTE = 3  # столько отметок за 72ч → пуш даже без роста к прошлому окну
MIN_RATINGS_FOR_DROP = 2
RATING_DROP_THRESHOLD = 0.3
GROWTH_MIN_EXTRA = 2
GROWTH_MIN_PCT = 0.4
IMPROVE_PREV_MIN = 4
IMPROVE_DROP_RATIO = 0.55
# События, после которых проверяем порог «прямо сейчас»
TRIGGER_EVENTS = frozenset(
    {
        report_pulse.EVENT_PROBLEM,
        report_pulse.EVENT_RATING,
        report_pulse.EVENT_COMMENT,
        "chef_shift",
    }
)

THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "kitchen": (
        "кухн",
        "медлен",
        "отдач",
        "раздач",
        "заказ",
        "блюд",
        "готови",
        "ждали",
        "ждать",
        "горяч",
        "суш",
        "пик",
    ),
    "team": (
        "команд",
        "коллег",
        "сменщик",
        "нехват",
        "персонал",
        "людей",
        "официант",
        "штат",
        "кадр",
    ),
    "staff": (
        "нехват",
        "персонал",
        "людей",
        "официант",
        "сменщик",
        "штат",
        "кадр",
    ),
    "processes": (
        "процесс",
        "организац",
        "бардак",
        "хаос",
        "менедж",
        "руковод",
        "планёр",
        "график",
        "зоны",
    ),
    "management": (
        "организац",
        "бардак",
        "хаос",
        "менедж",
        "руковод",
        "планёр",
        "график",
        "зоны",
    ),
    "self": (
        "устал",
        "состоян",
        "настроен",
        "выгоран",
        "сил нет",
        "мораль",
        "себя",
    ),
    "conflict": (
        "конфликт",
        "ссор",
        "груб",
        "крик",
        "токсич",
        "напряж",
        "руга",
    ),
    "stress": (
        "нагруз",
        "перегруз",
        "устал",
        "стресс",
        "завал",
        "не успева",
    ),
    "guests": (
        "гост",
        "клиент",
        "жалоб",
        "стол",
        "чаевы",
        "сервис",
    ),
}

RECOMMENDATIONS: dict[str, str] = {
    "kitchen": (
        "С шефом разберите один пик: где ломается тайминг; "
        "зал и кухня договариваются о приоритетах на раздаче — без поиска виноватых."
    ),
    "team": (
        "Тет-а-тет без публичного суда: что давит в команде; "
        "вы учитесь держать безопасность, линия — говорить раньше."
    ),
    "staff": (
        "Сверьте зоны и подмены на 3 смены; покажите линии, что дыры закрываете вы, "
        "а не чужими руками молча."
    ),
    "processes": (
        "Пройдите открытие/закрытие с линией и закройте 3 дыры в ролях; "
        "просите одно предложение «как упростить», не только жалобу."
    ),
    "management": (
        "До пика пройдите планёрку и зоны; команда должна уйти с ясностью, "
        "а не с ощущением «разберёмся на ходу»."
    ),
    "self": (
        "Тет-а-тет без «соберись»: как человек себя чувствует и что поможет; "
        "проверьте переработки — вы слышите, человек учится просить поддержку."
    ),
    "conflict": (
        "Разберите конфликтные точки зал ↔ кухня / бар без публичного суда; "
        "обе стороны выходят с правилом «как говорим на пике»."
    ),
    "stress": (
        "Пересмотрите плотность смен и переработки; "
        "линия учится сигналить о перегрузе, вы — балансировать до срыва."
    ),
    "guests": (
        "Зафиксируйте сценарий сложного гостя: кто подключается и какие слова можно; "
        "сотрудник не остаётся один, вы растёте в рамке сервиса."
    ),
    "rating_drop": (
        "На планёрке без имён: что повторяется в голосе линии; "
        "одна дыра в системе на эту неделю + проверка через 3–5 дней."
    ),
    "improved": (
        "Зафиксируйте, что сработало, и скажите команде вслух — "
        "так растут и вы (как управленец), и они (видят эффект)."
    ),
}


@dataclass
class ManagerAlert:
    kind: str
    code: str | None
    title: str
    body_lines: list[str]
    recommendation: str
    comments: list[str]
    priority: int
    problem_key: str | None = None


def alert_dedupe_key(chat_id: int, kind: str, code: str | None) -> str:
    return f"{chat_id}|mgr_alert|{kind}|{code or '-'}"


def alert_recently_sent(
    sent_map: dict[str, Any],
    key: str,
    *,
    now: datetime | None = None,
    cooldown_hours: int = ALERT_COOLDOWN_HOURS,
) -> bool:
    raw = sent_map.get(key)
    if raw is None:
        return False
    if raw is True:
        return True
    try:
        ts = datetime.fromisoformat(str(raw))
    except Exception:
        return True
    ref = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ref.tzinfo)
    return (ref - ts) < timedelta(hours=cooldown_hours)


def mark_alert_sent(
    sent_map: dict[str, Any],
    key: str,
    *,
    now: datetime | None = None,
) -> None:
    ref = now or datetime.now(timezone.utc)
    sent_map[key] = ref.isoformat(timespec="seconds")


def prune_alert_sent_map(
    sent_map: dict[str, Any],
    *,
    now: datetime | None = None,
    keep_hours: int = 48,
) -> bool:
    """Удаляет протухшие ключи mgr_alert. True если что-то удалили."""
    ref = now or datetime.now(timezone.utc)
    changed = False
    for k in list(sent_map.keys()):
        if "|mgr_alert|" not in str(k):
            continue
        raw = sent_map.get(k)
        if raw is True:
            del sent_map[k]
            changed = True
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
        except Exception:
            del sent_map[k]
            changed = True
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ref.tzinfo)
        if (ref - ts) > timedelta(hours=keep_hours):
            del sent_map[k]
            changed = True
    return changed


def _tz(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def _label(code: str, data: dict[str, Any] | None, chat_id: int) -> str:
    if data is not None:
        custom = survey_buttons.labels_map(data, chat_id)
        if code in custom:
            return custom[code]
    return report_pulse.PROBLEM_LABELS.get(
        code, problems_pulse.PROBLEM_TITLES.get(code, code)
    )


def _pct_growth(cur: int, prev: int) -> float | None:
    if prev <= 0:
        return None if cur <= 0 else 999.0
    return (cur - prev) / prev


def _guess_theme(text: str) -> str | None:
    low = (text or "").lower()
    if not low.strip():
        return None
    scores: Counter[str] = Counter()
    for code, keys in THEME_KEYWORDS.items():
        for k in keys:
            if k in low:
                scores[code] += 1
    if not scores:
        return None
    return scores.most_common(1)[0][0]


def _problem_counts(events: list[report_pulse.EventRow]) -> Counter[str]:
    c: Counter[str] = Counter()
    for e in events:
        if e.event_type == report_pulse.EVENT_PROBLEM and e.problem_code:
            c[e.problem_code] += 1
    return c


def _avg_rating(events: list[report_pulse.EventRow]) -> tuple[float | None, int]:
    vals = [
        e.rating
        for e in events
        if e.event_type == report_pulse.EVENT_RATING and e.rating is not None
    ]
    if not vals:
        return None, 0
    return sum(vals) / len(vals), len(vals)


def _comments_for_code(
    events: list[report_pulse.EventRow],
    code: str | None,
    *,
    limit: int = 3,
) -> list[str]:
    rows: list[tuple[float, str]] = []
    for e in events:
        if e.event_type != report_pulse.EVENT_COMMENT:
            continue
        raw = re.sub(r"\s+", " ", (e.comment_text or "").strip())
        if not raw:
            continue
        linked = (e.problem_code or "").strip() or None
        guessed = _guess_theme(raw)
        if code:
            if linked and linked != code:
                continue
            if not linked and guessed and guessed != code:
                continue
            if not linked and not guessed:
                score = report_pulse._vivid_score(raw) * 0.45
            else:
                score = report_pulse._vivid_score(raw)
        else:
            score = report_pulse._vivid_score(raw)
        rows.append((score, raw))
    rows.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for _, text in rows:
        key = text[:80].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(report_pulse._comment_excerpt(text, max_len=140))
        if len(out) >= limit:
            break
    return out


def detect_alerts(
    cur_events: list[report_pulse.EventRow],
    prev_events: list[report_pulse.EventRow],
    *,
    data: dict[str, Any],
    chat_id: int,
) -> list[ManagerAlert]:
    cur_p = _problem_counts(cur_events)
    prev_p = _problem_counts(prev_events)
    cur_avg, cur_n = _avg_rating(cur_events)
    prev_avg, _prev_n = _avg_rating(prev_events)
    alerts: list[ManagerAlert] = []

    for code, cur_n_p in cur_p.most_common(8):
        if cur_n_p < MIN_PROBLEM_MENTIONS:
            continue
        prev_n_p = prev_p.get(code, 0)
        growth = _pct_growth(cur_n_p, prev_n_p)
        is_new = prev_n_p == 0 and cur_n_p >= MIN_PROBLEM_MENTIONS
        is_spike = (
            prev_n_p > 0
            and (cur_n_p - prev_n_p) >= GROWTH_MIN_EXTRA
            and growth is not None
            and growth >= GROWTH_MIN_PCT
        )
        # Абсолютный жар: тема уже «красная» за 72ч, даже если в прошлом окне тоже была
        is_hot = cur_n_p >= HOT_ABSOLUTE and not is_new and not is_spike
        if not (is_new or is_spike or is_hot):
            continue
        label = _label(code, data, chat_id)
        comments = _comments_for_code(cur_events, code)
        if is_new:
            title = f"Новая повторяющаяся проблема: {label}"
            kind = "new_theme"
            priority = 1
            body = [
                f"За последние смены <b>{cur_n_p}</b> отметок по теме «{escape(label)}».",
                "Раньше этой темы почти не было.",
            ]
        elif is_spike:
            pct = int(round((growth or 0) * 100))
            title = f"Резко выросли жалобы: {label}"
            kind = "spike"
            priority = 1 if code in ("kitchen", "team", "self", "processes", "conflict", "stress") else 2
            body = [
                f"{escape(label)} — <b>{cur_n_p}</b> упоминаний "
                f"(+{pct}% к предыдущим сменам)."
            ]
            if cur_avg is not None and prev_avg is not None and cur_avg < prev_avg:
                body.append(
                    f"Средняя оценка смены: <b>{prev_avg:.1f}</b> → <b>{cur_avg:.1f}</b>."
                )
        else:
            title = f"Тема снова горит: {label}"
            kind = "hot"
            priority = 2
            body = [
                f"За {ALERT_WINDOW_HOURS} ч — <b>{cur_n_p}</b> отметок "
                f"«{escape(label)}».",
                "Порог жара уже пробит — стоит взять в работу.",
            ]
            if prev_n_p:
                body.append(f"В прошлом окне было {prev_n_p}.")
            if cur_avg is not None and prev_avg is not None and cur_avg < prev_avg:
                body.append(
                    f"Средняя оценка смены: <b>{prev_avg:.1f}</b> → <b>{cur_avg:.1f}</b>."
                )
        alerts.append(
            ManagerAlert(
                kind=kind,
                code=code,
                title=title,
                body_lines=body,
                recommendation=RECOMMENDATIONS.get(
                    code, RECOMMENDATIONS["rating_drop"]
                ),
                comments=comments,
                priority=priority,
                problem_key=code,
            )
        )

    if (
        cur_avg is not None
        and prev_avg is not None
        and cur_n >= MIN_RATINGS_FOR_DROP
        and (prev_avg - cur_avg) >= RATING_DROP_THRESHOLD
    ):
        top_code = cur_p.most_common(1)[0][0] if cur_p else None
        comments = _comments_for_code(cur_events, top_code)
        drop = prev_avg - cur_avg
        body = [
            f"Средняя оценка смены снизилась с <b>{prev_avg:.1f}</b> до "
            f"<b>{cur_avg:.1f}</b> (−{drop:.1f}).",
            f"Оценок за окно: {cur_n}.",
        ]
        if top_code:
            body.append(
                f"Чаще всего отмечали: <b>{escape(_label(top_code, data, chat_id))}</b> "
                f"({cur_p[top_code]})."
            )
        alerts.append(
            ManagerAlert(
                kind="rating_drop",
                code=top_code,
                title="Просела оценка смен",
                body_lines=body,
                recommendation=RECOMMENDATIONS.get(
                    top_code or "", RECOMMENDATIONS["rating_drop"]
                ),
                comments=comments,
                priority=1,
                problem_key=top_code,
            )
        )

    for code, prev_n_p in prev_p.items():
        if prev_n_p < IMPROVE_PREV_MIN:
            continue
        cur_n_p = cur_p.get(code, 0)
        if cur_n_p > prev_n_p * (1.0 - IMPROVE_DROP_RATIO):
            continue
        label = _label(code, data, chat_id)
        alerts.append(
            ManagerAlert(
                kind="improved",
                code=code,
                title="Проблема перестала повторяться",
                body_lines=[
                    f"«{escape(label)}»: было <b>{prev_n_p}</b> отметок, стало "
                    f"<b>{cur_n_p}</b> за сопоставимое окно.",
                    "Похоже, принятое решение дало результат.",
                ],
                recommendation=RECOMMENDATIONS["improved"],
                comments=[],
                priority=3,
                problem_key=code,
            )
        )

    kind_rank = {
        "spike": 0,
        "new_theme": 0,
        "hot": 1,
        "rating_drop": 2,
        "improved": 3,
    }
    seen: set[tuple[str, str | None]] = set()
    unique: list[ManagerAlert] = []
    for a in sorted(alerts, key=lambda x: (x.priority, kind_rank.get(x.kind, 9), x.kind)):
        key = (a.kind, a.code)
        if key in seen:
            continue
        if a.kind == "rating_drop" and any(
            u.code == a.code and u.kind in ("spike", "new_theme", "hot")
            for u in unique
        ):
            continue
        # Один код — один пуш (спайк важнее «жара»)
        if any(u.code == a.code and u.kind != a.kind for u in unique):
            continue
        seen.add(key)
        unique.append(a)
        if len(unique) >= MAX_ALERTS_PER_CHAT:
            break
    return unique


def format_alert_message(alert: ManagerAlert, *, restaurant_title: str) -> str:
    emoji = {
        "spike": "🚨",
        "new_theme": "⚠️",
        "hot": "🔥",
        "rating_drop": "📉",
        "improved": "✅",
        "comment_trend": "🔎",
    }.get(alert.kind, "🔔")
    lines = [
        f"{emoji} <b>{escape(alert.title)}</b>",
        f"<i>{escape(restaurant_title)}</i>",
        "",
    ]
    lines.extend(alert.body_lines)
    if alert.comments:
        lines.append("")
        lines.append("<b>Что пишут на смене</b> <i>(анонимно)</i>")
        for c in alert.comments[:3]:
            lines.append(f"• «{escape(c)}»")
    lines.append("")
    lines.append(f"<b>Что сделать:</b> {escape(alert.recommendation)}")
    if alert.kind != "improved":
        lines.append("")
        lines.append("<i>Можно сразу взять в работу или открыть список сигналов.</i>")
    return "\n".join(lines)


def alert_keyboard(chat_id: int, problem_id: str | None):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list] = []
    if problem_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="В работу",
                    callback_data=problems_pulse.pr_callback("w", problem_id, "ip"),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Решено",
                    callback_data=problems_pulse.pr_callback("w", problem_id, "rs"),
                ),
                InlineKeyboardButton(
                    text="Игнор",
                    callback_data=problems_pulse.pr_callback("w", problem_id, "ig"),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔥 Горящие вопросы",
                callback_data=problems_pulse.pr_callback("l", str(chat_id)),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_alerts_for_chat(
    data: dict[str, Any],
    chat_id: int,
    *,
    jsonl_path: Path,
    tz_name: str = "Europe/Moscow",
) -> list[tuple[ManagerAlert, str, Any]]:
    """Список (alert, html, keyboard) для отправки менеджерам."""
    tz = _tz(tz_name)
    now = datetime.now(tz)
    cur_start = now - timedelta(hours=ALERT_WINDOW_HOURS)
    prev_start = cur_start - timedelta(hours=ALERT_WINDOW_HOURS)

    cur_events = await report_pulse.load_events(
        [chat_id], cur_start, now, jsonl_path=jsonl_path
    )
    prev_events = await report_pulse.load_events(
        [chat_id], prev_start, cur_start, jsonl_path=jsonl_path
    )
    if not cur_events and not prev_events:
        return []

    info = (data.get("chats") or {}).get(str(chat_id), {}) or {}
    try:
        await problems_pulse.sync_problems_from_period(
            data,
            chat_id,
            info.get("organization_id"),
            jsonl_path=jsonl_path,
            tz_name=tz_name,
            days=problems_pulse.SIGNALS_SYNC_DAYS,
        )
    except Exception as e:
        print(f"[manager-alerts-sync] {chat_id}: {e}")

    active = await problems_pulse.list_problems_for_chat(data, chat_id)
    by_key = {r.problem_key: r for r in active}
    title = str(info.get("title") or chat_id)

    alerts = detect_alerts(cur_events, prev_events, data=data, chat_id=chat_id)
    if not alerts:
        cur_p = _problem_counts(cur_events)
        top = ", ".join(f"{k}:{v}" for k, v in cur_p.most_common(5)) or "—"
        print(
            f"[manager-alerts] chat={chat_id}: detect empty; "
            f"events_cur={len(cur_events)} prev={len(prev_events)} problems=[{top}]"
        )

    out: list[tuple[ManagerAlert, str, Any]] = []
    for alert in alerts:
        html = format_alert_message(alert, restaurant_title=title)
        row = by_key.get(alert.problem_key) if alert.problem_key else None
        kb = alert_keyboard(chat_id, row.id if row else None)
        out.append((alert, html, kb))
    return out
