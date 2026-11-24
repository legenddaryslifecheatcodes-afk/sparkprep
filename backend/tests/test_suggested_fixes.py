"""Suggested fixes (2026-11-24) and the PDF cover safe-margin Auto-Fix.

Each suggested fix is checked by the SAME scanner check that flagged the
problem: after the fix, that check must pass (or, for the shape fix, the file
must be exactly the right shape with nothing squashed more than 2%)."""
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import suggested_fixes as sf  # noqa: E402
import file_processor as fp  # noqa: E402
from pdfx_validator import (check_cover_template_leftovers, check_cover_isbn, check_cover_safety_margins,  # noqa: E402
                            _template_box_share)

BOOK_ISBN = "9798349419829"


def _cover_pdf(path, draw, w_in=20.5, h_in=9.5):
    c = canvas.Canvas(str(path), pagesize=(w_in * inch, h_in * inch))
    c.setFillColorRGB(0.08, 0.07, 0.06)
    c.rect(0, 0, w_in * inch, h_in * inch, fill=1, stroke=0)
    draw(c)
    c.showPage()
    c.save()
    return str(path)


# ------------------------------------------------------------- cover shape
def test_slightly_wrong_shape_is_fitted_without_squashing(tmp_path):
    # 12.75" x 9.25" needed; art is 4% too narrow.
    w_in, h_in = 12.75, 9.25
    h = 925
    w = round(h * w_in / h_in / 1.04)
    img = Image.new("RGB", (w, h), (30, 60, 120))
    ImageDraw.Draw(img).ellipse((w // 2 - 150, h // 2 - 150, w // 2 + 150, h // 2 + 150), fill=(240, 200, 40))
    out, summary = sf.fit_cover_shape(img, w_in, h_in)
    assert abs(out.width / out.height - w_in / h_in) < 0.002
    assert out.height == h
    # the circle in the middle is still a circle (stretched at most 2%)
    a = np.asarray(out)
    ys, xs = np.where((a[..., 0] > 200) & (a[..., 1] > 150))
    ratio = (xs.max() - xs.min()) / (ys.max() - ys.min())
    assert 0.99 <= ratio <= 1.03
    assert "4.0% off" in summary and "squashed" in summary


def test_too_wrong_shape_is_not_offered():
    img = Image.new("RGB", (1000, 925), (30, 60, 120))
    with pytest.raises(sf.NotOffered, match="rebuilt"):
        sf.fit_cover_shape(img, 12.75, 9.25)        # ~27% off


def test_slightly_too_wide_is_trimmed_evenly():
    img = Image.new("RGB", (1325, 925), (30, 60, 120))  # needs 1275 wide: 3.8% too wide
    out, summary = sf.fit_cover_shape(img, 12.75, 9.25)
    assert abs(out.width - 1275) <= 1 and "trimmed" in summary


# ------------------------------------------------------------- template leftovers
def test_template_boxes_are_painted_out_and_the_check_passes(tmp_path):
    def boxes(c):
        c.setFillColorRGB(0.25, 0.3, 0.5)
        c.rect(0, 0, 20.5 * inch, 9.5 * inch, fill=1, stroke=0)
        c.setFillColorRGB(252 / 255, 232 / 255, 241 / 255)
        c.rect(2 * inch, 2 * inch, 1.2 * inch, 0.6 * inch, fill=1, stroke=0)
        c.setFillColorRGB(170 / 255, 224 / 255, 249 / 255)
        c.rect(0, 0, 20.5 * inch, 0.12 * inch, fill=1, stroke=0)
    src = _cover_pdf(tmp_path / "boxes.pdf", boxes)
    assert check_cover_template_leftovers(src, True, 20.5, "IngramSpark")
    img, info = sf.load(src)
    out, summary = sf.remove_template_guides(img)
    fixed = sf.save(out, info, str(tmp_path / "fixed"))
    assert fixed.endswith(".pdf")
    assert check_cover_template_leftovers(fixed, True, 20.5, "IngramSpark") == []
    assert sum(_template_box_share(fixed, True)) < 0.0005
    assert "guide boxes" in summary


def test_nothing_to_remove_is_not_offered():
    with pytest.raises(sf.NotOffered):
        sf.remove_template_guides(Image.new("RGB", (800, 400), (20, 20, 20)))


# ------------------------------------------------------------- spine text
def test_spine_text_is_cleared():
    img = Image.new("RGB", (2050, 950), (90, 20, 20))
    d = ImageDraw.Draw(img)
    d.rectangle((1018, 100, 1032, 850), fill=(250, 250, 250))   # "text" down a 0.2" spine at 100 px/in
    out, summary = sf.clear_spine_text(img, 20.5, 10.15, 0.2)
    band = np.asarray(out)[:, 1015:1036].astype(int)
    assert np.abs(band - np.array([90, 20, 20])).max() < 60
    assert "too thin" in summary


# ------------------------------------------------------------- barcode
def test_wrong_barcode_is_replaced_and_the_isbn_check_passes(tmp_path):
    from barcode_engine import generate_barcode_png_bytes

    old = tmp_path / "old.png"
    old.write_bytes(generate_barcode_png_bytes("9780306406157"))

    def back(c):
        c.setFillColorRGB(1, 1, 1)
        c.rect(1.5 * inch, 0.8 * inch, 2.4 * inch, 1.6 * inch, fill=1, stroke=0)
        c.drawImage(str(old), 1.7 * inch, 1.0 * inch, 2 * inch, 1.2 * inch)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 14)
        c.drawString(1.75 * inch, 2.25 * inch, "ISBN 978-0-306-40615-7")
    src = _cover_pdf(tmp_path / "isbn.pdf", back)
    assert [f["id"] for f in check_cover_isbn(src, True, 20.5, BOOK_ISBN, "IngramSpark")] == ["cover_isbn_mismatch"]
    img, info = sf.load(src)
    out, summary = sf.replace_barcode(img, BOOK_ISBN, info["w_in"])
    fixed = sf.save(out, info, str(tmp_path / "fixed"))
    assert check_cover_isbn(fixed, True, 20.5, BOOK_ISBN, "IngramSpark") == []
    assert BOOK_ISBN in summary


def test_barcode_needs_the_books_isbn():
    with pytest.raises(sf.NotOffered, match="ISBN"):
        sf.replace_barcode(Image.new("RGB", (100, 100)), "", 1.0)


# ------------------------------------------------------------- PDF cover margin (automatic)
def test_pdf_cover_text_is_pulled_inside_the_safe_margin_and_stays_vector(tmp_path):
    import fitz

    def edge_text(c):
        c.setFillColorRGB(0.95, 0.85, 0.3)
        c.setFont("Helvetica-Bold", 40)
        c.drawString(4 * inch, 9.5 * inch - 0.15 * inch - 40, "EDGE TITLE")   # top of letters ~0.15" from the edge
    src = _cover_pdf(tmp_path / "edge.pdf", edge_text, 12.75, 9.5)
    before = [f for f in check_cover_safety_margins(src, True, 12.75, 9.5, "KDP", bleed_in=0.125)
              if f["id"] == "cover_safety_margin"]
    assert before and before[0]["one_click_fix"] is True
    out = tmp_path / "fixed.pdf"
    r = fp.autofix_pdf_cover_safe_margin(src, str(out), before[0]["pinpoint"]["margin_in"] + 0.125, 0.27 + 0.125)
    assert r["vector_kept"] and r["scale_factor"] < 1.0
    after = [f for f in check_cover_safety_margins(str(out), True, 12.75, 9.5, "KDP", bleed_in=0.125)
             if f["id"] == "cover_safety_margin"]
    assert not after or after[0]["pinpoint"]["margin_in"] > before[0]["pinpoint"]["margin_in"]
    with fitz.open(str(out)) as d:
        assert abs(d[0].rect.width - 12.75 * 72) < 0.5 and abs(d[0].rect.height - 9.5 * 72) < 0.5
        assert "EDGE TITLE" in d[0].get_text()            # still real text, not a picture


def _edge_text_cover(w, h=2775):
    from PIL import ImageDraw, ImageFont
    img = Image.new("RGB", (w, h), (40, 40, 110))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 110)
    d.text((150, 300), "BACKCOPY", font=f, fill=(255, 255, 255))           # 0.5" in from the left
    d.text((w - 150, 300), "TITLE", font=f, fill=(255, 255, 255), anchor="ra")  # 0.5" in from the right
    return img


@pytest.mark.parametrize("pct", [4.9, 5.0, -4.9, -5.0])
def test_shape_fit_at_the_5_percent_limit_keeps_text_safe(pct, tmp_path):
    from pdfx_validator import check_cover_safety_margins
    h = 2775
    w = round(h * (12.7 / 9.25) / (1 + pct / 100))
    out, msg = sf.fit_cover_shape(_edge_text_cover(w, h), 12.7, 9.25)
    assert abs(out.width / out.height - 12.7 / 9.25) < 0.002          # right shape
    p = tmp_path / "out.png"
    out.save(p)
    found = check_cover_safety_margins(str(p), False, 12.7, 9.25, "ingramspark", bleed_in=0.125)
    assert not [c for c in found if c.get("id") == "cover_safety_margin"], msg


def test_shape_fit_just_over_5_percent_is_refused():
    with pytest.raises(sf.NotOffered, match="5.1%"):
        sf.fit_cover_shape(_edge_text_cover(round(2775 * (12.7 / 9.25) / 1.051)), 12.7, 9.25)
