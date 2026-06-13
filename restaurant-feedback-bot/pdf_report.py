"""
PDF-сводка для руководства из HTML-текста отчёта Pulse Team.
"""
from __future__ import annotations

import re
from html import unescape
from io import BytesIO
from pathlib import Path

FONT_PATH = Path(__file__).resolve().parent / "assets" / "DejaVuSans.ttf"


def _strip_html(text: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"</?blockquote>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def build_pdf_bytes(
    parts: list[str],
    *,
    title: str = "Pulse Team — сводка",
) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    if FONT_PATH.exists():
        pdf.add_font("DejaVu", "", str(FONT_PATH))
        pdf.set_font("DejaVu", size=11)
        title_font = "DejaVu"
    else:
        pdf.set_font("Helvetica", size=11)
        title_font = "Helvetica"

    pdf.set_font(title_font, size=14)
    pdf.multi_cell(0, 8, _strip_html(title))
    pdf.ln(4)
    pdf.set_font(title_font, size=10)

    for part in parts:
        plain = _strip_html(part)
        if not plain:
            continue
        for line in plain.split("\n"):
            line = line.strip()
            if not line:
                pdf.ln(3)
                continue
            try:
                pdf.multi_cell(0, 5.5, line)
            except Exception:
                pdf.multi_cell(0, 5.5, line.encode("latin-1", "replace").decode("latin-1"))
        pdf.ln(4)

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def pdf_filename(scope_title: str, period_label: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "", scope_title, flags=re.UNICODE)
    safe = re.sub(r"\s+", "_", safe.strip())[:40] or "report"
    period = re.sub(r"[^\w\-]", "_", period_label)[:20]
    return f"Pulse_{safe}_{period}.pdf"
