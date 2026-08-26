"""
PDF отчёта ИИ-аудита: индекс здоровья + 4 блока.
"""
from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

FONT_PATH = Path(__file__).resolve().parent / "assets" / "DejaVuSans.ttf"

INK = (28, 25, 23)
MUTED = (87, 83, 78)
ACCENT = (255, 90, 31)
LINE = (231, 226, 220)
BG_WARM = (250, 247, 243)
WHITE = (255, 255, 255)
HERO = (28, 25, 23)
GREEN = (22, 163, 74)
AMBER = (217, 119, 6)
RED = (220, 38, 38)
TEAL = (13, 148, 136)

BLOCK_ORDER = (
    ("people", "Люди и команда"),
    ("processes", "Процессы и смена"),
    ("guest", "Гость и сервис"),
    ("finance_ops", "Финансы и операционка"),
)

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FEFF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def _plain(text: str) -> str:
    s = _EMOJI_RE.sub("", text or "")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def _score_color(score: int) -> tuple[int, int, int]:
    if score >= 70:
        return GREEN
    if score >= 50:
        return AMBER
    return RED


def _fmt_dt(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return iso[:19]


def build_audit_pdf_bytes(
    analysis: dict[str, Any],
    *,
    location: str,
    audit_id: str = "",
    completed_at: str = "",
) -> bytes:
    try:
        from fpdf import FPDF
    except ImportError as e:
        raise RuntimeError("fpdf2_missing") from e

    if not FONT_PATH.exists():
        raise RuntimeError("font_missing")

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    pdf.add_font("DejaVu", "", str(FONT_PATH))
    font = "DejaVu"

    # Hero bar
    pdf.set_fill_color(*HERO)
    pdf.rect(0, 0, 210, 42, "F")
    pdf.set_text_color(*WHITE)
    pdf.set_font(font, size=11)
    pdf.set_xy(14, 10)
    pdf.cell(0, 6, "Pulse Team  ·  ИИ-аудит операционного здоровья")
    pdf.set_font(font, size=18)
    pdf.set_xy(14, 18)
    pdf.cell(0, 9, _plain(location)[:70] or "Точка")
    pdf.set_font(font, size=9)
    pdf.set_xy(14, 30)
    meta = "  ·  ".join(
        x for x in (_fmt_dt(completed_at), audit_id) if x
    )
    pdf.cell(0, 5, meta)

    y = 50
    overall = int(analysis.get("overall_index") or 0)
    pdf.set_fill_color(*BG_WARM)
    pdf.rect(14, y, 182, 28, "F")
    pdf.set_draw_color(*LINE)
    pdf.rect(14, y, 182, 28, "D")

    pdf.set_xy(18, y + 5)
    pdf.set_text_color(*MUTED)
    pdf.set_font(font, size=9)
    pdf.cell(60, 5, "Индекс здоровья")
    pdf.set_xy(18, y + 11)
    pdf.set_text_color(*_score_color(overall))
    pdf.set_font(font, size=22)
    pdf.cell(40, 10, f"{overall}/100")

    summary = _plain(str(analysis.get("summary") or ""))
    pdf.set_xy(70, y + 6)
    pdf.set_text_color(*INK)
    pdf.set_font(font, size=9)
    pdf.multi_cell(120, 4.5, summary[:500])

    y = max(pdf.get_y() + 6, y + 34)
    pdf.set_y(y)

    # Block score strip
    blocks = analysis.get("blocks") or {}
    box_w = 43
    gap = 3
    x0 = 14
    for i, (key, title) in enumerate(BLOCK_ORDER):
        b = blocks.get(key) or {}
        sc = int(b.get("score") or 0)
        x = x0 + i * (box_w + gap)
        pdf.set_fill_color(*WHITE)
        pdf.set_draw_color(*LINE)
        pdf.rect(x, y, box_w, 18, "FD")
        pdf.set_xy(x + 2, y + 2)
        pdf.set_text_color(*MUTED)
        pdf.set_font(font, size=7)
        pdf.cell(box_w - 4, 4, title[:22])
        pdf.set_xy(x + 2, y + 8)
        pdf.set_text_color(*_score_color(sc))
        pdf.set_font(font, size=14)
        pdf.cell(box_w - 4, 7, str(sc))

    pdf.set_xy(14, y + 24)

    priorities = analysis.get("top_priorities") or []
    if priorities:
        pdf.set_text_color(*ACCENT)
        pdf.set_font(font, size=11)
        pdf.cell(0, 7, "Приоритеты", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)
        pdf.set_font(font, size=9)
        for p in priorities[:5]:
            pdf.set_x(14)
            pdf.multi_cell(182, 5, f"- {_plain(str(p))}")
        pdf.ln(2)

    for key, title in BLOCK_ORDER:
        b = blocks.get(key) or {}
        sc = int(b.get("score") or 0)
        if pdf.get_y() > 250:
            pdf.add_page()
        yb = pdf.get_y()
        pdf.set_fill_color(*TEAL)
        pdf.rect(14, yb, 3, 8, "F")
        pdf.set_xy(20, yb)
        pdf.set_text_color(*INK)
        pdf.set_font(font, size=12)
        pdf.cell(140, 8, title)
        pdf.set_text_color(*_score_color(sc))
        pdf.set_font(font, size=12)
        pdf.cell(36, 8, f"{sc}/100", align="R", new_x="LMARGIN", new_y="NEXT")

        def _bullets(label: str, items: list) -> None:
            clean = [_plain(str(x)) for x in (items or []) if str(x).strip()]
            if not clean:
                return
            pdf.set_x(14)
            pdf.set_text_color(*MUTED)
            pdf.set_font(font, size=8)
            pdf.cell(0, 5, label, new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*INK)
            pdf.set_font(font, size=9)
            for it in clean[:8]:
                pdf.set_x(14)
                pdf.multi_cell(182, 4.5, f"- {it}")

        _bullets("Находки", b.get("findings") or [])
        _bullets("Риски", b.get("risks") or [])
        _bullets("Быстрые шаги", b.get("quick_wins") or [])
        pdf.ln(3)

    quotes = analysis.get("quotes") or []
    if quotes:
        if pdf.get_y() > 240:
            pdf.add_page()
        pdf.set_x(14)
        pdf.set_text_color(*ACCENT)
        pdf.set_font(font, size=11)
        pdf.cell(0, 7, "Формулировки из разговора", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*INK)
        pdf.set_font(font, size=9)
        for q in quotes[:5]:
            pdf.set_x(14)
            pdf.multi_cell(182, 5, f'"{_plain(str(q))}"')
            pdf.ln(1)

    # Footer
    pdf.set_y(-14)
    pdf.set_draw_color(*LINE)
    pdf.line(14, pdf.get_y() - 2, 196, pdf.get_y() - 2)
    pdf.set_font(font, size=7.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(0, 4, "Pulse Team  ·  pulseteam.online  ·  конфиденциально", align="C")

    out = BytesIO()
    pdf.output(out)
    return out.getvalue()
