"""
Календарный месяц: план на месяц, агрегация ops_days, блок отчёта.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import ops_day

MONTH_NAMES_RU = {
    1: "январь",
    2: "февраль",
    3: "март",
    4: "апрель",
    5: "май",
    6: "июнь",
    7: "июль",
    8: "август",
    9: "сентябрь",
    10: "октябрь",
    11: "ноябрь",
    12: "декабрь",
}

MONTH_NAMES_RU_GEN = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def month_key_from_date(d: date) -> str:
    return d.strftime("%Y-%m")


def parse_month_key(raw: str) -> tuple[int, int] | None:
    try:
        y, m = raw.split("-", 1)
        yi, mi = int(y), int(m)
        if 1 <= mi <= 12:
            return yi, mi
    except (ValueError, AttributeError):
        pass
    return None


def month_label_ru(month_key: str, *, genitive: bool = False) -> str:
    parsed = parse_month_key(month_key)
    if not parsed:
        return month_key
    y, m = parsed
    names = MONTH_NAMES_RU_GEN if genitive else MONTH_NAMES_RU
    return f"{names[m].capitalize()} {y}"


def recent_month_keys(tz_name: str = "Europe/Moscow", count: int = 4) -> list[str]:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    d = datetime.now(tz).date().replace(day=1)
    keys: list[str] = []
    for _ in range(count):
        keys.append(month_key_from_date(d))
        d = (d - timedelta(days=1)).replace(day=1)
    return keys


def calendar_month_window(
    month_key: str, tz_name: str
) -> tuple[datetime, datetime, datetime, datetime, str]:
    """Текущий и предыдущий календарный месяц для сравнения трендов."""
    parsed = parse_month_key(month_key)
    if not parsed:
        raise ValueError(f"bad month_key: {month_key}")
    y, m = parsed
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    start = datetime(y, m, 1, 0, 0, 0, tzinfo=tz)
    last_day = monthrange(y, m)[1]
    end = datetime(y, m, last_day, 23, 59, 59, tzinfo=tz)
    now = datetime.now(tz)
    if end > now:
        end = now
    if m == 1:
        py, pm = y - 1, 12
    else:
        py, pm = y, m - 1
    prev_start = datetime(py, pm, 1, 0, 0, 0, tzinfo=tz)
    prev_last = monthrange(py, pm)[1]
    prev_end = datetime(py, pm, prev_last, 23, 59, 59, tzinfo=tz)
    label = month_label_ru(month_key)
    return start, end, prev_start, prev_end, label


def get_monthly_plan(rec: dict[str, Any], month_key: str) -> dict[str, Any]:
    raw = rec.get("monthly_plans")
    if not isinstance(raw, dict):
        return {}
    row = raw.get(month_key)
    return row if isinstance(row, dict) else {}


def monthly_plan_amount(rec: dict[str, Any], month_key: str) -> int | None:
    row = get_monthly_plan(rec, month_key)
    v = ops_day.parse_amount(str(row.get("revenue_plan") or ""))
    return v


def save_monthly_plan(
    data: dict[str, Any],
    chat_id: int,
    *,
    month_key: str,
    revenue_plan: int,
    by_uid: int,
) -> None:
    rec = ops_day._chat_rec(data, chat_id)
    plans = rec.setdefault("monthly_plans", {})
    if not isinstance(plans, dict):
        plans = {}
        rec["monthly_plans"] = plans
    plans[month_key] = {
        "revenue_plan": revenue_plan,
        "by_uid": by_uid,
        "set_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def aggregate_calendar_month(rec: dict[str, Any], month_key: str, tz_name: str) -> dict[str, Any]:
    parsed = parse_month_key(month_key)
    if not parsed:
        return {}
    y, m = parsed
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    start, end, _, _, label = calendar_month_window(month_key, tz_name)
    days_map = rec.get("ops_days", {}) if isinstance(rec.get("ops_days"), dict) else {}

    total_actual = 0
    total_daily_plan = 0
    days_with_close = 0
    days_with_morning = 0
    days_met_yes = 0
    days_met_partial = 0
    days_met_no = 0
    daily_pcts: list[float] = []
    day_rows: list[dict[str, Any]] = []
    stops_published = 0
    total_headcount = 0
    days_with_headcount = 0

    last_day = monthrange(y, m)[1]
    today = datetime.now(tz).date()
    for day_num in range(1, last_day + 1):
        d = date(y, m, day_num)
        if d > today:
            break
        day_key = d.isoformat()
        bucket = days_map.get(day_key, {}) if isinstance(days_map.get(day_key), dict) else {}
        morning = bucket.get("morning", {}) if isinstance(bucket.get("morning"), dict) else {}
        evening = bucket.get("evening", {}) if isinstance(bucket.get("evening"), dict) else {}
        chef_stop = bucket.get("chef_stop", {}) if isinstance(bucket.get("chef_stop"), dict) else {}

        plan_amt = ops_day._morning_revenue_plan(morning)
        actual = ops_day.parse_amount(str(evening.get("revenue") or ""))
        met = evening.get("plan_met")
        pct = evening.get("revenue_pct")
        try:
            pct_f = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct_f = None

        if ops_day.morning_complete(morning):
            days_with_morning += 1
            if plan_amt:
                total_daily_plan += plan_amt
            try:
                hc = int(morning.get("shift_headcount") or 0)
                if hc > 0:
                    total_headcount += hc
                    days_with_headcount += 1
            except (TypeError, ValueError):
                pass

        if ops_day.evening_complete(evening) and actual is not None:
            days_with_close += 1
            total_actual += actual
            if met == ops_day.PLAN_MET_YES:
                days_met_yes += 1
            elif met == ops_day.PLAN_MET_PARTIAL:
                days_met_partial += 1
            elif met == ops_day.PLAN_MET_NO:
                days_met_no += 1
            if pct_f is not None:
                daily_pcts.append(pct_f)

        if ops_day.chef_stop_has_content(chef_stop):
            stops_published += 1

        if actual is not None or plan_amt:
            day_rows.append(
                {
                    "day": day_key,
                    "plan": plan_amt,
                    "actual": actual,
                    "pct": pct_f,
                    "met": met,
                }
            )

    month_plan = monthly_plan_amount(rec, month_key)
    month_pct = (
        round(100.0 * total_actual / month_plan, 1)
        if month_plan and month_plan > 0
        else None
    )
    avg_daily_pct = round(sum(daily_pcts) / len(daily_pcts), 1) if daily_pcts else None
    ops_days_total = days_with_close or 1
    elapsed = len([d for d in range(1, last_day + 1) if date(y, m, d) <= today])
    discipline_pct = round(100.0 * days_with_morning / max(1, elapsed), 0)

    best_day = None
    worst_day = None
    if daily_pcts:
        ranked = [r for r in day_rows if r.get("pct") is not None]
        if ranked:
            best = max(ranked, key=lambda x: x["pct"])
            worst = min(ranked, key=lambda x: x["pct"])
            best_day = (best["day"], best["pct"])
            worst_day = (worst["day"], worst["pct"])

    return {
        "month_key": month_key,
        "label": label,
        "start": start,
        "end": end,
        "monthly_plan": month_plan,
        "total_actual": total_actual,
        "total_daily_plan_sum": total_daily_plan,
        "month_pct": month_pct,
        "days_with_morning": days_with_morning,
        "days_with_close": days_with_close,
        "days_met_yes": days_met_yes,
        "days_met_partial": days_met_partial,
        "days_met_no": days_met_no,
        "avg_daily_pct": avg_daily_pct,
        "daily_pcts": daily_pcts,
        "best_day": best_day,
        "worst_day": worst_day,
        "stops_published": stops_published,
        "total_headcount": total_headcount,
        "days_with_headcount": days_with_headcount,
        "discipline_morning_pct": discipline_pct,
        "day_rows": day_rows,
        "calendar_days_elapsed": len([d for d in range(1, last_day + 1) if date(y, m, d) <= today]),
    }


def format_calendar_month_ops_block(stats: dict[str, Any]) -> str:
    if not stats:
        return ""
    lines = [
        "<b>💰 Финансы за месяц</b>",
    ]
    mp = stats.get("monthly_plan")
    actual = stats.get("total_actual", 0)
    if mp:
        pct = stats.get("month_pct")
        pct_s = f" · <b>{pct}%</b>" if pct is not None else ""
        lines.append(
            f"План на месяц: <b>{escape(ops_day.format_money(mp))}</b>"
        )
        lines.append(
            f"Факт (сумма смен): <b>{escape(ops_day.format_money(actual))}</b>{pct_s}"
        )
        if stats.get("month_pct") is not None:
            if stats["month_pct"] >= 100:
                lines.append("Итог по месяцу: ✅ <b>план выполнен</b>")
            elif stats["month_pct"] >= 85:
                lines.append("Итог по месяцу: 🟡 <b>частично</b>")
            else:
                lines.append("Итог по месяцу: ❌ <b>не выполнен</b>")
    else:
        lines.append(
            f"Факт за месяц: <b>{escape(ops_day.format_money(actual))}</b>"
        )
        lines.append("<i>План на месяц не задан — задайте в «План на месяц».</i>")

    sum_plan = stats.get("total_daily_plan_sum", 0)
    if sum_plan and actual:
        daily_ratio = round(100.0 * actual / sum_plan, 1)
        lines.append(
            f"Сумма дневных планов: {escape(ops_day.format_money(sum_plan))} "
            f"· факт/планы: <b>{daily_ratio}%</b>"
        )

    lines.extend(["", "<b>📈 Эффективность смен</b>"])
    dc = stats.get("days_with_close", 0)
    dm = stats.get("days_with_morning", 0)
    lines.append(
        f"Закрыто смен: <b>{dc}</b> · утренних планов: <b>{dm}</b> · "
        f"стоп-листов: <b>{stats.get('stops_published', 0)}</b>"
    )
    if stats.get("avg_daily_pct") is not None:
        lines.append(f"Средний % выполнения дневного плана: <b>{stats['avg_daily_pct']}%</b>")
    yes_n = stats.get("days_met_yes", 0)
    part_n = stats.get("days_met_partial", 0)
    no_n = stats.get("days_met_no", 0)
    if dc:
        lines.append(
            f"По дням: ✅ <b>{yes_n}</b> · 🟡 <b>{part_n}</b> · ❌ <b>{no_n}</b>"
        )
    best = stats.get("best_day")
    worst = stats.get("worst_day")
    if best:
        lines.append(
            f"Лучший день: <b>{escape(best[0])}</b> ({best[1]:.0f}%)"
        )
    if worst and worst != best:
        lines.append(
            f"Слабый день: <b>{escape(worst[0])}</b> ({worst[1]:.0f}%)"
        )
    if stats.get("days_with_headcount"):
        avg_hc = round(stats["total_headcount"] / stats["days_with_headcount"], 1)
        lines.append(f"Средняя численность на смене: <b>{avg_hc}</b> чел.")
    return "\n".join(lines)


def format_calendar_month_discipline_block(stats: dict[str, Any]) -> str:
    if not stats:
        return ""
    elapsed = stats.get("calendar_days_elapsed", 0)
    dm = stats.get("days_with_morning", 0)
    dc = stats.get("days_with_close", 0)
    if not elapsed:
        return ""
    m_pct = round(100 * dm / elapsed) if elapsed else 0
    c_pct = round(100 * dc / elapsed) if elapsed else 0
    return (
        "<b>🎯 Операционная дисциплина</b>\n"
        f"Утренний план: <b>{dm}</b> из {elapsed} дн. ({m_pct}%)\n"
        f"Закрытие дня: <b>{dc}</b> из {elapsed} дн. ({c_pct}%)\n"
        f"Стоп-листы шефа: <b>{stats.get('stops_published', 0)}</b> из {elapsed} дн."
    )
