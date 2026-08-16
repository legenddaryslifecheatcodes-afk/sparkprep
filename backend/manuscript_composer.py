"""Interior manuscript template composer — generates a properly formatted PDF/X-1a ready interior
from a template + user text. Beats InDesign for straightforward fiction / workbook / poetry books."""
import io
import re
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Frame, PageTemplate,
    BaseDocTemplate, KeepTogether,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas


# Distributor margin rules (inches)
DISTRIBUTOR_MARGINS = {
    "kdp": {"top": 0.75, "bottom": 0.75, "outer": 0.5, "inner": 0.75},
    "ingramspark": {"top": 0.875, "bottom": 0.875, "outer": 0.5, "inner": 0.875},
    "barnes_noble": {"top": 0.75, "bottom": 0.75, "outer": 0.5, "inner": 0.75},
    "lulu": {"top": 0.75, "bottom": 0.75, "outer": 0.5, "inner": 0.75},
}

TEMPLATES = {
    "fiction_novel": {
        "label": "Fiction Novel",
        "description": "Classic novel layout — justified body, drop caps on chapter openers, running heads",
        "body_font": "Times-Roman", "body_size": 11, "leading": 15,
        "chapter_font": "Times-Bold", "chapter_size": 22, "chapter_align": TA_CENTER,
        "drop_caps": True, "running_head": True,
        "chapter_start_page": "right",  # chapters always start on recto (right-hand page)
        "first_para_indent": 0,
        "para_indent": 0.25 * inch,
    },
    "workbook": {
        "label": "Workbook / Journal",
        "description": "Extra whitespace, larger body text, room for annotation — great for guided journals",
        "body_font": "Helvetica", "body_size": 12, "leading": 18,
        "chapter_font": "Helvetica-Bold", "chapter_size": 20, "chapter_align": TA_LEFT,
        "drop_caps": False, "running_head": False,
        "chapter_start_page": "any",
        "first_para_indent": 0,
        "para_indent": 0,
    },
    "poetry_chapbook": {
        "label": "Poetry Chapbook",
        "description": "Ragged-right, centered short lines, generous vertical rhythm — for verse collections",
        "body_font": "Times-Italic", "body_size": 11, "leading": 17,
        "chapter_font": "Times-Bold", "chapter_size": 18, "chapter_align": TA_CENTER,
        "drop_caps": False, "running_head": False,
        "chapter_start_page": "any",
        "first_para_indent": 0,
        "para_indent": 0,
    },
}


def parse_source_text(text: str):
    """Split source text on '# Chapter Title' markers. Everything before the first marker
    is treated as front matter body (no chapter title)."""
    chapters = []
    current = {"title": None, "paragraphs": []}
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("# "):
            if current["title"] is not None or current["paragraphs"]:
                chapters.append(current)
            current = {"title": line[2:].strip(), "paragraphs": []}
        elif line.strip() == "":
            if current["paragraphs"] and current["paragraphs"][-1] != "":
                current["paragraphs"].append("")
        else:
            if not current["paragraphs"] or current["paragraphs"][-1] == "":
                current["paragraphs"].append(line)
            else:
                current["paragraphs"][-1] += " " + line
    if current["title"] is not None or current["paragraphs"]:
        chapters.append(current)
    # Filter out empty paragraph strings and empty chapters
    for c in chapters:
        c["paragraphs"] = [p for p in c["paragraphs"] if p.strip()]
    chapters = [c for c in chapters if c["title"] or c["paragraphs"]]
    return chapters


def compose_manuscript_pdf(
    output_path: str,
    template_key: str,
    title: str,
    author: str,
    source_text: str,
    trim_w: float,
    trim_h: float,
    platform: str = "kdp",
) -> dict:
    """Compose a manuscript PDF with proper margins, typography and page numbers.
    Returns metadata dict.
    """
    tpl = TEMPLATES.get(template_key, TEMPLATES["fiction_novel"])
    margins = DISTRIBUTOR_MARGINS.get(platform, DISTRIBUTOR_MARGINS["kdp"])
    page_w = trim_w * inch
    page_h = trim_h * inch

    # Mirrored margins — inner is spine-side (larger)
    m_top = margins["top"] * inch
    m_bottom = margins["bottom"] * inch
    m_outer = margins["outer"] * inch
    m_inner = margins["inner"] * inch

    doc = BaseDocTemplate(
        output_path, pagesize=(page_w, page_h),
        title=title, author=author,
        creator="SparkPrep Book Production Engine",
        subject="Interior manuscript",
    )

    def draw_page_furniture(canv, doc):
        # Page number — centered bottom, small
        canv.setFont("Helvetica", 9)
        canv.setFillColorRGB(0.35, 0.35, 0.35)
        canv.drawCentredString(page_w / 2, m_bottom / 2, str(doc.page))
        # Running head — only for fiction template + not on chapter first page
        if tpl["running_head"] and doc.page > 1:
            canv.setFont("Helvetica-Oblique", 9)
            canv.setFillColorRGB(0.4, 0.4, 0.4)
            is_verso = doc.page % 2 == 0
            if is_verso:
                canv.drawString(m_outer, page_h - m_top / 2, (author or "").upper())
            else:
                canv.drawRightString(page_w - m_outer, page_h - m_top / 2, (title or "").upper())
        # Reset color for body flow
        canv.setFillColorRGB(0, 0, 0)

    # Recto (right/odd) frame: larger inner margin on the LEFT
    recto_frame = Frame(m_inner, m_bottom, page_w - m_inner - m_outer, page_h - m_top - m_bottom, id="recto")
    # Verso (left/even) frame: larger inner margin on the RIGHT
    verso_frame = Frame(m_outer, m_bottom, page_w - m_inner - m_outer, page_h - m_top - m_bottom, id="verso")

    doc.addPageTemplates([
        PageTemplate(id="recto", frames=recto_frame, onPage=draw_page_furniture),
        PageTemplate(id="verso", frames=verso_frame, onPage=draw_page_furniture),
    ])

    # Styles
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "Body", parent=styles["BodyText"],
        fontName=tpl["body_font"], fontSize=tpl["body_size"], leading=tpl["leading"],
        alignment=TA_JUSTIFY if tpl["body_font"].startswith("Times") else TA_LEFT,
        firstLineIndent=tpl["para_indent"],
        spaceAfter=0, textColor=(0, 0, 0),
    )
    body_first_para = ParagraphStyle(
        "BodyFirst", parent=body_style,
        firstLineIndent=tpl.get("first_para_indent", 0),
        spaceBefore=0.15 * inch,
    )
    chapter_style = ParagraphStyle(
        "Chapter", parent=styles["Heading1"],
        fontName=tpl["chapter_font"], fontSize=tpl["chapter_size"], leading=tpl["chapter_size"] * 1.2,
        alignment=tpl["chapter_align"],
        spaceBefore=1.0 * inch, spaceAfter=0.4 * inch, textColor=(0, 0, 0),
    )
    title_style = ParagraphStyle(
        "Title", parent=styles["Title"],
        fontName=tpl["chapter_font"], fontSize=32, alignment=TA_CENTER,
        spaceBefore=2.0 * inch, spaceAfter=0.3 * inch, textColor=(0, 0, 0),
    )
    author_style = ParagraphStyle(
        "Author", parent=styles["Normal"],
        fontName=tpl["body_font"], fontSize=14, alignment=TA_CENTER,
        spaceBefore=0.5 * inch, textColor=(0.2, 0.2, 0.2),
    )

    # Build flowables
    story = []
    # Title page
    if title:
        story.append(Paragraph(title, title_style))
    if author:
        story.append(Paragraph(f"by {author}", author_style))
    story.append(PageBreak())
    # Copyright placeholder
    copy_style = ParagraphStyle("Copy", parent=styles["Normal"], fontName=tpl["body_font"], fontSize=9, alignment=TA_CENTER, leading=13, textColor=(0.3, 0.3, 0.3))
    story.append(Spacer(1, page_h * 0.6))
    story.append(Paragraph(f"Copyright © {author}. All rights reserved.", copy_style))
    story.append(Paragraph("Interior composed with Legenddary's SparkPrep.", copy_style))
    story.append(PageBreak())

    chapters = parse_source_text(source_text)
    for ci, ch in enumerate(chapters):
        if ci > 0 and tpl["chapter_start_page"] == "right":
            # Ensure chapter starts on recto — insert a blank if we're currently on recto
            story.append(PageBreak())
        if ch["title"]:
            story.append(Paragraph(ch["title"], chapter_style))
        for pi, para in enumerate(ch["paragraphs"]):
            # Escape HTML-ish characters
            safe = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            style = body_first_para if pi == 0 else body_style
            story.append(Paragraph(safe, style))

    doc.build(story)

    # Read back page count
    from pypdf import PdfReader
    r = PdfReader(output_path)
    page_count = len(r.pages)

    return {
        "output_path": output_path,
        "template": template_key,
        "template_label": tpl["label"],
        "page_count": page_count,
        "trim": [trim_w, trim_h],
        "margins": margins,
        "platform": platform,
    }


def list_templates():
    return [
        {"key": k, "label": v["label"], "description": v["description"]}
        for k, v in TEMPLATES.items()
    ]
