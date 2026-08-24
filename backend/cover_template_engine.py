"""Cover Design Template library -- a starter set of typographic full-wrap
cover layouts (title/author placement + color palette + accent marks), no
external stock art or font files required. Renders a real vector PDF
using ReportLab's built-in base-14 fonts, geometrically correct for the
project's actual trim/spine/bleed (via print_specs.calculate_full_cover_dimensions),
which is then dropped into the project's full_wrap slot the same way an
uploaded file would be -- it goes through the same compliance checks and
Auto-Fix pipeline as any other cover, and the author is expected to
replace/adjust it, not ship it verbatim.
"""
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

from print_specs import calculate_full_cover_dimensions

COVER_TEMPLATES = {
    "thriller_bold": {
        "label": "Thriller Bold",
        "genre_tags": ["thriller", "mystery", "crime"],
        "bg_color": "#0D0D0D",
        "title_color": "#FFFFFF",
        "author_color": "#C9C9C9",
        "accent_color": "#D0333A",
        "title_font": "Helvetica-Bold",
        "author_font": "Helvetica",
        "style": "bold_rule",
    },
    "romance_soft": {
        "label": "Romance Soft",
        "genre_tags": ["romance", "women's fiction"],
        "bg_color": "#F3E4E1",
        "title_color": "#7A2E2E",
        "author_color": "#5C4A4A",
        "accent_color": "#C9A15A",
        "title_font": "Times-BoldItalic",
        "author_font": "Times-Italic",
        "style": "thin_rule",
    },
    "fantasy_dark": {
        "label": "Fantasy Dark",
        "genre_tags": ["fantasy", "sci-fi", "young adult"],
        "bg_color": "#1B1035",
        "title_color": "#D4AF37",
        "author_color": "#B7A8D6",
        "accent_color": "#D4AF37",
        "title_font": "Times-Bold",
        "author_font": "Times-Roman",
        "style": "corner_marks",
    },
    "nonfiction_clean": {
        "label": "Nonfiction Clean",
        "genre_tags": ["nonfiction", "business", "self-help"],
        "bg_color": "#FFFFFF",
        "title_color": "#111111",
        "author_color": "#444444",
        "accent_color": "#1F5FBF",
        "title_font": "Helvetica-Bold",
        "author_font": "Helvetica",
        "style": "edge_bar",
    },
    "literary_minimal": {
        "label": "Literary Minimal",
        "genre_tags": ["literary fiction", "poetry"],
        "bg_color": "#F7F5F0",
        "title_color": "#1A1A1A",
        "author_color": "#666666",
        "accent_color": "#1A1A1A",
        "title_font": "Times-Roman",
        "author_font": "Times-Italic",
        "style": "minimal",
    },
    "memoir_warm": {
        "label": "Memoir Warm",
        "genre_tags": ["memoir", "biography"],
        "bg_color": "#C97B4A",
        "title_color": "#FFF7EE",
        "author_color": "#FBE6D3",
        "accent_color": "#FFF7EE",
        "title_font": "Times-BoldItalic",
        "author_font": "Times-Italic",
        "style": "minimal",
    },
}


def _hex_to_rgb01(hex_color: str):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _wrap_title(title: str, max_chars_per_line: int = 16):
    """Very small greedy word-wrap for the title block -- good enough for
    typical book titles (a handful of words) at cover-scale font sizes."""
    words = title.split()
    lines, current = [], ""
    for w in words:
        candidate = f"{current} {w}".strip()
        if len(candidate) > max_chars_per_line and current:
            lines.append(current)
            current = w
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [title]


def render_cover_template(
    output_path: str,
    template_key: str,
    title: str,
    author: str,
    trim_w: float,
    trim_h: float,
    spine_w: float,
    bleed: float,
    binding: str = "paperback",
) -> dict:
    tpl = COVER_TEMPLATES.get(template_key, COVER_TEMPLATES["literary_minimal"])
    full = calculate_full_cover_dimensions(trim_w, trim_h, spine_w, bleed, binding)
    total_w_pt = full["total_width"] * inch
    total_h_pt = full["total_height"] * inch
    front_x_pt = full["front_x"] * inch
    back_x_pt = full["back_x"] * inch
    spine_x_pt = full["spine_x"] * inch
    trim_w_pt = trim_w * inch
    spine_w_pt = spine_w * inch

    c = canvas.Canvas(output_path, pagesize=(total_w_pt, total_h_pt))
    bg = _hex_to_rgb01(tpl["bg_color"])
    accent = _hex_to_rgb01(tpl["accent_color"])
    title_color = _hex_to_rgb01(tpl["title_color"])
    author_color = _hex_to_rgb01(tpl["author_color"])

    # Full-bleed background
    c.setFillColorRGB(*bg)
    c.rect(0, 0, total_w_pt, total_h_pt, fill=1, stroke=0)

    style = tpl["style"]
    if style == "edge_bar":
        c.setFillColorRGB(*accent)
        c.rect(front_x_pt, 0, 0.25 * inch, total_h_pt, fill=1, stroke=0)
    elif style == "corner_marks":
        c.setStrokeColorRGB(*accent)
        c.setLineWidth(1.2)
        m = 0.4 * inch
        L = 0.6 * inch
        for cx, cy, dx, dy in [
            (front_x_pt + m, total_h_pt - m, 1, -1),
            (front_x_pt + trim_w_pt - m, total_h_pt - m, -1, -1),
            (front_x_pt + m, m, 1, 1),
            (front_x_pt + trim_w_pt - m, m, -1, 1),
        ]:
            c.line(cx, cy, cx + dx * L, cy)
            c.line(cx, cy, cx, cy + dy * L)

    # Front cover title block (right-most panel)
    title_lines = _wrap_title(title.upper() if style == "bold_rule" else title)
    line_h = 0.55 * inch
    block_h = line_h * len(title_lines)
    title_top = total_h_pt * 0.62
    c.setFillColorRGB(*title_color)
    for i, line in enumerate(title_lines):
        size = 34 if len(title_lines) <= 2 else 26
        c.setFont(tpl["title_font"], size)
        y = title_top - i * line_h
        c.drawCentredString(front_x_pt + trim_w_pt / 2, y, line)

    if style in ("bold_rule", "thin_rule"):
        c.setStrokeColorRGB(*accent)
        c.setLineWidth(2 if style == "bold_rule" else 1)
        rule_y = title_top - block_h - 0.25 * inch
        c.line(front_x_pt + trim_w_pt * 0.25, rule_y, front_x_pt + trim_w_pt * 0.75, rule_y)

    if author:
        c.setFillColorRGB(*author_color)
        c.setFont(tpl["author_font"], 16)
        c.drawCentredString(front_x_pt + trim_w_pt / 2, total_h_pt * 0.18, author.upper() if style == "bold_rule" else author)

    # Spine text (only if there's enough width to read it)
    if spine_w >= 0.18:
        c.saveState()
        c.translate(spine_x_pt + spine_w_pt / 2, total_h_pt / 2)
        c.rotate(90)
        c.setFillColorRGB(*title_color)
        c.setFont(tpl["title_font"], min(14, max(8, spine_w * 40)))
        c.drawCentredString(0, -3, title[:40])
        c.restoreState()

    # Back cover: a light echo of the accent so the wrap doesn't look unfinished
    if style == "edge_bar":
        c.setFillColorRGB(*accent)
        c.rect(back_x_pt, 0, 0.08 * inch, total_h_pt, fill=1, stroke=0)

    c.showPage()
    c.save()

    return {
        "output_path": output_path,
        "template": template_key,
        "template_label": tpl["label"],
        "page_size_inches": [round(full["total_width"], 4), round(full["total_height"], 4)],
    }


def list_cover_templates():
    return [
        {"key": k, "label": v["label"], "genre_tags": v["genre_tags"], "bg_color": v["bg_color"], "accent_color": v["accent_color"]}
        for k, v in COVER_TEMPLATES.items()
    ]
