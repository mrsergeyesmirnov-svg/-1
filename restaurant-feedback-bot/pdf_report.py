"""
PDF-сводка для руководства — дашборд в стиле one-pager / demo Pulse Team.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from io import BytesIO
from pathlib import Path

FONT_PATH = Path(__file__).resolve().parent / "assets" / "DejaVuSans.ttf"

# one-pager palette
INK = (28, 25, 23)
MUTED = (87, 83, 78)
ACCENT = (255, 90, 31)
ACCENT_DARK = (229, 77, 21)
CALM = (13, 148, 136)
CALM_LIGHT = (45, 212, 191)
LINE = (231, 226, 220)
BG_WARM = (250, 247, 243)
SURFACE = (255, 255, 255)
INSIGHT_BG = (255, 238, 228)
INSIGHT_BG_ALT = (232, 245, 243)
WHITE = (255, 255, 255)
HERO_DARK = (28, 25, 23)
HERO_MID = (41, 37, 36)
RED_SOFT = (254, 226, 226)

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FEFF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_STAT_PATTERNS = (
    (re.compile(r"средн(?:яя|\.)\s*оценка[^0-9]*([0-9]+[.,][0-9]+)", re.I), "rating"),
    (re.compile(r"оценок\s+смен[^0-9]*([0-9]+)", re.I), "ratings"),
    (re.compile(r"отметок\s+смены[^0-9]*([0-9]+)", re.I), "marks"),
    (re.compile(r"комментар(?:иев|ии)[^0-9]*([0-9]+)", re.I), "comments"),
)

_SECTION_HINTS = (
    "что мешало",
    "что было сложным",
    "самочувствие",
    "на что обратить внимание",
    "темы по дням",
    "вовлечённость",
    "вовлеченность",
    "дисциплина",
    "уходы",
    "стоп-лист",
    "выручка",
    "план",
)


@dataclass
class ReportDashboard:
    location: str = ""
    period: str = ""
    subtitle: str = ""
    rating: str = ""
    rating_prev: str = ""
    trend_arrow: str = ""
    trend_text: str = ""
    ratings_count: str = ""
    comments_count: str = ""
    marks_count: str = ""
    kpi_extra: list[tuple[str, str]] = field(default_factory=list)
    problems: list[tuple[str, str]] = field(default_factory=list)
    problems_title: str = "Что мешало на смене"
    personal: list[tuple[str, str]] = field(default_factory=list)
    personal_title: str = "Самочувствие и нагрузка"
    highlights: list[str] = field(default_factory=list)
    weekday_themes: list[str] = field(default_factory=list)
    misc_sections: list[tuple[str, list[str]]] = field(default_factory=list)


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


def _wrap_chunks(text: str, max_len: int = 88) -> list[str]:
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


def _is_insights_message(plain: str) -> bool:
    low = plain.lower()
    return "готовые выводы" in low or "🧠" in plain


def _is_comments_message(plain: str) -> bool:
    low = plain.lower()
    return (
        "голос команды" in low
        or "яркие комментарии" in low
        or "последние комментарии" in low
    )


def _parse_bullet_count(line: str) -> tuple[str, str] | None:
    m = re.match(r"^[•🔸\-—]\s*(.+?)\s*[—–-]\s*(\d+)\s*$", line.strip())
    if m:
        return m.group(1).strip(), m.group(2)
    m2 = re.match(r"^[•🔸\-—]\s*(.+?)\s+(\d+)\s*$", line.strip())
    if m2:
        return m2.group(1).strip(), m2.group(2)
    return None


def _is_section_title(line: str) -> bool:
    if line.startswith("•") or line.startswith("🔸"):
        return False
    low = line.lower().rstrip(":")
    if any(skip in low for skip in ("готовые выводы", "голос команды", "яркие комментарии")):
        return True
    return any(h in low for h in _SECTION_HINTS) and len(line) < 100


def _parse_location_period(main: str) -> tuple[str, str, str]:
    lines = [ln.strip() for ln in main.split("\n") if ln.strip()]
    location = ""
    period = ""
    subtitle = ""
    body_lines: list[str] = []
    for ln in lines:
        low = ln.lower()
        if "сводка pulse" in low or "отчёт за календарный месяц" in low:
            continue
        if low.startswith("период:"):
            period = ln.split(":", 1)[-1].strip()
            continue
        if not location and not ln.startswith("•"):
            if (
                len(ln) < 90
                and "средняя" not in low
                and "оценок" not in low
                and "отметок" not in low
                and not re.match(r"^\d+\.\d+", ln)
                and "динамика" not in low
            ):
                if "·" in ln and not period:
                    subtitle = ln
                    continue
                location = ln
                continue
        body_lines.append(ln)
    return location, period or subtitle, "\n".join(body_lines)


def _parse_dashboard(body: str) -> ReportDashboard:
    dash = ReportDashboard()
    low_body = body.lower()

    for rx, key in _STAT_PATTERNS:
        m = rx.search(low_body)
        if not m:
            continue
        val = m.group(1).replace(",", ".")
        if key == "rating":
            dash.rating = val
        elif key == "ratings":
            dash.ratings_count = val
        elif key == "marks":
            dash.marks_count = val
        elif key == "comments":
            dash.comments_count = val

    m_prev = re.search(r"было\s+([0-9]+[.,][0-9]+)", body, re.I)
    if m_prev:
        dash.rating_prev = m_prev.group(1).replace(",", ".")

    m_arr = re.search(r"динамика:\s*([↑↓→])", body, re.I)
    if m_arr:
        dash.trend_arrow = m_arr.group(1)
    m_trend = re.search(r"динамика:\s*[↑↓→]\s*(.+)$", body, re.I | re.M)
    if m_trend:
        dash.trend_text = m_trend.group(1).strip()

    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    current_section = ""
    section_bullets: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        low = ln.lower()

        if any(rx.search(low) for rx, _ in _STAT_PATTERNS):
            i += 1
            continue
        if low.startswith("динамика:"):
            i += 1
            continue

        if _is_section_title(ln):
            if current_section and section_bullets:
                dash.misc_sections.append((current_section, section_bullets))
            section_bullets = []
            clean = ln.rstrip(":")
            if "что мешало" in low or "что было сложным" in low:
                dash.problems_title = clean
                current_section = "__problems__"
            elif "самочувствие" in low:
                dash.personal_title = clean
                current_section = "__personal__"
            elif "темы по дням" in low:
                current_section = "__weekday__"
            else:
                current_section = clean
            i += 1
            continue

        bullet = _parse_bullet_count(ln.lstrip("•🔸 ").strip()) or _parse_bullet_count(ln)
        if bullet:
            label, cnt = bullet
            if current_section == "__problems__":
                dash.problems.append((label, cnt))
            elif current_section == "__personal__":
                dash.personal.append((label, cnt))
            elif current_section == "__weekday__":
                dash.weekday_themes.append(f"{label} — {cnt}")
            else:
                section_bullets.append(f"{label} — {cnt}")
            i += 1
            continue

        if ln.startswith("•") or ln.startswith("🔸"):
            text = ln.lstrip("•🔸 ").strip()
            if current_section == "__weekday__":
                dash.weekday_themes.append(text)
            elif current_section == "__problems__":
                dash.problems.append((text, ""))
            elif current_section == "__personal__":
                dash.personal.append((text, ""))
            else:
                section_bullets.append(text)
            i += 1
            continue

        if current_section == "__weekday__":
            dash.weekday_themes.append(ln)
        elif "вовлеч" in low or "дисциплин" in low or "уход" in low:
            dash.highlights.append(ln)
        elif current_section and current_section not in ("__problems__", "__personal__"):
            section_bullets.append(ln)
        elif not dash.rating and "нет оценок" not in low:
            if len(ln) < 120 and not ln.startswith("("):
                dash.highlights.append(ln)
        i += 1

    if current_section and section_bullets and current_section not in (
        "__problems__",
        "__personal__",
        "__weekday__",
    ):
        dash.misc_sections.append((current_section, section_bullets))

    return dash


class PulseReportPDF:
    def __init__(self) -> None:
        from fpdf import FPDF

        self.pdf = FPDF()
        self.pdf.set_margins(14, 12, 14)
        self.pdf.set_auto_page_break(auto=True, margin=16)
        self.pdf.add_page()
        self.pdf.add_font("DejaVu", "", str(FONT_PATH))
        self._font = "DejaVu"

    @property
    def epw(self) -> float:
        return self.pdf.epw

    def _set_color(self, rgb: tuple[int, int, int]) -> None:
        self.pdf.set_text_color(*rgb)

    def _fill_page_bg(self) -> None:
        p = self.pdf
        p.set_fill_color(*BG_WARM)
        p.rect(0, 0, p.w, p.h, style="F")

    def _check_page(self, need: float) -> None:
        if self.pdf.get_y() + need > self.pdf.h - 18:
            self.pdf.add_page()
            self._fill_page_bg()

    def draw_hero(self, location: str, period: str, *, eyebrow: str = "Сводка для руководства") -> None:
        p = self.pdf
        self._fill_page_bg()
        hero_h = 44
        p.set_fill_color(*HERO_DARK)
        p.rect(0, 0, p.w, hero_h, style="F")
        p.set_fill_color(*HERO_MID)
        p.rect(0, hero_h - 8, p.w, 8, style="F")
        p.set_fill_color(*ACCENT)
        p.rect(0, hero_h, p.w, 2.2, style="F")

        p.set_xy(p.l_margin, 10)
        p.set_font(self._font, size=7)
        p.set_text_color(180, 175, 170)
        p.cell(0, 4, eyebrow.upper(), ln=True)

        p.set_x(p.l_margin)
        p.set_font(self._font, size=15)
        p.set_text_color(*WHITE)
        title = location or "Pulse Team"
        p.multi_cell(self.epw, 7, title)

        if period:
            badge_y = max(p.get_y() + 2, 30)
            p.set_fill_color(60, 55, 52)
            p.set_draw_color(90, 85, 80)
            p.set_xy(p.l_margin, badge_y)
            p.set_font(self._font, size=8.5)
            p.set_text_color(230, 228, 225)
            badge_w = min(self.epw, 4 + p.get_string_width(f"  {period}  "))
            p.rect(p.l_margin, badge_y, badge_w, 7, style="DF")
            p.set_xy(p.l_margin + 2, badge_y + 1.5)
            p.cell(badge_w - 4, 4, period)

        p.set_y(hero_h + 8)

    def draw_kpi_dashboard(self, dash: ReportDashboard) -> None:
        p = self.pdf
        cards: list[tuple[str, str, str, tuple[int, int, int]]] = []

        if dash.rating:
            sub = ""
            if dash.rating_prev:
                sub = f"было {dash.rating_prev}"
            if dash.trend_arrow:
                sub = (sub + f"  {dash.trend_arrow}").strip()
            cards.append(("Средняя оценка", f"{dash.rating}", sub or "за период", ACCENT))
        elif dash.marks_count:
            cards.append(("Отметок смены", dash.marks_count, "кухня", ACCENT))

        if dash.ratings_count:
            cards.append(("Оценок смен", dash.ratings_count, "зал", INK))
        if dash.comments_count:
            cards.append(("Комментариев", dash.comments_count, "текстовых", INK))
        if dash.marks_count and dash.rating:
            cards.append(("Отметок кухни", dash.marks_count, "смена", CALM))

        if not cards:
            return

        self._check_page(30)
        y = p.get_y()
        n = min(len(cards), 4)
        gap = 3
        card_w = (self.epw - gap * (n - 1)) / n
        card_h = 26

        for i, (label, value, sub, color) in enumerate(cards[:4]):
            x = p.l_margin + i * (card_w + gap)
            p.set_fill_color(*SURFACE)
            p.set_draw_color(*LINE)
            p.rect(x, y, card_w, card_h, style="DF")
            if i == 0:
                p.set_fill_color(*color)
                p.rect(x, y, card_w, 1.8, style="F")

            p.set_xy(x + 4, y + 4)
            p.set_font(self._font, size=7)
            self._set_color(MUTED)
            p.cell(card_w - 8, 3.5, label)

            p.set_xy(x + 4, y + 10)
            p.set_font(self._font, size=16)
            self._set_color(color if i == 0 else INK)
            p.cell(card_w - 8, 8, value)

            if sub:
                p.set_xy(x + 4, y + 19)
                p.set_font(self._font, size=6.5)
                self._set_color(MUTED)
                p.cell(card_w - 8, 3, sub[:28])

        p.set_y(y + card_h + 6)

        if dash.trend_text:
            p.set_fill_color(*SURFACE)
            p.set_draw_color(*LINE)
            ty = p.get_y()
            p.rect(p.l_margin, ty, self.epw, 9, style="DF")
            p.set_xy(p.l_margin + 4, ty + 2.5)
            p.set_font(self._font, size=8.5)
            arrow = dash.trend_arrow or "→"
            color = CALM if arrow == "↑" else (ACCENT if arrow == "↓" else MUTED)
            self._set_color(color)
            p.cell(0, 4, f"Динамика {arrow}  {dash.trend_text}")
            p.set_y(ty + 11)

    def draw_eyebrow(self, title: str) -> None:
        self._check_page(12)
        p = self.pdf
        p.set_font(self._font, size=7)
        self._set_color(MUTED)
        p.set_x(p.l_margin)
        p.cell(0, 4, title.upper())
        p.ln(5)

    def draw_panel(
        self,
        title: str,
        *,
        accent: tuple[int, int, int] = ACCENT,
        bg: tuple[int, int, int] = SURFACE,
    ) -> float:
        """Начало карточки-секции. Возвращает Y начала контента."""
        self._check_page(20)
        p = self.pdf
        y = p.get_y()
        p.set_fill_color(*bg)
        p.set_draw_color(*LINE)
        p.rect(p.l_margin, y, self.epw, 6, style="F")
        p.set_fill_color(*accent)
        p.rect(p.l_margin, y, 3.5, 6, style="F")
        p.set_xy(p.l_margin + 6, y + 1.2)
        p.set_font(self._font, size=10)
        self._set_color(INK)
        p.cell(0, 4, title)
        return y + 8

    def draw_bar_chart(
        self,
        title: str,
        items: list[tuple[str, str]],
        *,
        accent: tuple[int, int, int] = ACCENT,
    ) -> None:
        if not items:
            return
        p = self.pdf
        counts: list[int] = []
        for _, c in items:
            try:
                counts.append(int(c))
            except (TypeError, ValueError):
                counts.append(1)
        max_val = max(counts) if counts else 1
        bar_max_w = self.epw - 52
        row_h = 7.5
        needed = 8 + len(items) * (row_h + 1.5) + 4
        self._check_page(needed)

        start_y = self.draw_panel(title, accent=accent)
        p.set_y(start_y)

        for (label, cnt_s), cnt in zip(items, counts):
            y = p.get_y()
            p.set_font(self._font, size=8)
            self._set_color(INK)
            short = label if len(label) <= 32 else label[:29] + "…"
            p.set_xy(p.l_margin + 4, y)
            p.cell(38, 4, short)

            bar_w = max(4, bar_max_w * (cnt / max_val))
            p.set_fill_color(*accent)
            p.rect(p.l_margin + 42, y + 0.5, bar_w, 4, style="F")

            p.set_xy(p.l_margin + 42 + bar_max_w + 2, y)
            p.set_font(self._font, size=8)
            self._set_color(INK)
            p.cell(8, 4, str(cnt), align="R")
            p.set_y(y + row_h)

        p.set_y(p.get_y() + 4)

    def draw_bullet_panel(
        self,
        title: str,
        items: list[tuple[str, str]],
        *,
        accent: tuple[int, int, int] = CALM,
        bg: tuple[int, int, int] = INSIGHT_BG_ALT,
    ) -> None:
        if not items:
            return
        needed = 10 + len(items) * 6 + 6
        self._check_page(needed)
        start_y = self.draw_panel(title, accent=accent, bg=bg)
        p = self.pdf
        p.set_y(start_y)
        row_h = 6
        for label, cnt in items:
            y = p.get_y()
            p.set_font(self._font, size=8.5)
            self._set_color(INK)
            p.set_xy(p.l_margin + 6, y)
            line = f"  {label}"
            if cnt:
                line += f"  —  {cnt}"
            p.multi_cell(self.epw - 10, 4.5, line)
            p.set_y(max(p.get_y(), y + row_h) + 0.5)
        p.ln(3)

    def draw_highlight_strip(self, lines: list[str]) -> None:
        if not lines:
            return
        p = self.pdf
        needed = 8 + len(lines) * 5 + 4
        self._check_page(needed)
        y = p.get_y()
        p.set_fill_color(*SURFACE)
        p.set_draw_color(*LINE)
        h = 6 + len(lines) * 5
        p.rect(p.l_margin, y, self.epw, h, style="DF")
        p.set_xy(p.l_margin + 5, y + 3)
        for ln in lines[:6]:
            p.set_font(self._font, size=8.5)
            self._set_color(INK)
            p.set_x(p.l_margin + 5)
            p.multi_cell(self.epw - 10, 4.5, ln)
        p.set_y(y + h + 4)

    def draw_two_column_panels(
        self,
        left_title: str,
        left_items: list[tuple[str, str]],
        right_title: str,
        right_items: list[tuple[str, str]],
    ) -> None:
        if not left_items and not right_items:
            return
        if not left_items or not right_items:
            if left_items:
                self.draw_bar_chart(left_title, left_items)
            if right_items:
                self.draw_bullet_panel(right_title, right_items)
            return

        self._check_page(45)
        p = self.pdf
        y0 = p.get_y()
        col_w = (self.epw - 4) / 2
        gap = 4

        def _col(x: float, title: str, items: list[tuple[str, str]], accent: tuple[int, int, int]) -> float:
            p.set_fill_color(*SURFACE)
            p.set_draw_color(*LINE)
            max_cnt = max((int(c) for _, c in items if c.isdigit()), default=1)
            h = 12 + len(items) * 7
            p.rect(x, y0, col_w, h, style="DF")
            p.set_fill_color(*accent)
            p.rect(x, y0, 3, h, style="F")
            p.set_xy(x + 5, y0 + 3)
            p.set_font(self._font, size=9)
            self._set_color(INK)
            p.cell(col_w - 8, 4, title)
            cy = y0 + 10
            bar_w = col_w - 28
            for label, cnt_s in items[:6]:
                try:
                    cnt = int(cnt_s)
                except (TypeError, ValueError):
                    cnt = 0
                p.set_xy(x + 5, cy)
                p.set_font(self._font, size=7.5)
                self._set_color(INK)
                short = label[:22] + "…" if len(label) > 22 else label
                p.cell(col_w - 10, 3.5, short)
                if cnt:
                    bw = max(3, bar_w * cnt / max_cnt)
                    p.set_fill_color(*accent)
                    p.rect(x + 5, cy + 4, bw, 2.5, style="F")
                    p.set_xy(x + col_w - 12, cy)
                    p.set_font(self._font, size=7.5)
                    self._set_color(MUTED)
                    p.cell(8, 3.5, str(cnt), align="R")
                cy += 7
            return h

        h1 = _col(p.l_margin, left_title, left_items, ACCENT)
        h2 = _col(p.l_margin + col_w + gap, right_title, right_items, CALM)
        p.set_y(y0 + max(h1, h2) + 5)

    def draw_weekday_panel(self, themes: list[str]) -> None:
        if not themes:
            return
        self.draw_bullet_panel(
            "Темы по дням недели",
            [(t, "") for t in themes],
            accent=(120, 113, 108),
            bg=SURFACE,
        )

    def draw_misc_section(self, title: str, bullets: list[str]) -> None:
        if not bullets:
            return
        items = []
        for b in bullets:
            parsed = _parse_bullet_count(b)
            items.append(parsed if parsed else (b, ""))
        self.draw_bullet_panel(title, items, accent=(120, 113, 108), bg=SURFACE)

    def _block_height(
        self, title: str, lines: list[str], *, title_w: int = 82, body_w: int = 82
    ) -> float:
        h = 5 * max(1, len(_wrap_chunks(title, title_w)))
        for ln in lines:
            low = ln.lower()
            if low.startswith("факторы:"):
                h += 4
                continue
            if low.startswith("рекомендация:"):
                h += 4
                ln = ln.split(":", 1)[-1].strip()
            h += 4.5 * max(1, len(_wrap_chunks(ln, body_w)))
        return h + 10

    def draw_insight_card(self, title: str, body_lines: list[str], *, alt: bool = False) -> None:
        p = self.pdf
        bg = INSIGHT_BG_ALT if alt else INSIGHT_BG
        border = CALM if alt else ACCENT
        pad = 5
        text_x = p.l_margin + 7
        text_w = self.epw - 12

        est_h = self._block_height(title, body_lines, title_w=88, body_w=82)
        self._check_page(est_h + 4)
        start_y = p.get_y()

        p.set_fill_color(*bg)
        p.set_draw_color(*border)
        p.rect(p.l_margin, start_y, self.epw, est_h, style="DF")
        p.set_fill_color(*border)
        p.rect(p.l_margin, start_y, 3.5, est_h, style="F")

        p.set_xy(text_x, start_y + pad)
        p.set_font(self._font, size=9.5)
        self._set_color(INK)
        p.multi_cell(text_w, 5, title)

        for ln in body_lines:
            p.set_x(text_x)
            low = ln.lower()
            if low.startswith("рекомендация:"):
                p.set_font(self._font, size=7.5)
                self._set_color(border)
                p.multi_cell(text_w, 4, "РЕКОМЕНДАЦИЯ")
                ln = ln.split(":", 1)[-1].strip()
                p.set_x(text_x)
                p.set_font(self._font, size=8.5)
                self._set_color(INK)
                p.multi_cell(text_w, 4.5, ln)
                continue
            if low.startswith("факторы:"):
                p.set_font(self._font, size=7.5)
                self._set_color(MUTED)
                p.multi_cell(text_w, 4, "Факторы")
                continue
            p.set_font(self._font, size=8.5)
            self._set_color(MUTED if ln.startswith("•") else INK)
            p.multi_cell(text_w, 4.5, ln)

        end_y = p.get_y() + pad
        if end_y > start_y + est_h:
            extra = end_y - (start_y + est_h)
            p.set_fill_color(*bg)
            p.rect(p.l_margin, start_y + est_h, self.epw, extra, style="F")
            p.set_fill_color(*border)
            p.rect(p.l_margin, start_y, 3.5, end_y - start_y, style="F")
            p.set_draw_color(*border)
            p.rect(p.l_margin, start_y, self.epw, end_y - start_y, style="D")
        p.set_y(end_y + 4)

    def render_insights(self, text: str) -> None:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            return
        self.draw_eyebrow("Готовые выводы")
        card_title = ""
        card_body: list[str] = []
        idx = 0
        for ln in lines:
            if "готовые выводы" in ln.lower() or ln.startswith("Период:"):
                continue
            if re.match(r"^\d+\.\s", ln):
                if card_title:
                    self.draw_insight_card(card_title, card_body, alt=idx % 2 == 1)
                    idx += 1
                card_title = ln
                card_body = []
                continue
            if card_title:
                card_body.append(ln)
        if card_title:
            self.draw_insight_card(card_title, card_body, alt=idx % 2 == 1)

    def render_comments(self, text: str) -> None:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            return
        self.draw_eyebrow("Яркие комментарии")
        for ln in lines:
            if "коммент" in ln.lower() and "«" not in ln:
                continue
            if "ещё" in ln.lower() and "коммент" in ln.lower():
                p = self.pdf
                p.set_font(self._font, size=7.5)
                self._set_color(MUTED)
                p.set_x(p.l_margin)
                p.multi_cell(self.epw, 4, ln)
                continue
            quote = ln.lstrip("• ").strip()
            if not quote:
                continue
            self._check_page(18)
            p = self.pdf
            y = p.get_y()
            p.set_fill_color(*SURFACE)
            p.set_draw_color(*LINE)
            est = 10 + len(_wrap_chunks(quote, max_len=90)) * 5
            p.rect(p.l_margin, y, self.epw, est, style="DF")
            p.set_fill_color(*ACCENT)
            p.rect(p.l_margin, y, 2.5, est, style="F")
            p.set_xy(p.l_margin + 6, y + 4)
            p.set_font(self._font, size=9)
            self._set_color(INK)
            p.multi_cell(self.epw - 10, 5, f"«{quote.strip('«»')}»")
            p.set_y(y + est + 3)

    def render_dashboard(self, dash: ReportDashboard) -> None:
        self.draw_kpi_dashboard(dash)
        if dash.highlights:
            self.draw_highlight_strip(dash.highlights)

        self.draw_two_column_panels(
            dash.problems_title,
            dash.problems,
            dash.personal_title,
            dash.personal,
        )

        if dash.weekday_themes:
            self.draw_weekday_panel(dash.weekday_themes)

        for title, bullets in dash.misc_sections:
            self.draw_misc_section(title, bullets)

    def draw_footer(self) -> None:
        p = self.pdf
        p.set_y(-14)
        p.set_draw_color(*LINE)
        p.line(p.l_margin, p.get_y() - 2, p.w - p.r_margin, p.get_y() - 2)
        p.set_font(self._font, size=7.5)
        self._set_color(MUTED)
        p.set_x(p.l_margin)
        p.cell(0, 4, "Pulse Team  ·  pulseteam.online  ·  конфиденциально", align="C")

    def ln(self, h: float = 0) -> None:
        self.pdf.ln(h)


def build_pdf_bytes(
    messages: list[str],
    *,
    title: str = "Pulse Team — сводка",
    period_label: str = "",
    comments_html: str = "",
) -> bytes:
    try:
        from fpdf import FPDF  # noqa: F401
    except ImportError as e:
        raise RuntimeError("fpdf2_missing") from e

    if not FONT_PATH.exists():
        raise RuntimeError("font_missing")

    main_chunks: list[str] = []
    insights_text = ""
    for msg in messages:
        plain = _strip_html(msg)
        if not plain:
            continue
        if _is_insights_message(plain):
            insights_text = plain
            continue
        if _is_comments_message(plain):
            continue
        main_chunks.append(plain)

    main_text = "\n\n".join(main_chunks)
    if not main_text and messages:
        main_text = _strip_html(messages[0])

    location, period, body = _parse_location_period(main_text)
    if not location and title:
        location = title.replace("Pulse Team — ", "").strip()

    dash = _parse_dashboard(body)
    if not dash.location:
        dash.location = location
    if not dash.period:
        dash.period = period or period_label

    comments_plain = _strip_html(comments_html) if comments_html else ""

    doc = PulseReportPDF()
    doc.draw_hero(location, dash.period or period_label)
    doc.render_dashboard(dash)
    if insights_text:
        doc.ln(4)
        doc.render_insights(insights_text)
    if comments_plain:
        doc.ln(4)
        doc.render_comments(comments_plain)
    doc.draw_footer()

    buf = BytesIO()
    doc.pdf.output(buf)
    return buf.getvalue()


def pdf_filename(scope_title: str, period_label: str) -> str:
    safe = re.sub(r"[^\w\s\-]", "", scope_title, flags=re.UNICODE)
    safe = re.sub(r"\s+", "_", safe.strip())[:40] or "report"
    period = re.sub(r"[^\w\-]", "_", period_label)[:20]
    return f"Pulse_{safe}_{period}.pdf"
