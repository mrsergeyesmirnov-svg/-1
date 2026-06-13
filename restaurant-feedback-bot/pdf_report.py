"""
PDF-сводка для руководства — стиль близкий к one-pager Pulse Team.
"""
from __future__ import annotations

import re
from html import unescape
from io import BytesIO
from pathlib import Path

FONT_PATH = Path(__file__).resolve().parent / "assets" / "DejaVuSans.ttf"

# One-pager palette
INK = (28, 25, 23)
MUTED = (87, 83, 78)
ACCENT = (255, 90, 31)
CALM = (13, 148, 136)
LINE = (231, 226, 220)
BG_WARM = (250, 247, 243)
WHITE = (255, 255, 255)

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FEFF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_SECTION_HINTS = (
    "что мешало",
    "что было сложным",
    "самочувствие",
    "готовые выводы",
    "голос команды",
    "яркие комментарии",
    "последние комментарии",
    "на что обратить внимание",
    "темы по дням",
    "факторы:",
    "рекомендация:",
)


def _strip_html(text: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"</?blockquote>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    s = _EMOJI_RE.sub("", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _wrap_chunks(text: str, max_len: int = 105) -> list[str]:
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


def _classify_part(text: str) -> str:
    low = text.lower()
    if "готовые выводы" in low or "рекомендация:" in low:
        return "insights"
    if "голос команды" in low or "комментари" in low:
        return "comments"
    return "main"


def _parse_location_period(main: str) -> tuple[str, str, str]:
    lines = [ln.strip() for ln in main.split("\n") if ln.strip()]
    location = ""
    period = ""
    body_lines: list[str] = []
    for ln in lines:
        low = ln.lower()
        if ln.startswith("Сводка Pulse"):
            continue
        if low.startswith("период:"):
            period = ln.replace("Период:", "").replace("период:", "").strip()
            continue
        if not location and not ln.startswith("•") and "оценок" not in low and "отметок" not in low:
            if len(ln) < 80 and "средняя" not in low:
                location = ln
                continue
        body_lines.append(ln)
    return location, period, "\n".join(body_lines)


def _is_section_title(line: str) -> bool:
    if line.startswith("•") or line.startswith("🔸"):
        return False
    if re.match(r"^\d+\.\s", line):
        return True
    low = line.lower().rstrip(":")
    return any(h in low for h in _SECTION_HINTS) and len(line) < 90


def _is_insight_card_start(line: str) -> bool:
    return bool(re.match(r"^\d+\.\s", line))


class PulseReportPDF:
    def __init__(self) -> None:
        from fpdf import FPDF

        self.pdf = FPDF()
        self.pdf.set_margins(16, 14, 16)
        self.pdf.set_auto_page_break(auto=True, margin=18)
        self.pdf.add_page()
        self.pdf.add_font("DejaVu", "", str(FONT_PATH))
        self._font = "DejaVu"
        self._content_w = self.pdf.epw

    @property
    def epw(self) -> float:
        return self.pdf.epw

    def _set_color(self, rgb: tuple[int, int, int]) -> None:
        self.pdf.set_text_color(*rgb)

    def _write_wrapped(
        self, text: str, *, size: float = 10, color: tuple[int, int, int] = INK, lh: float = 5.2
    ) -> None:
        self.pdf.set_font(self._font, size=size)
        self._set_color(color)
        for chunk in _wrap_chunks(text):
            self.pdf.set_x(self.pdf.l_margin)
            self.pdf.multi_cell(self.epw, lh, chunk)

    def draw_header(self, location: str, period: str) -> None:
        p = self.pdf
        p.set_fill_color(*INK)
        p.rect(0, 0, p.w, 36, style="F")
        p.set_fill_color(*ACCENT)
        p.rect(0, 36, p.w, 2.5, style="F")

        p.set_xy(p.l_margin, 11)
        p.set_font(self._font, size=14)
        p.set_text_color(*WHITE)
        p.cell(0, 7, "Pulse Team", ln=True)
        p.set_x(p.l_margin)
        p.set_font(self._font, size=10)
        p.set_text_color(220, 220, 220)
        p.cell(0, 5, "Сводка для руководства", ln=True)

        p.set_y(42)
        if location:
            p.set_font(self._font, size=13)
            self._set_color(INK)
            p.set_x(p.l_margin)
            p.multi_cell(self.epw, 7, location)
        if period:
            p.set_font(self._font, size=9)
            self._set_color(MUTED)
            p.set_x(p.l_margin)
            p.multi_cell(self.epw, 5, f"Период: {period}")
        p.ln(4)

    def draw_section_title(self, title: str) -> None:
        p = self.pdf
        if p.get_y() > 255:
            p.add_page()
        y = p.get_y()
        p.set_fill_color(*ACCENT)
        p.rect(p.l_margin, y + 1, 3, 8, style="F")
        p.set_x(p.l_margin + 6)
        p.set_font(self._font, size=11)
        self._set_color(INK)
        clean = title.rstrip(":")
        p.multi_cell(self.epw - 6, 6, clean)
        p.ln(2)

    def draw_bullet(self, text: str, *, muted: bool = False) -> None:
        p = self.pdf
        p.set_font(self._font, size=9.5)
        self._set_color(MUTED if muted else INK)
        bullet = "—  "
        for i, chunk in enumerate(_wrap_chunks(text, max_len=98)):
            p.set_x(p.l_margin + (0 if i == 0 else 6))
            prefix = bullet if i == 0 else ""
            p.multi_cell(self.epw - (6 if i else 0), 5, prefix + chunk)
        p.ln(0.5)

    def draw_metric_block(self, text: str) -> None:
        p = self.pdf
        y = p.get_y()
        if y > 250:
            p.add_page()
            y = p.get_y()
        p.set_font(self._font, size=10)
        self._set_color(INK)
        p.set_x(p.l_margin + 4)
        p.set_fill_color(*BG_WARM)
        p.set_draw_color(*LINE)
        lines = _wrap_chunks(text, max_len=95)
        h = max(12, 4 + len(lines) * 5.5)
        p.rect(p.l_margin, y, self.epw, h, style="DF")
        p.set_xy(p.l_margin + 4, y + 4)
        for ln in lines:
            p.multi_cell(self.epw - 8, 5.5, ln)
        p.set_y(y + h + 3)

    def draw_insight_card(self, title: str, body_lines: list[str]) -> None:
        p = self.pdf
        y = p.get_y()
        if y > 230:
            p.add_page()
            y = p.get_y()
        p.set_fill_color(*WHITE)
        p.set_draw_color(*LINE)
        start_y = y
        p.set_xy(p.l_margin + 4, y + 4)
        p.set_font(self._font, size=10)
        self._set_color(INK)
        p.multi_cell(self.epw - 8, 5.5, title)
        y_after = p.get_y()
        for ln in body_lines:
            p.set_x(p.l_margin + 4)
            low = ln.lower()
            if low.startswith("рекомендация:"):
                p.set_font(self._font, size=9)
                self._set_color(CALM)
                p.multi_cell(self.epw - 8, 5, "Рекомендация")
                ln = ln.split(":", 1)[-1].strip()
            elif low.startswith("факторы:"):
                p.set_font(self._font, size=9)
                self._set_color(MUTED)
                p.multi_cell(self.epw - 8, 5, "Факторы")
                continue
            p.set_font(self._font, size=9.5)
            self._set_color(MUTED if ln.startswith("—") or ln.startswith("•") else INK)
            p.multi_cell(self.epw - 8, 5, ln)
        end_y = p.get_y() + 3
        p.set_draw_color(*LINE)
        p.rect(p.l_margin, start_y, self.epw, end_y - start_y, style="D")
        p.set_y(end_y + 2)

    def draw_body_text(self, text: str, *, muted: bool = False) -> None:
        self._write_wrapped(text, size=9.5, color=MUTED if muted else INK, lh=5)

    def ln(self, h: float = 0) -> None:
        self.pdf.ln(h)

    def draw_footer(self) -> None:
        p = self.pdf
        p.set_y(-14)
        p.set_font(self._font, size=8)
        self._set_color(MUTED)
        p.set_x(p.l_margin)
        p.cell(0, 4, "Pulse Team · pulseteam.online · конфиденциально", align="C")

    def render_main(self, body: str) -> None:
        lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
        i = 0
        while i < len(lines):
            ln = lines[i]
            low = ln.lower()
            if _is_section_title(ln):
                self.draw_section_title(ln)
                i += 1
                continue
            if ln.startswith("•") or ln.startswith("🔸"):
                self.draw_bullet(ln.lstrip("•🔸 ").strip())
                i += 1
                continue
            if "средняя оценка" in low or "оценок смен" in low or "отметок смены" in low:
                block = [ln]
                while i + 1 < len(lines) and not _is_section_title(lines[i + 1]):
                    nxt = lines[i + 1]
                    if nxt.startswith("•"):
                        break
                    if "средняя" in nxt.lower() or "динамика" in nxt.lower() or "оценок" in nxt.lower():
                        block.append(nxt)
                        i += 1
                    else:
                        break
                self.draw_metric_block(" · ".join(block))
                i += 1
                continue
            if ln.startswith("«") or "ещё" in low and "коммент" in low:
                self.draw_bullet(ln, muted=True)
                i += 1
                continue
            self.draw_body_text(ln, muted=ln.startswith("(") or "нет" in low[:20])
            i += 1

    def render_insights(self, text: str) -> None:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            return
        self.draw_section_title("Готовые выводы")
        card_title = ""
        card_body: list[str] = []
        for ln in lines:
            if "готовые выводы" in ln.lower():
                continue
            if _is_insight_card_start(ln):
                if card_title:
                    self.draw_insight_card(card_title, card_body)
                card_title = ln
                card_body = []
                continue
            if card_title:
                card_body.append(ln)
        if card_title:
            self.draw_insight_card(card_title, card_body)

    def render_comments(self, text: str) -> None:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            return
        title = lines[0]
        if "коммент" in title.lower() or "голос" in title.lower():
            self.draw_section_title(title.replace("(анонимно)", "").replace("(выжимка, анонимно)", "").strip())
            lines = lines[1:]
        for ln in lines:
            if ln.startswith("•") or ln.startswith("«"):
                self.draw_bullet(ln.lstrip("• ").strip(), muted=True)
            else:
                self.draw_body_text(ln, muted=True)


def build_pdf_bytes(
    parts: list[str],
    *,
    title: str = "Pulse Team — сводка",
    period_label: str = "",
) -> bytes:
    try:
        from fpdf import FPDF  # noqa: F401
    except ImportError as e:
        raise RuntimeError("fpdf2_missing") from e

    if not FONT_PATH.exists():
        raise RuntimeError("font_missing")

    classified: dict[str, str] = {"main": "", "insights": "", "comments": ""}
    for part in parts:
        plain = _strip_html(part)
        if not plain:
            continue
        kind = _classify_part(plain)
        if classified[kind]:
            classified[kind] += "\n\n" + plain
        else:
            classified[kind] = plain

    main_text = classified["main"] or _strip_html("\n\n".join(parts))
    location, period, body = _parse_location_period(main_text)
    if not location and title:
        location = title.replace("Pulse Team — ", "").strip()

    doc = PulseReportPDF()
    doc.draw_header(location, period or period_label)
    doc.render_main(body)
    if classified["insights"]:
        doc.ln(2)
        doc.render_insights(classified["insights"])
    if classified["comments"]:
        doc.ln(2)
        doc.render_comments(classified["comments"])
    doc.draw_footer()

    buf = BytesIO()
    doc.pdf.output(buf)
    return buf.getvalue()


def pdf_filename(scope_title: str, period_label: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "", scope_title, flags=re.UNICODE)
    safe = re.sub(r"\s+", "_", safe.strip())[:40] or "report"
    period = re.sub(r"[^\w\-]", "_", period_label)[:20]
    return f"Pulse_{safe}_{period}.pdf"
