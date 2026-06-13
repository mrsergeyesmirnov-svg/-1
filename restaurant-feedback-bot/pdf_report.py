"""
PDF-сводка для руководства из HTML-текста отчёта Pulse Team.
"""
from __future__ import annotations

import re
from html import unescape
from io import BytesIO
from pathlib import Path

FONT_PATH = Path(__file__).resolve().parent / "assets" / "DejaVuSans.ttf"

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FEFF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def _strip_html(text: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"</?blockquote>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    s = _EMOJI_RE.sub("", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _wrap_chunks(text: str, max_len: int = 120) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= max_len:
            out.append(text)
            break
        cut = text.rfind(" ", 0, max_len + 1)
        if cut <= 0:
            cut = max_len
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    return out


def _write_lines(pdf, lines: list[str], *, line_h: float = 5.5) -> None:
    width = pdf.epw
    for line in lines:
        if not line:
            pdf.ln(line_h * 0.6)
            continue
        for chunk in _wrap_chunks(line):
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(width, line_h, chunk)


def build_pdf_bytes(
    parts: list[str],
    *,
    title: str = "Pulse Team — сводка",
) -> bytes:
    try:
        from fpdf import FPDF
    except ImportError as e:
        raise RuntimeError("fpdf2_missing") from e

    if not FONT_PATH.exists():
        raise RuntimeError("font_missing")

    pdf = FPDF()
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.add_font("DejaVu", "", str(FONT_PATH))
    pdf.set_font("DejaVu", size=14)

    _write_lines(pdf, [_strip_html(title)], line_h=7)
    pdf.ln(3)
    pdf.set_font("DejaVu", size=10)

    for part in parts:
        plain = _strip_html(part)
        if not plain:
            continue
        _write_lines(pdf, plain.split("\n"))
        pdf.ln(2)

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def pdf_filename(scope_title: str, period_label: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "", scope_title, flags=re.UNICODE)
    safe = re.sub(r"\s+", "_", safe.strip())[:40] or "report"
    period = re.sub(r"[^\w\-]", "_", period_label)[:20]
    return f"Pulse_{safe}_{period}.pdf"
