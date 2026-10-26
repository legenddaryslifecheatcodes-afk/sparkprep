"""Interior Studio: typesets a book's inside pages from the author's own writing.

The author writes (or imports a Word file) in a Word-style editor in the browser. The editor sends its document
(TipTap/ProseMirror JSON) here with a book style and the book's setup, and this module lays it out as a real
print interior: mirrored margins, embedded open-licensed fonts, hyphenated justified text with no widows or orphans,
chapters that open on a right-hand page, running heads, page numbers, title/copyright/dedication pages and a table
of contents, all in black ink only (no rich black).

Margins are never below the chosen distributor's OWN published minimum for the book's page count (owner rule: each
distributor uses only its own numbers), and never below the safe margin SparkPrep's own preflight checks, so a
book typeset here passes the margin check it will be put through next.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import CMYKColor
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, PageBreak, PageTemplate, Paragraph, Spacer, TableStyle
from reportlab.platypus.doctemplate import DocIf
from reportlab.platypus.flowables import Flowable, ImageAndFlowables
from reportlab.platypus.tableofcontents import TableOfContents

FONT_DIR = Path(__file__).resolve().parent / "fonts" / "interior"
BLACK = CMYKColor(0, 0, 0, 1)
GREY = CMYKColor(0, 0, 0, 0.75)   # running heads: black ink only, never a mix of four inks

# ---------------------------------------------------------------------------------------------------------------
# Margins. Each distributor's own numbers, from its own help pages (checked 2026-10-26):
#   KDP  "Set Trim Size, Bleed, and Margins" (kdp.amazon.com/en_US/help/topic/GVBQ3CMEQW3W2VL6): inside margin by
#        page count 24-150: 0.375, 151-300: 0.5, 301-500: 0.625, 501-700: 0.75, 701-828: 0.875; outside >= 0.25
#        without bleed.
#   IngramSpark "File Requirements for Print Books" (ingramspark.com/blog/file-requirements-for-print-books):
#        "a minimum of 0.5 inches (13mm) from final trim size on all sides", page numbers included. No gutter figure.
#   Lulu "What is a safety margin?" 0.5 in from the trim edge; "What is a gutter?" recommended interior (inside)
#        margin by page count: up to 60: 0.5, 61-150: 0.625, 151-400: 1.0, 401-600: 1.125, over 600: 1.25.
#   Barnes & Noble Press "Book Interior File Preparation Settings" quick guide: top 0.5, bottom 0.5, outside 0.5,
#        inside 0.75, mirror margins.
# On top of those, SparkPrep's own preflight safe margin (print_specs.PLATFORMS[...]["safe_margin_interior"]) is
# applied to every side, so a typeset book never trips the check it goes through next.
# ---------------------------------------------------------------------------------------------------------------
_KDP_INSIDE = ((150, 0.375), (300, 0.5), (500, 0.625), (700, 0.75), (10 ** 6, 0.875))
_LULU_INSIDE = ((60, 0.5), (150, 0.625), (400, 1.0), (600, 1.125), (10 ** 6, 1.25))

MARGIN_SOURCES = {
    "kdp": "Amazon KDP's margin table (inside margin grows with page count)",
    "ingramspark": "IngramSpark's 0.5\" minimum on every side",
    "lulu": "Lulu's safety margin and its recommended gutter for your page count",
    "barnes_noble": "Barnes & Noble Press's interior margin settings",
}


def _by_pages(table, pages):
    for upto, v in table:
        if pages <= upto:
            return v
    return table[-1][1]


def distributor_min_margins(platform: str, page_count: int, safe_margin: float) -> dict:
    """The smallest margins (inches) the distributor allows for this page count, never below `safe_margin`."""
    if platform == "kdp":
        m = {"top": 0.25, "bottom": 0.25, "outside": 0.25, "inside": _by_pages(_KDP_INSIDE, page_count)}
    elif platform == "lulu":
        m = {"top": 0.5, "bottom": 0.5, "outside": 0.5, "inside": _by_pages(_LULU_INSIDE, page_count)}
    elif platform == "barnes_noble":
        m = {"top": 0.5, "bottom": 0.5, "outside": 0.5, "inside": 0.75}
    else:  # ingramspark
        m = {"top": 0.5, "bottom": 0.5, "outside": 0.5, "inside": 0.5}
    return {k: max(v, safe_margin) for k, v in m.items()}


# ---------------------------------------------------------------------------------------------------------------
# Fonts (all SIL Open Font License, embedded in the PDF). family key -> file prefix
# ---------------------------------------------------------------------------------------------------------------
FONT_FAMILIES = {
    "EB Garamond": "EBGaramond", "Libre Baskerville": "LibreBaskerville", "Crimson Pro": "CrimsonPro",
    "Lora": "Lora", "Merriweather": "Merriweather", "Source Serif 4": "SourceSerif4",
    "Cormorant Garamond": "CormorantGaramond", "Lato": "Lato", "Playfair Display": "PlayfairDisplay",
    "Atkinson Hyperlegible": "AtkinsonHyperlegible", "Cinzel": "Cinzel",
}
_registered: set = set()


def _font(family: str, bold=False, italic=False) -> str:
    """Registers (once) and returns the reportlab font name for one face of a family."""
    prefix = FONT_FAMILIES[family]
    _register_family(prefix)
    face = ("Bold" if bold else "") + ("Italic" if italic else "")
    name = f"{prefix}-{face or 'Regular'}"
    return name if name in _registered else f"{prefix}-Regular"


def _register_family(prefix: str):
    if prefix in _registered:
        return
    faces = {}
    for face in ("Regular", "Italic", "Bold", "BoldItalic", "Black"):
        f = FONT_DIR / f"{prefix}-{face}.ttf"
        if f.exists():
            name = f"{prefix}-{face}"
            pdfmetrics.registerFont(TTFont(name, str(f)))
            _registered.add(name)
            faces[face] = name
    reg = faces["Regular"]
    pdfmetrics.registerFontFamily(prefix, normal=reg, bold=faces.get("Bold", reg), italic=faces.get("Italic", reg),
                                  boldItalic=faces.get("BoldItalic", faces.get("Bold", reg)))
    _registered.add(prefix)


# ---------------------------------------------------------------------------------------------------------------
# Book styles. Sizes in points, margins in inches (the design margins; distributor minimums still win).
# ---------------------------------------------------------------------------------------------------------------
_BASE = {
    "body_font": "EB Garamond", "body_size": 11.5, "leading": 15.5, "align": "justify", "indent": 1.5,  # em
    "para_space": 0, "heading_font": "EB Garamond", "label_font": "EB Garamond", "label_size": 11,
    "label_upper": True, "label_tracking": 2.0, "numbering": "words", "title_size": 22, "title_italic": False,
    "title_bold": False, "title_upper": False, "title_align": "center", "sink": 0.22, "ornament": "",
    "drop_cap": True, "drop_lines": 3, "recto_chapters": True, "running_heads": True, "head_upper": True,
    "head_size": 8.5, "folio": "bottom", "scene_break": "*   *   *", "sub_size": 13, "sub_bold": True,
    "poetry": False, "margins": {"top": 0.8, "bottom": 0.75, "outside": 0.625, "inside": 0.8},
}


def _style(**over):
    s = dict(_BASE)
    s.update(over)
    return s


BOOK_STYLES = {
    "classic_novel": _style(label="Classic Novel", genre="Fiction",
                            blurb="Timeless Garamond, drop caps, chapters on the right-hand page.",
                            ornament="❧", scene_break="❧"),
    "modern_fiction": _style(label="Modern Fiction", genre="Fiction",
                             blurb="Clean Crimson Pro text with bold sans chapter numbers.",
                             body_font="Crimson Pro", body_size=12, leading=16, heading_font="Crimson Pro",
                             label_font="Lato", label_size=30, label_upper=False, label_tracking=0, numbering="numerals",
                             title_size=16, title_italic=True, drop_cap=False, scene_break="•   •   •"),
    "literary": _style(label="Literary", genre="Fiction",
                       blurb="Elegant Libre Baskerville, quiet small headings, generous space.",
                       body_font="Libre Baskerville", body_size=10, leading=15.5, heading_font="Libre Baskerville",
                       label_font="Libre Baskerville", label_size=9.5, title_size=17, title_italic=True,
                       drop_cap=False, sink=0.3),
    "romance": _style(label="Romance", genre="Fiction",
                      blurb="Warm Lora text with flowing Playfair italic chapter titles.",
                      body_font="Lora", body_size=10.5, leading=15, heading_font="Playfair Display",
                      label_font="Playfair Display", label_size=11, title_size=24, title_italic=True,
                      scene_break="~   ~   ~"),
    "thriller": _style(label="Thriller & Mystery", genre="Fiction",
                       blurb="Sharp Source Serif text, heavy black chapter numbers, fast-moving pages.",
                       body_font="Source Serif 4", body_size=10.5, leading=14.5, heading_font="Lato",
                       label_font="Lato", label_size=40, label_upper=False, label_tracking=0, numbering="numerals",
                       title_size=13, title_upper=True, title_bold=True, drop_cap=False, sink=0.18,
                       scene_break="■"),
    "fantasy": _style(label="Fantasy & Epic", genre="Fiction",
                      blurb="Garamond text with carved Cinzel chapter titles and ornaments.",
                      heading_font="Cinzel", label_font="Cinzel", label_size=12, title_size=20, ornament="❧",
                      scene_break="❧"),
    "nonfiction": _style(label="Nonfiction & Business", genre="Nonfiction",
                         blurb="Readable Merriweather text, bold sans headings, left-aligned openers.",
                         body_font="Merriweather", body_size=9.5, leading=14.5, heading_font="Lato",
                         label_font="Lato", label_size=10, numbering="numerals", title_size=22, title_bold=True,
                         title_align="left", drop_cap=False, recto_chapters=False, scene_break="",
                         sub_size=12.5),
    "memoir": _style(label="Memoir", genre="Nonfiction",
                     blurb="Personal and warm: Lora text with graceful Cormorant chapter titles.",
                     body_font="Lora", body_size=10.5, leading=15, heading_font="Cormorant Garamond",
                     label_font="Cormorant Garamond", label_size=12, title_size=26, title_italic=True),
    "workbook": _style(label="Workbook & Journal", genre="Nonfiction",
                       blurb="Friendly Lato, roomy spacing, blocks of text instead of indents.",
                       body_font="Lato", body_size=11.5, leading=18, align="left", indent=0, para_space=8,
                       heading_font="Lato", label_font="Lato", label_size=10, numbering="numerals", title_size=24,
                       title_bold=True, title_align="left", drop_cap=False, recto_chapters=False,
                       running_heads=False, scene_break="", sub_size=14,
                       margins={"top": 0.75, "bottom": 0.75, "outside": 0.75, "inside": 0.875}),
    "large_print": _style(label="Large Print", genre="Accessible",
                          blurb="16 pt Atkinson Hyperlegible, made for low-vision readers.",
                          body_font="Atkinson Hyperlegible", body_size=16, leading=22, align="left",
                          heading_font="Atkinson Hyperlegible", label_font="Atkinson Hyperlegible", label_size=14,
                          numbering="numerals", title_size=26, title_bold=True, drop_cap=False, head_size=11,
                          scene_break="*   *   *"),
    "poetry": _style(label="Poetry", genre="Poetry",
                     blurb="Your line breaks kept exactly, ragged right, each poem on its own page.",
                     align="left", indent=0, para_space=11.5, numbering="none", title_size=16, title_italic=True,
                     drop_cap=False, recto_chapters=False, running_heads=False, poetry=True, sink=0.12,
                     scene_break="*"),
}


def list_styles():
    return [{"key": k, "label": v["label"], "genre": v["genre"], "blurb": v["blurb"], "body_font": v["body_font"],
             "heading_font": v["heading_font"], "body_size": v["body_size"]} for k, v in BOOK_STYLES.items()]


# ---------------------------------------------------------------------------------------------------------------
# Document parsing (TipTap JSON -> blocks)
# ---------------------------------------------------------------------------------------------------------------
_NAMED_HEADING = re.compile(
    r"^(prologue|epilogue|introduction|preface|foreword|afterword|interlude|acknowledg\w*|about the author|"
    r"author'?s note|dedication|contents|glossary|appendix\b.*|part\b.*|book\b.*)$", re.I)
_CHAPTER_PREFIX = re.compile(r"^chapter\s+([0-9]+|[a-z\-]+)\s*[:.\-–—]?\s*(.*)$", re.I)
_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def number_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")
    return str(n)


def _inline(nodes, poetry=False) -> str:
    """TipTap inline content -> reportlab paragraph markup."""
    out = []
    for n in nodes or []:
        t = n.get("type")
        if t == "hardBreak":
            out.append("<br/>")
        elif t == "text":
            s = escape(n.get("text", ""))
            if poetry:   # keep the poet's spacing
                s = re.sub(r"  +", lambda m: "&nbsp;" * len(m.group()), s)
            for mark in n.get("marks") or []:
                mt = mark.get("type")
                if mt == "bold":
                    s = f"<b>{s}</b>"
                elif mt == "italic":
                    s = f"<i>{s}</i>"
                elif mt == "underline":
                    s = f"<u>{s}</u>"
                elif mt == "strike":
                    s = f"<strike>{s}</strike>"
                elif mt == "superscript":
                    s = f"<super>{s}</super>"
                elif mt == "subscript":
                    s = f"<sub>{s}</sub>"
            out.append(s)
    return "".join(out)


def _plain(nodes) -> str:
    return "".join(n.get("text", "") for n in nodes or [] if n.get("type") == "text").strip()


def parse_document(doc: dict, poetry=False) -> list:
    """Flattens the editor document into blocks: chapter, sub, para, quote, bullet, scene, pagebreak."""
    blocks = []

    def walk(nodes, quote=False, list_kind=None):
        for n in nodes or []:
            t = n.get("type")
            attrs = n.get("attrs") or {}
            if t == "heading":
                text = _plain(n.get("content"))
                if not text:
                    continue
                if attrs.get("level", 1) == 1:
                    blocks.append({"t": "chapter", "text": text})
                else:
                    blocks.append({"t": "sub", "markup": _inline(n.get("content")), "level": attrs.get("level", 2)})
            elif t == "paragraph":
                markup = _inline(n.get("content"), poetry)
                if not markup.strip():
                    if poetry and blocks and blocks[-1]["t"] == "para":   # an empty line in a poem = stanza gap
                        blocks.append({"t": "gap"})
                    continue
                kind = "quote" if quote else ("bullet" if list_kind else "para")
                blocks.append({"t": kind, "markup": markup, "align": attrs.get("textAlign"),
                               "list": list_kind, "plain": _plain(n.get("content"))})
            elif t == "blockquote":
                walk(n.get("content"), quote=True)
            elif t in ("bulletList", "orderedList"):
                for i, item in enumerate(n.get("content") or []):
                    before = len(blocks)
                    walk(item.get("content"), quote, "ordered" if t == "orderedList" else "bullet")
                    if len(blocks) > before and blocks[before]["t"] == "bullet":
                        blocks[before]["marker"] = f"{(attrs.get('start') or 1) + i}." if t == "orderedList" else "•"
            elif t == "horizontalRule":
                blocks.append({"t": "scene"})
            elif t == "pageBreak":
                blocks.append({"t": "pagebreak"})
            elif n.get("content"):
                walk(n.get("content"), quote, list_kind)

    walk((doc or {}).get("content"))
    return blocks


# ---------------------------------------------------------------------------------------------------------------
# Flowables
# ---------------------------------------------------------------------------------------------------------------
class _Mark(Flowable):
    """Zero-size marker: records the page it lands on into doc.<attr> (a set), or sets doc.<attr> = value."""
    def __init__(self, attr, value=None):
        super().__init__()
        self.attr, self.value = attr, value

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        doc = self.canv._doctemplate
        if self.value is None:
            getattr(doc, self.attr).add(doc.page)
        else:
            setattr(doc, self.attr, self.value)


class _DropCap(Flowable):
    """A true drop cap: the first letter sunk across `lines` lines of the paragraph that wraps around it."""
    def __init__(self, letter, font, size, leading, lines):
        super().__init__()
        self.letter, self.font, self.lines, self.leading, self.body_size = letter, font, lines, leading, size
        cap_h = (lines - 1) * leading + size * 0.7           # top of first line's capitals to last baseline
        face = pdfmetrics.getFont(font).face
        self.cap_ratio = (getattr(face, "capHeight", 700) or 700) / 1000.0
        self.size = cap_h / self.cap_ratio
        self.width = pdfmetrics.stringWidth(letter, font, self.size)
        self.height = (lines - 1) * leading + size

    def wrap(self, aw, ah):
        return self.width, self.height

    def _restrictSize(self, aw, ah):   # ImageAndFlowables asks its "image" for this
        return self.width, self.height

    def draw(self):
        # Baseline of the drop cap = baseline of the paragraph's last sunk line. Paragraph puts its first baseline
        # one font size below its top, then one leading per line.
        self.canv.setFillColor(BLACK)
        self.canv.setFont(self.font, self.size)
        self.canv.drawString(0, self.height - self.body_size - (self.lines - 1) * self.leading, self.letter)


# ---------------------------------------------------------------------------------------------------------------
# Typesetting
# ---------------------------------------------------------------------------------------------------------------
_ALIGN = {"left": TA_LEFT, "center": TA_CENTER, "right": TA_RIGHT, "justify": TA_JUSTIFY}


def _hyphen_lang():
    try:
        import pyphen  # noqa: F401
        return "en_US"
    except ImportError:
        return None


def _missing_chars(blocks, details, font_names) -> str:
    text = " ".join(b.get("plain") or b.get("text") or "" for b in blocks) + " ".join(
        str(v) for v in details.values() if isinstance(v, str))
    faces = [pdfmetrics.getFont(f).face for f in font_names]
    missing = sorted({c for c in set(text) if not c.isspace() and ord(c) >= 32
                      and all(ord(c) not in face.charToGlyph for face in faces)})
    return "".join(missing[:20])


def compose(output, payload: dict, platform_safe_margin: float = 0.5) -> dict:
    """Typesets the book into `output` (path or file-like). payload: {style, setup{platform, trim_w, trim_h},
    details{title, subtitle, author, copyright, dedication, isbn, toc}, content (TipTap JSON)}.
    The inside margin depends on the page count, so the book is laid out again whenever the count moves it into
    another band of the distributor's table (at most a few passes)."""
    pages_guess = int(payload.get("page_hint") or 100)
    result = None
    for _ in range(4):
        buf = io.BytesIO()
        result = _compose_once(buf, payload, platform_safe_margin, pages_guess)
        if result["min_margins_for"] == result["page_count"] or _band_same(payload, pages_guess, result["page_count"],
                                                                           platform_safe_margin):
            break
        pages_guess = result["page_count"]
    data = buf.getvalue()
    if hasattr(output, "write"):
        output.write(data)
    else:
        Path(output).write_bytes(data)
    return result


def _band_same(payload, a, b, safe):
    plat = (payload.get("setup") or {}).get("platform", "kdp")
    return distributor_min_margins(plat, a, safe) == distributor_min_margins(plat, b, safe)


def _compose_once(buf, payload: dict, safe_margin: float, page_guess: int) -> dict:
    st = BOOK_STYLES.get(payload.get("style") or "classic_novel", BOOK_STYLES["classic_novel"])
    setup = payload.get("setup") or {}
    platform = setup.get("platform", "kdp")
    trim_w, trim_h = float(setup.get("trim_w", 6)), float(setup.get("trim_h", 9))
    details = {k: (v.strip() if isinstance(v, str) else v) for k, v in (payload.get("details") or {}).items()}
    title = details.get("title") or "Untitled"
    author = details.get("author") or ""
    blocks = parse_document(payload.get("content") or {}, poetry=st["poetry"])

    body_r = _font(st["body_font"])
    body_i = _font(st["body_font"], italic=True)
    head_r = _font(st["heading_font"])
    head_b = _font(st["heading_font"], bold=True)
    head_i = _font(st["heading_font"], italic=True)
    head_bi = _font(st["heading_font"], bold=True, italic=True)
    label_f = _font(st["label_font"], bold=st["label_size"] >= 24)
    if st["label_font"] == "Lato" and st["label_size"] >= 30:
        _register_family("Lato")
        label_f = "Lato-Black"
    title_f = {(False, False): head_r, (True, False): head_b, (False, True): head_i, (True, True): head_bi}[
        (st["title_bold"], st["title_italic"])]
    body_family = FONT_FAMILIES[st["body_font"]]

    # -- margins: design margins, raised to the distributor's minimums for this page count --------------------
    mins = distributor_min_margins(platform, page_guess, safe_margin)
    design = st["margins"]
    head_pt = st["head_size"]
    tol = 0.0625                                      # IngramSpark's stated 1/16" print variance, used for all
    head_base_from_top = mins["top"] + tol + head_pt / 72.0          # running head sits inside the safe area
    folio_base_from_bottom = mins["bottom"] + tol + 0.02
    m = {
        "top": max(design["top"], mins["top"], head_base_from_top + 0.22 if st["running_heads"] or st["folio"] == "top" else 0),
        "bottom": max(design["bottom"], mins["bottom"], folio_base_from_bottom + head_pt / 72.0 + 0.2),
        "outside": max(design["outside"], mins["outside"]),
        "inside": max(design["inside"], mins["inside"]),
    }
    if trim_w <= 5.25:   # small trims: keep the text column usable
        m["outside"] = max(mins["outside"], min(m["outside"], 0.55))
    pw, ph = trim_w * inch, trim_h * inch
    fw = pw - (m["inside"] + m["outside"]) * inch
    fh = ph - (m["top"] + m["bottom"]) * inch

    # -- styles --------------------------------------------------------------------------------------------------
    hy = _hyphen_lang() if st["align"] == "justify" else None
    em = st["body_size"]
    body = ParagraphStyle("Body", fontName=body_r, fontSize=st["body_size"], leading=st["leading"],
                          alignment=_ALIGN[st["align"]], firstLineIndent=st["indent"] * em,
                          spaceAfter=st["para_space"], textColor=BLACK, allowWidows=0, allowOrphans=0,
                          hyphenationLang=hy, embeddedHyphenation=1 if hy else 0, uriWasteReduce=0.3 if hy else 0)
    body_first = ParagraphStyle("BodyFirst", parent=body, firstLineIndent=0)
    quote = ParagraphStyle("Quote", parent=body, firstLineIndent=0, leftIndent=1.6 * em, rightIndent=1.6 * em,
                           spaceBefore=st["leading"] * 0.5, spaceAfter=st["leading"] * 0.5,
                           fontSize=st["body_size"] * 0.95, leading=st["leading"] * 0.95)
    bullet = ParagraphStyle("Bullet", parent=body, firstLineIndent=0, leftIndent=1.6 * em, bulletIndent=0.5 * em,
                            alignment=TA_LEFT, spaceAfter=max(2, st["para_space"] / 2), bulletFontName=body_r)
    sub = ParagraphStyle("Sub", fontName=head_b if st["sub_bold"] else head_i, fontSize=st["sub_size"],
                         leading=st["sub_size"] * 1.3, spaceBefore=st["leading"] * 1.1, spaceAfter=st["leading"] * 0.45,
                         keepWithNext=1, textColor=BLACK, alignment=TA_LEFT)
    label = ParagraphStyle("Label", fontName=label_f, fontSize=st["label_size"], leading=st["label_size"] * 1.25,
                           alignment=_ALIGN[st["title_align"]], textColor=BLACK, spaceAfter=st["label_size"] * 0.5,
                           keepWithNext=1)
    chapter = ParagraphStyle("Chapter", fontName=title_f, fontSize=st["title_size"], leading=st["title_size"] * 1.2,
                             alignment=_ALIGN[st["title_align"]], textColor=BLACK, keepWithNext=1,
                             spaceAfter=st["leading"] * 2.2)
    ornament = ParagraphStyle("Ornament", fontName=body_r, fontSize=st["body_size"] * 1.3,
                              leading=st["body_size"] * 1.6, alignment=TA_CENTER, textColor=BLACK,
                              spaceBefore=-st["leading"] * 1.4, spaceAfter=st["leading"] * 1.6, keepWithNext=1)
    scene = ParagraphStyle("Scene", parent=ornament, spaceBefore=st["leading"] * 0.6, spaceAfter=st["leading"] * 0.6,
                           keepWithNext=0)
    center = lambda name, font, size, **kw: ParagraphStyle(name, fontName=font, fontSize=size, leading=size * 1.25,
                                                            alignment=TA_CENTER, textColor=BLACK, **kw)
    t_title = center("TTitle", title_f, min(34, st["title_size"] * 1.6))
    t_sub = center("TSub", head_i, st["title_size"] * 0.7, spaceBefore=10)
    t_author = center("TAuthor", head_r, st["title_size"] * 0.75)
    copy_style = ParagraphStyle("Copy", fontName=body_r, fontSize=8.5, leading=12, textColor=BLACK, alignment=TA_LEFT)
    dedic = center("Dedication", body_i, st["body_size"] * 1.05)

    # -- the document --------------------------------------------------------------------------------------------
    doc = BaseDocTemplate(buf, pagesize=(pw, ph), title=title, author=author,
                          creator="SparkPrep Interior Studio", subject="Book interior", enforceColorSpace="cmyk",
                          leftMargin=0, rightMargin=0, topMargin=0, bottomMargin=0,
                          initialFontName=body_r, initialFontSize=st["body_size"])   # no stray unembedded Helvetica
    doc.blank_pages, doc.opener_pages, doc.front_pages = set(), set(), set()
    doc.body_started = False
    doc.chapter_title = ""

    def furniture(canv, d):
        page = d.page
        if page in d.blank_pages or not d.body_started or page in d.front_pages:
            return
        recto = page % 2 == 1
        canv.saveState()
        canv.setFillColor(GREY)
        head_font = body_r
        y_head = ph - head_base_from_top * inch
        outer_x = pw - m["outside"] * inch if recto else m["outside"] * inch
        inner_x = m["inside"] * inch if recto else pw - m["inside"] * inch
        mid = (outer_x + inner_x) / 2
        if st["running_heads"] and page not in d.opener_pages:
            text = (title if recto else (author or title))
            if st["head_upper"]:
                text = text.upper()
            canv.setFont(head_font, head_pt)
            tracked = " ".join(text) if st["head_upper"] and len(text) < 40 else text
            if pdfmetrics.stringWidth(tracked, head_font, head_pt) > fw * 0.7:
                tracked = text
            canv.drawCentredString(mid, y_head, tracked)
        if st["folio"] == "top" and page not in d.opener_pages:
            canv.setFont(head_font, head_pt + 0.5)
            (canv.drawRightString if recto else canv.drawString)(outer_x, y_head, str(page))
        else:
            canv.setFont(head_font, head_pt + 0.5)
            canv.drawCentredString(mid, folio_base_from_bottom * inch, str(page))
        canv.restoreState()

    recto_frame = Frame(m["inside"] * inch, m["bottom"] * inch, fw, fh, id="r", leftPadding=0, rightPadding=0,
                        topPadding=0, bottomPadding=0)
    verso_frame = Frame(m["outside"] * inch, m["bottom"] * inch, fw, fh, id="v", leftPadding=0, rightPadding=0,
                        topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(id="recto", frames=[recto_frame], onPageEnd=furniture, autoNextPageTemplate="verso"),
        PageTemplate(id="verso", frames=[verso_frame], onPageEnd=furniture, autoNextPageTemplate="recto"),
    ])

    def to_recto(story):
        """Next content starts on a right-hand (odd) page; a skipped left page stays fully blank."""
        story.append(PageBreak())
        story.append(DocIf("doc.page % 2 == 0", [_Mark("blank_pages"), PageBreak()]))

    story = []
    # front matter: title page, copyright, dedication, contents
    story += [_Mark("front_pages"), Spacer(1, fh * 0.28), Paragraph(escape(title), t_title)]
    if details.get("subtitle"):
        story.append(Paragraph(escape(details["subtitle"]), t_sub))
    if author:
        story += [Spacer(1, fh * 0.3), Paragraph(escape(author), t_author)]
    story += [PageBreak(), _Mark("front_pages")]
    year = datetime.now().year
    copy_text = details.get("copyright") or (f"Copyright © {year} {author}. All rights reserved." if author else
                                             f"Copyright © {year}. All rights reserved.")
    copy_lines = [escape(line) for line in copy_text.splitlines() if line.strip()]
    if details.get("isbn"):
        copy_lines.append("ISBN " + escape(details["isbn"]))
    copy_paras = [Paragraph(line, ParagraphStyle(f"c{i}", parent=copy_style, spaceAfter=6))
                  for i, line in enumerate(copy_lines)]
    copy_h = sum(p.wrap(fw, fh)[1] + 6 for p in copy_paras)
    story += [Spacer(1, max(0, fh - copy_h - 4))] + copy_paras
    if details.get("dedication"):
        to_recto(story)
        story += [_Mark("front_pages"), Spacer(1, fh * 0.3)]
        story += [Paragraph(escape(line), dedic) for line in details["dedication"].splitlines() if line.strip()]
    chapters = [b for b in blocks if b["t"] == "chapter"]
    toc = None
    if details.get("toc", True) and len(chapters) >= 2:
        to_recto(story)
        toc = TableOfContents(dotsMinLevel=0, tableStyle=TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), body_r), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))  # no stray Helvetica
        toc.levelStyles = [ParagraphStyle("TOC0", fontName=body_r, fontSize=st["body_size"],
                                          leading=st["leading"] * 1.25, textColor=BLACK, leftIndent=0,
                                          firstLineIndent=0, rightIndent=0.3 * inch)]
        story += [_Mark("front_pages"), Spacer(1, fh * 0.08),
                  Paragraph("Contents", ParagraphStyle("TOCT", parent=chapter, spaceAfter=st["leading"] * 1.5)),
                  toc]
    to_recto(story)
    story.append(_Mark("body_started", True))

    # body
    chapter_no = 0
    first_in_section = True
    first_chapter = True
    for b in blocks:
        t = b["t"]
        if t == "chapter":
            if not first_chapter:
                if st["recto_chapters"]:
                    to_recto(story)
                else:
                    story.append(PageBreak())
            first_chapter = False
            text = b["text"]
            lab, ttl = None, text
            mm = _CHAPTER_PREFIX.match(text)
            if mm:
                chapter_no += 1
                lab, ttl = "Chapter " + mm.group(1), mm.group(2) or ""
            elif not _NAMED_HEADING.match(text) and st["numbering"] != "none":
                chapter_no += 1
                lab = ("Chapter " + number_words(chapter_no)) if st["numbering"] == "words" else str(chapter_no)
            if lab and st["numbering"] == "numerals" and mm and mm.group(1).isdigit():
                lab = mm.group(1)
            story += [_Mark("opener_pages"), Spacer(1, fh * st["sink"])]
            toc_text = (f"{lab}: {ttl}" if lab and ttl else (lab or ttl))
            story.append(_TocEntry(toc_text))
            if lab:
                ltxt = escape(lab.upper() if st["label_upper"] else lab)
                if st["label_tracking"]:
                    story.append(_Tracked(lab.upper() if st["label_upper"] else lab, label_f, st["label_size"],
                                          st["label_tracking"], st["title_align"], st["label_size"] * 0.9))
                else:
                    story.append(Paragraph(ltxt, label))
            if ttl:
                story.append(Paragraph(escape(ttl.upper() if st["title_upper"] else ttl), chapter))
            elif lab:
                story.append(Spacer(1, st["leading"] * 1.5))
            if st["ornament"]:
                story.append(Paragraph(st["ornament"], ornament))
            first_in_section = True
            continue
        if t == "pagebreak":
            story.append(PageBreak())
            first_in_section = True
            continue
        if t == "scene":
            story.append(Paragraph(escape(st["scene_break"]), scene) if st["scene_break"]
                         else Spacer(1, st["leading"] * 1.2))
            first_in_section = True
            continue
        if t == "gap":
            story.append(Spacer(1, st["leading"] * 0.5))
            continue
        if t == "sub":
            story.append(Paragraph(b["markup"], sub))
            first_in_section = True
            continue
        if t == "quote":
            story.append(Paragraph(b["markup"], quote))
            continue
        if t == "bullet":
            story.append(Paragraph(b["markup"], bullet, bulletText=b.get("marker", "•")))
            continue
        # paragraph
        style = body_first if first_in_section else body
        if b.get("align") and b["align"] in _ALIGN and b["align"] != st["align"]:
            style = ParagraphStyle("A", parent=style, alignment=_ALIGN[b["align"]],
                                   firstLineIndent=0 if b["align"] in ("center", "right") else style.firstLineIndent)
        if first_in_section and st["drop_cap"] and story and _opener_just_before(story) and len(b["plain"]) > 80:
            story.append(_drop_cap_para(b, style, body_family, st))
        else:
            story.append(Paragraph(b["markup"], style))
        first_in_section = False

    if not blocks:
        story.append(Paragraph("Start writing, or import your Word file, and your pages appear here.", body_first))

    def after(flowable):
        if isinstance(flowable, _TocEntry) and toc is not None:
            doc.notify("TOCEntry", (0, flowable.text, doc.page))
    doc.afterFlowable = after
    if toc is not None:
        doc.multiBuild(story, maxPasses=6)
    else:
        doc.build(story)

    pages = doc.page
    warnings = []
    missing = _missing_chars(blocks, details, [body_r, head_r])
    if missing:
        warnings.append({"id": "missing_glyphs", "message": f"These characters aren't in the {st['body_font']} font and "
                         f"would print as empty boxes: {missing}. Try another book style, or replace them."})
    return {
        "page_count": pages,
        "min_margins_for": page_guess,
        "margins": {k: round(v, 4) for k, v in m.items()},
        "distributor_minimums": mins,
        "margin_source": MARGIN_SOURCES.get(platform, ""),
        "trim": [trim_w, trim_h],
        "style": payload.get("style") or "classic_novel",
        "chapters": len(chapters),
        "words": sum(len((bl.get("plain") or "").split()) for bl in blocks),
        "warnings": warnings,
    }


class _Tracked(Flowable):
    """One line of letter-spaced capitals (chapter labels): CHAPTER ONE."""
    def __init__(self, text, font, size, tracking, align, space_after):
        super().__init__()
        self.text, self.font, self.size, self.track, self.align = text, font, size, tracking, align
        self.spaceAfter = space_after
        self.keepWithNext = 1

    def wrap(self, aw, ah):
        self.aw = aw
        return aw, self.size * 1.25

    def draw(self):
        n = len(self.text)
        w = pdfmetrics.stringWidth(self.text, self.font, self.size) + self.track * (n - 1)
        x = 0 if self.align == "left" else (self.aw - w) / 2
        t = self.canv.beginText(x, self.size * 0.25)
        t.setFont(self.font, self.size)
        t.setCharSpace(self.track)
        t.setFillColor(BLACK)
        t.textOut(self.text)
        self.canv.drawText(t)


class _TocEntry(Flowable):
    def __init__(self, text):
        super().__init__()
        self.text = text

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        pass


def _opener_just_before(story) -> bool:
    """True when the paragraph about to be added is the first one after a chapter opener."""
    for f in reversed(story):
        if isinstance(f, Paragraph) and f.style.name in ("Chapter", "Ornament", "Label"):
            return True
        if isinstance(f, _Tracked):
            return True
        if isinstance(f, (Spacer, _TocEntry, _Mark)):
            continue
        return False
    return False


def _drop_cap_para(b, style, family, st):
    """First paragraph of a chapter with a real sunk drop cap (quotes stay with the letter)."""
    markup = b["markup"]
    m = re.match(r"^((?:<[^>]+>)*)((?:&quot;|“|‘|\"|')?)(\w)", markup)
    if not m:
        return Paragraph(markup, style)
    lead_tags, quote_mark, letter = m.groups()
    rest = lead_tags + markup[m.end():]
    cap_font = _font(st["heading_font"] if st["heading_font"] != "Lato" else st["body_font"])
    cap = _DropCap((quote_mark.replace("&quot;", '"') if quote_mark else "") + letter, cap_font,
                   st["body_size"], st["leading"], st["drop_lines"])
    para = Paragraph(rest, ParagraphStyle("DC", parent=style, firstLineIndent=0))
    return ImageAndFlowables(cap, [para], imageSide="left", imageRightPadding=3, imageBottomPadding=0,
                             imageTopPadding=0)


def payload_key(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def render_page_jpeg(pdf_path: str, page_no: int, dpi: int = 80) -> bytes:
    """One page as a JPEG for the on-screen preview (screen resolution, not print resolution)."""
    import pymupdf
    with pymupdf.open(pdf_path) as d:
        if page_no < 1 or page_no > d.page_count:
            raise IndexError(page_no)
        pix = d[page_no - 1].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        return pix.tobytes("jpeg", jpg_quality=72)
