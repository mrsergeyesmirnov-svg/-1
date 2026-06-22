"""
Чек-листы открытия и закрытия смены.
Шаблон пунктов — на точке; отметки — по дням в ops_days.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from html import escape
from typing import Any, Literal

import ops_day

CheckKind = Literal[
    # morning
    "opening_waiters",
    "opening_hosts",
    "opening_manager",
    # evening
    "closing",
    # legacy (kept for backward compatibility with stored data)
    "opening",
]

OPENING_KEY = "opening_checklist"
CLOSING_KEY = "closing_checklist"


def _items_key(kind: CheckKind) -> str:
    if kind in ("opening", "opening_waiters"):
        return "opening_waiters_checklist"
    if kind == "opening_hosts":
        return "opening_hosts_checklist"
    if kind == "opening_manager":
        return "opening_manager_checklist"
    return CLOSING_KEY


def _checks_key(kind: CheckKind) -> str:
    if kind in ("opening", "opening_waiters"):
        return "opening_waiters_checks"
    if kind == "opening_hosts":
        return "opening_hosts_checks"
    if kind == "opening_manager":
        return "opening_manager_checks"
    return "closing_checks"


def _opened_key(kind: CheckKind) -> str:
    if kind in ("opening", "opening_waiters"):
        return "waiters_opened_at"
    if kind == "opening_hosts":
        return "hosts_opened_at"
    if kind == "opening_manager":
        return "manager_opened_at"
    return "closing_checklist_done_at"


def _legacy_opened_keys(kind: CheckKind) -> tuple[str, ...]:
    if kind == "closing":
        return ("shift_closed_at",)
    if kind in ("opening", "opening_waiters"):
        return ("shift_opened_at",)
    return ()


def kind_code(kind: CheckKind) -> str:
    if kind in ("opening", "opening_waiters"):
        return "ow"
    if kind == "opening_hosts":
        return "oh"
    if kind == "opening_manager":
        return "om"
    return "c"


def kind_from_code(code: str) -> CheckKind | None:
    code = (code or "").strip().lower()
    if code == "ow":
        return "opening_waiters"
    if code == "oh":
        return "opening_hosts"
    if code == "om":
        return "opening_manager"
    if code == "c":
        return "closing"
    if code == "o":
        # legacy callback data
        return "opening_waiters"
    return None


def _legacy_items_key(kind: CheckKind) -> str | None:
    if kind in ("opening", "opening_waiters"):
        return OPENING_KEY
    if kind == "closing":
        return CLOSING_KEY
    return None


def _legacy_checks_key(kind: CheckKind) -> str | None:
    if kind in ("opening", "opening_waiters"):
        return "opening_checks"
    if kind == "closing":
        return "closing_checks"
    return None


def _legacy_opened_key(kind: CheckKind) -> str | None:
    if kind in ("opening", "opening_waiters"):
        return "shift_opened_at"
    if kind == "closing":
        return "shift_closed_at"
    return None


def get_template_items(rec: dict[str, Any], kind: CheckKind) -> list[dict[str, Any]]:
    raw = rec.get(_items_key(kind))
    if not isinstance(raw, list):
        legacy = _legacy_items_key(kind)
        raw = rec.get(legacy) if legacy else None
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        out.append(
            {
                "id": str(item["id"]),
                "text": str(item.get("text") or ""),
                "order": int(item.get("order", i)),
            }
        )
    return sorted(out, key=lambda x: x["order"])


def save_template_items(
    rec: dict[str, Any], kind: CheckKind, items: list[dict[str, Any]]
) -> None:
    rec[_items_key(kind)] = items


def add_template_item(rec: dict[str, Any], kind: CheckKind, text: str) -> tuple[bool, str | None]:
    text = text.strip()
    if len(text) < 2 or len(text) > 120:
        return False, "Текст пункта: от 2 до 120 символов."
    items = get_template_items(rec, kind)
    if len(items) >= 20:
        return False, "Не больше 20 пунктов в чек-листе."
    item_id = "c_" + secrets.token_hex(3)
    items.append({"id": item_id, "text": text, "order": len(items)})
    save_template_items(rec, kind, items)
    return True, None


def delete_template_item(
    rec: dict[str, Any], kind: CheckKind, item_id: str
) -> tuple[bool, str | None]:
    items = get_template_items(rec, kind)
    if not any(i["id"] == item_id for i in items):
        return False, "Пункт не найден."
    items = [i for i in items if i["id"] != item_id]
    for i, item in enumerate(items):
        item["order"] = i
    save_template_items(rec, kind, items)
    return True, None


def get_checks(rec: dict[str, Any], day: str, kind: CheckKind) -> dict[str, Any]:
    bucket = ops_day.get_day_bucket(rec, day)
    raw = bucket.get(_checks_key(kind))
    if isinstance(raw, dict):
        return raw
    legacy = _legacy_checks_key(kind)
    raw = bucket.get(legacy) if legacy else None
    return raw if isinstance(raw, dict) else {}


def _check_entry(checks: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    raw = checks.get(item_id)
    if not isinstance(raw, dict):
        return None
    if "status" not in raw and raw.get("by_uid"):
        return {**raw, "status": "ok"}
    return raw


def _entry_complete(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    status = str(entry.get("status") or "ok")
    if status == "issue":
        return len(str(entry.get("note") or "").strip()) >= 3
    return status in ("ok", "legacy")


def _entry_mark(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "⬜"
    if not _entry_complete(entry):
        return "⚠️"
    status = str(entry.get("status") or "ok")
    return "⚠️" if status == "issue" else "✅"


def set_check_status(
    rec: dict[str, Any],
    day: str,
    kind: CheckKind,
    item_id: str,
    uid: int,
    *,
    status: str,
    note: str = "",
) -> None:
    bucket = ops_day.get_day_bucket(rec, day)
    checks = bucket.setdefault(_checks_key(kind), {})
    if not isinstance(checks, dict):
        checks = {}
        bucket[_checks_key(kind)] = checks
    if status == "clear":
        checks.pop(item_id, None)
        bucket.pop(_opened_key(kind), None)
        for leg in _legacy_opened_keys(kind):
            bucket.pop(leg, None)
        return
    checks[item_id] = {
        "status": status,
        "note": note.strip()[:300] if status == "issue" else "",
        "by_uid": uid,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if all_checked(rec, day, kind):
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        bucket[_opened_key(kind)] = ts
    else:
        bucket.pop(_opened_key(kind), None)
        for leg in _legacy_opened_keys(kind):
            bucket.pop(leg, None)


def toggle_check(
    rec: dict[str, Any], day: str, kind: CheckKind, item_id: str, uid: int
) -> bool:
    """Legacy toggle — помечает пункт «в порядке» или снимает отметку."""
    checks = get_checks(rec, day, kind)
    entry = _check_entry(checks, item_id)
    if entry and _entry_complete(entry):
        set_check_status(rec, day, kind, item_id, uid, status="clear")
        return False
    set_check_status(rec, day, kind, item_id, uid, status="ok")
    return True


def all_checked(rec: dict[str, Any], day: str, kind: CheckKind) -> bool:
    items = get_template_items(rec, kind)
    if not items:
        return True
    checks = get_checks(rec, day, kind)
    return all(_entry_complete(_check_entry(checks, i["id"])) for i in items)


def shift_gate_complete(rec: dict[str, Any], day: str, kind: CheckKind) -> bool:
    """Открытие/закрытие смены по чек-листу (если пункты заданы)."""
    items = get_template_items(rec, kind)
    if not items:
        return True
    bucket = ops_day.get_day_bucket(rec, day)
    if bucket.get(_opened_key(kind)) and all_checked(rec, day, kind):
        return True
    for leg in _legacy_opened_keys(kind):
        if leg in bucket and all_checked(rec, day, kind):
            return True
    return all_checked(rec, day, kind)


def morning_gate_complete(rec: dict[str, Any], day: str) -> bool:
    """Полное утро: официанты + хосты + менеджер (если на точке заданы пункты)."""
    return (
        shift_gate_complete(rec, day, "opening_waiters")
        and shift_gate_complete(rec, day, "opening_hosts")
        and shift_gate_complete(rec, day, "opening_manager")
    )


def kind_title(kind: CheckKind) -> str:
    if kind in ("opening", "opening_waiters"):
        return "Открытие · Официанты"
    if kind == "opening_hosts":
        return "Открытие · Хосты"
    if kind == "opening_manager":
        return "Открытие · Менеджер"
    return "Закрытие смены"


def format_config_message(
    rec: dict[str, Any], chat_title: str, kind: CheckKind
) -> str:
    items = get_template_items(rec, kind)
    title = kind_title(kind)
    lines = [
        f"📋 <b>{title}</b> · {escape(chat_title)}",
        f"Пунктов: <b>{len(items)}</b>",
        "",
        "<i>Менеджер отмечает каждый пункт: «в порядке» или «замечание» с комментарием.</i>",
        "",
    ]
    if items:
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. {escape(item['text'])}")
    else:
        lines.append("<i>Пунктов пока нет — добавьте ниже.</i>")
    return "\n".join(lines)


def config_keyboard(chat_id: int, kind: CheckKind):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    k = "o" if kind == "opening" else "c"
    rows: list[list] = [
        [
            InlineKeyboardButton(
                text="➕ Добавить пункт",
                callback_data=f"cl:add:{k}:{cid}"[:64],
            )
        ],
    ]
    items = []  # filled by caller if needed — use dynamic in bot
    return rows, k


def build_config_keyboard(rec: dict[str, Any], chat_id: int, kind: CheckKind):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    k = kind_code(kind)
    rows: list[list] = []
    for item in get_template_items(rec, kind)[:15]:
        short = item["text"][:30] + ("…" if len(item["text"]) > 30 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 {short}",
                    callback_data=f"cl:del:{k}:{cid}:{item['id']}"[:64],
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➕ Добавить пункт", callback_data=f"cl:add:{k}:{cid}")]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"cl:menu:{cid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def checklist_menu_keyboard(chat_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="☀️ Официанты",
                    callback_data=f"cl:cfg:ow:{cid}",
                ),
                InlineKeyboardButton(
                    text="☀️ Хосты",
                    callback_data=f"cl:cfg:oh:{cid}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="☀️ Менеджер",
                    callback_data=f"cl:cfg:om:{cid}",
                ),
                InlineKeyboardButton(
                    text="🌙 Закрытие",
                    callback_data=f"cl:cfg:c:{cid}",
                ),
            ],
        ]
    )


def format_checklist_prompt(
    rec: dict[str, Any], chat_title: str, day: str, kind: CheckKind
) -> str:
    items = get_template_items(rec, kind)
    checks = get_checks(rec, day, kind)
    done = sum(
        1 for i in items if _entry_complete(_check_entry(checks, i["id"]))
    )
    title = kind_title(kind)
    lines = [
        f"<b>{title}</b> · {escape(chat_title)}",
        f"Проверено: <b>{done}/{len(items)}</b>",
        "",
    ]
    for item in items:
        entry = _check_entry(checks, item["id"])
        mark = _entry_mark(entry)
        line = f"{mark} {escape(item['text'])}"
        if entry and str(entry.get("status")) == "issue" and entry.get("note"):
            note = str(entry["note"]).strip()
            if len(note) > 80:
                note = note[:77] + "…"
            line += f"\n   <i>{escape(note)}</i>"
        lines.append(line)
    if done < len(items):
        lines.append(
            "\n<i>Нажмите пункт → «В порядке» или «Замечание» (+ комментарий).</i>"
        )
    return "\n".join(lines)


def item_action_keyboard(
    rec: dict[str, Any], chat_id: int, day: str, kind: CheckKind, item_id: str
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    k = kind_code(kind)
    items = {i["id"]: i for i in get_template_items(rec, kind)}
    item = items.get(item_id)
    if not item:
        return run_checklist_keyboard(rec, chat_id, day, kind)
    checks = get_checks(rec, day, kind)
    entry = _check_entry(checks, item_id)
    rows: list[list] = [
        [
            InlineKeyboardButton(
                text="✅ В порядке",
                callback_data=f"cl:ok:{k}:{cid}:{day}:{item_id}"[:64],
            )
        ],
        [
            InlineKeyboardButton(
                text="⚠️ Замечание",
                callback_data=f"cl:iss:{k}:{cid}:{day}:{item_id}"[:64],
            )
        ],
    ]
    if entry:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬜ Снять отметку",
                    callback_data=f"cl:clr:{k}:{cid}:{day}:{item_id}"[:64],
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="← К списку",
                callback_data=f"cl:back:{k}:{cid}:{day}"[:64],
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_item_action_prompt(
    rec: dict[str, Any], chat_title: str, day: str, kind: CheckKind, item_id: str
) -> str:
    items = {i["id"]: i for i in get_template_items(rec, kind)}
    item = items.get(item_id)
    if not item:
        return format_checklist_prompt(rec, chat_title, day, kind)
    checks = get_checks(rec, day, kind)
    entry = _check_entry(checks, item_id)
    lines = [
        f"<b>{kind_title(kind)}</b> · {escape(chat_title)}",
        "",
        f"<b>{escape(item['text'])}</b>",
        "",
    ]
    if entry and str(entry.get("status")) == "issue" and entry.get("note"):
        lines.append(f"Замечание: <i>{escape(str(entry['note']))}</i>\n")
    lines.append("<i>Как прошла проверка?</i>")
    return "\n".join(lines)


def run_checklist_keyboard(rec: dict[str, Any], chat_id: int, day: str, kind: CheckKind):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    k = kind_code(kind)
    checks = get_checks(rec, day, kind)
    rows: list[list] = []
    for item in get_template_items(rec, kind):
        mark = _entry_mark(_check_entry(checks, item["id"]))
        short = item["text"][:28] + ("…" if len(item["text"]) > 28 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {short}",
                    callback_data=f"cl:itm:{k}:{cid}:{day}:{item['id']}"[:64],
                )
            ]
        )
    if shift_gate_complete(rec, day, kind):
        if kind in ("opening", "opening_waiters"):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✔️ К плану дня",
                        callback_data=f"ops:mcont:{cid}:{day}"[:64],
                    )
                ]
            )
        elif kind == "closing":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✔️ Продолжить",
                        callback_data=f"ops:econt:{cid}:{day}"[:64],
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="← К меню чек-листов",
                        callback_data=f"cl:menu:{cid}"[:64],
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


_CHECKLIST_REPORT_KINDS: tuple[CheckKind, ...] = (
    "opening_waiters",
    "opening_hosts",
    "opening_manager",
    "closing",
)


def collect_checklist_issues(
    rec: dict[str, Any],
    start: datetime,
    end: datetime,
    tz_name: str,
) -> list[dict[str, Any]]:
    """Замечания по чек-листам за период (status=issue с комментарием)."""
    from datetime import time
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    if isinstance(start, datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
    else:
        start = datetime.combine(start, time.min, tzinfo=tz)
    if isinstance(end, datetime):
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
    else:
        end = datetime.combine(end, time.max, tzinfo=tz)

    days_map = rec.get("ops_days")
    if not isinstance(days_map, dict):
        return []

    item_labels: dict[tuple[str, str], str] = {}
    for kind in _CHECKLIST_REPORT_KINDS:
        for item in get_template_items(rec, kind):
            item_labels[(kind, item["id"])] = item["text"]

    out: list[dict[str, Any]] = []
    cur = start.astimezone(tz).date()
    end_d = end.astimezone(tz).date()
    while cur <= end_d:
        day = cur.isoformat()
        bucket = days_map.get(day)
        if isinstance(bucket, dict):
            for kind in _CHECKLIST_REPORT_KINDS:
                checks = bucket.get(_checks_key(kind))
                if not isinstance(checks, dict):
                    legacy = _legacy_checks_key(kind)
                    checks = bucket.get(legacy) if legacy else None
                if not isinstance(checks, dict):
                    continue
                for item_id, raw in checks.items():
                    entry = raw if isinstance(raw, dict) else None
                    if not entry or str(entry.get("status")) != "issue":
                        continue
                    note = str(entry.get("note") or "").strip()
                    if len(note) < 3:
                        continue
                    label = item_labels.get((kind, str(item_id)), str(item_id))
                    out.append(
                        {
                            "day": day,
                            "kind": kind,
                            "kind_title": kind_title(kind),
                            "item": label,
                            "note": note,
                        }
                    )
        cur += timedelta(days=1)
    out.sort(key=lambda x: (x["day"], x["kind_title"], x["item"]), reverse=True)
    return out


def day_for_kind(rec: dict[str, Any], kind: CheckKind, tz_name: str) -> str:
    if kind == "closing":
        sched = ops_day.ops_schedule(rec)
        return ops_day.shift_day_for_evening(tz_name, sched["morning_post"])
    return ops_day.today_key(tz_name)


def format_checklist_issues_block(issues: list[dict[str, Any]], *, limit: int = 12) -> str | None:
    if not issues:
        return None
    from collections import Counter

    counter: Counter[str] = Counter()
    samples: dict[str, str] = {}
    for row in issues:
        key = f"{row['kind_title']} · {row['item']}"
        counter[key] += 1
        if key not in samples:
            samples[key] = row["note"]
    lines = [
        "<b>Замечания по чек-листам</b>",
        "<i>что чаще отмечали с комментарием</i>",
    ]
    for key, n in counter.most_common(limit):
        lines.append(f"• {escape(key)} — <b>{n}</b>")
        sample = samples.get(key, "")
        if sample:
            if len(sample) > 100:
                sample = sample[:97] + "…"
            lines.append(f"  <i>«{escape(sample)}»</i>")
    if len(counter) > limit:
        lines.append(f"<i>…ещё {len(counter) - limit} пунктов</i>")
    return "\n".join(lines)
