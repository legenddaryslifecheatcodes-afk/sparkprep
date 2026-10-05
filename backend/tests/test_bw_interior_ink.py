"""Black & white interiors can't fail the 240% total-ink limit (owner, 2026-10-05: "most users aren't going to
know how to fix print coverage at 240 … why can't we fix this for the users").

The owner's own book failed it on nearly every page: black text drawn in screen colour (RGB) converts for print
into "rich black" -- all four inks, ~294%. Two fixes, both checked here:
  1. The book builder (manuscript_composer) writes black and grey as black ink only.
  2. A black & white book's interior is exported as true grayscale, so an uploaded PDF with RGB black text or
     4-colour rich black comes out at no more than 100% ink.
"""
import sys
from pathlib import Path

import numpy as np
import pikepdf
import pytest
from PIL import Image
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import file_processor as fp  # noqa: E402
from manuscript_composer import compose_manuscript_pdf  # noqa: E402
from print_specs import BLACK_AND_WHITE_PAPERS, PAPER_TYPES  # noqa: E402

TEXT = "# Chapter One\n" + "\n".join("It was a dark and stormy night, and the ink ran heavy on the page. " * 6 for _ in range(30))


def _colour_ops(pdf_path):
    ops = set()
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            for op in pikepdf.parse_content_stream(page):
                if str(op.operator) in ("rg", "RG", "k", "K", "g", "G"):
                    ops.add((str(op.operator), tuple(round(float(v), 3) for v in op.operands)))
    return ops


def _heavy_black_pdf(path):
    """What a typical Word/Canva export looks like: RGB black text, a 4-colour rich-black block, a colour photo."""
    photo = path.parent / "photo.png"
    Image.fromarray(np.random.default_rng(1).integers(0, 255, (120, 160, 3), dtype=np.uint8), "RGB").save(photo)
    c = canvas.Canvas(str(path), pagesize=(6 * inch, 9 * inch))
    for _ in range(2):
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 40)
        for y in range(40, 640, 45):
            c.drawString(20, y, "HEAVY BLACK TEXT")
        c.setFillColorCMYK(0.75, 0.68, 0.67, 0.9)
        c.rect(300, 20, 120, 600, fill=1, stroke=0)
        c.drawImage(str(photo), 20, 300, 150, 110)
        c.showPage()
    c.save()


def test_black_and_white_papers_are_the_bw_ones():
    assert BLACK_AND_WHITE_PAPERS <= set(PAPER_TYPES)
    assert not any("color" in k for k in BLACK_AND_WHITE_PAPERS)


def test_composer_writes_black_ink_only(tmp_path):
    out = tmp_path / "composed.pdf"
    compose_manuscript_pdf(str(out), "fiction_novel", "Ink Test", "A. Author", TEXT, 6, 9, "ingramspark")
    ops = _colour_ops(out)
    assert not any(o in ("rg", "RG") for o, _ in ops), f"RGB colour in the composed book: {ops}"
    for o, v in ops:
        if o in ("k", "K"):
            assert v[:3] == (0, 0, 0), f"composed colour uses more than black ink: {o} {v}"
    assert fp.check_final_pdf_ink_coverage(str(out), "ingramspark", "IngramSpark") is None


def _needs_gs():
    from ghostscript_engine import find_ghostscript
    if not find_ghostscript():
        pytest.skip("Ghostscript not installed here (it is in the Render image)")


def test_bw_export_of_heavy_black_pdf_passes_ink(tmp_path):
    _needs_gs()
    src = tmp_path / "upload.pdf"
    _heavy_black_pdf(src)

    colour_out = tmp_path / "colour.pdf"
    fp.build_interior_pdf_x1a(str(src), str(colour_out), 6.0, 9.0, 0.125, title="T")
    assert fp.check_final_pdf_ink_coverage(str(colour_out), "ingramspark", "IngramSpark") is not None, \
        "test file should reproduce the rich-black problem on a colour export"

    bw_out = tmp_path / "bw.pdf"
    result = fp.build_interior_pdf_x1a(str(src), str(bw_out), 6.0, 9.0, 0.125, title="T", grayscale=True)
    assert "not_grayscale" in result["print_conversion"]["reasons"]
    assert fp.check_final_pdf_ink_coverage(str(bw_out), "ingramspark", "IngramSpark") is None
    assert fp.print_conversion_reasons(str(bw_out), grayscale=True) == [], "B&W export must be fonts-embedded black ink only"
    with pikepdf.open(str(bw_out)) as pdf:
        assert len(pdf.pages) == 2 and str(pdf.Root.GTS_PDFXVersion) == "PDF/X-1a:2001"
        assert pdf.Root.get("/OutputIntents") is not None


def test_clean_cmyk_bw_book_is_left_alone(tmp_path):
    """A file that's already print-clean CMYK with ink under the limit isn't re-converted (a big image-heavy one
    took ~9 minutes) -- only colour that actually breaks the limit triggers the grayscale pass."""
    src = tmp_path / "clean.pdf"
    c = canvas.Canvas(str(src), pagesize=(6 * inch, 9 * inch))
    c.setFillColorCMYK(0.2, 0.1, 0.1, 0.3)
    c.rect(72, 72, 200, 200, fill=1, stroke=0)
    c.showPage()
    c.save()
    with pikepdf.open(str(src), allow_overwriting_input=True) as pdf:   # drop ReportLab's listed default font
        for page in pdf.pages:
            if "/Font" in page.Resources:
                del page.Resources["/Font"]
        pdf.save(str(src))
    assert fp.print_conversion_reasons(str(src), grayscale=True) == []


def test_clean_cmyk_rich_black_bw_book_is_converted(tmp_path):
    src = tmp_path / "rich.pdf"
    c = canvas.Canvas(str(src), pagesize=(6 * inch, 9 * inch))
    c.setFillColorCMYK(0.75, 0.68, 0.67, 0.9)
    c.rect(0, 0, 6 * inch, 9 * inch, fill=1, stroke=0)
    c.showPage()
    c.save()
    with pikepdf.open(str(src), allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            if "/Font" in page.Resources:
                del page.Resources["/Font"]
        pdf.save(str(src))
    assert fp.print_conversion_reasons(str(src)) == []                       # fine for a colour book's structure
    assert fp.print_conversion_reasons(str(src), grayscale=True) == ["ink_over_limit"]


def test_editor_marks_ink_as_fixed_on_export(monkeypatch, tmp_path):
    """The editor no longer says "upload a new file" for ink that export fixes by itself."""
    import os
    import tempfile
    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sp_ink_"))
    os.environ.setdefault("USE_MEMORY_DB", "1")
    os.environ.setdefault("JWT_SECRET", "ink-test-" + "x" * 32)
    import server
    ink = {"id": "total_ink_coverage", "status": "warning", "message": "Too much ink.", "auto_fix": False}
    other = {"id": "dpi", "status": "warning", "message": "Soft.", "auto_fix": True}
    stored = [dict(ink), dict(other)]
    bw = {"paper_type": "cream_50lb"}
    colour = {"paper_type": "color_60lb_premium"}
    pdf, cmyk_img, rgb_img = {"color_mode": "PDF"}, {"color_mode": "CMYK"}, {"color_mode": "RGB"}

    marked = server._mark_export_fixes(bw, "interior", pdf, stored)
    assert marked[0]["fixed_on_export"] is True and "automatically" in marked[0]["message"]
    assert "fixed_on_export" not in marked[1], "only the ink check is fixed by export"
    assert "fixed_on_export" not in stored[0], "the stored check is never changed"

    assert not server._export_fixes_ink(colour, "interior", pdf)       # colour PDF interiors aren't clamped yet
    assert server._export_fixes_ink(colour, "interior", rgb_img)       # an RGB image interior is
    assert server._export_fixes_ink(colour, "full_wrap", pdf)          # a PDF cover is redrawn with the limit
    assert server._export_fixes_ink(colour, "case_wrap", rgb_img)
    assert not server._export_fixes_ink(colour, "full_wrap", cmyk_img)  # CMYK art keeps its exact ink

    project = {"paper_type": "cream_50lb", "slots": {"interior": {"color_mode": "PDF", "compliance": stored}}}
    pd = server._mark_project_export_fixes(project, server.project_to_dict({**project, "_id": "x"}))
    assert pd["slots"]["interior"]["compliance"][0]["fixed_on_export"] is True
    assert "fixed_on_export" not in project["slots"]["interior"]["compliance"][0]
