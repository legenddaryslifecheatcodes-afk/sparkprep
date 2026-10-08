"""SparkPrep vs. the owner's real IngramSpark rejections (Title Processing Error emails, June 2026).

Each rejection reason must be caught -- and fixed where SparkPrep can fix it:
  6/24  JACKET: incorrect spine width (0.375" for 108 pages) / ISBN on jacket doesn't match / layout not to spec
  6/26  BOOKBLOCK: interior content outside the safety area, not centered        (letter-size file for a 6x9 book)
  6/29  COVER: template boxes visible on cover file, PDF document size incorrect   (template cropped, labels left in)
  6/29  BOOKBLOCK: RGB colors in interior file -- text may print gray, not 100% black
  10/7  JACKET: incorrect spine width, 0.313" for 78 pages -- REDUCE SPINE TEXT TO FIT (a SparkPrep-certified export:
        its spine lettering ran sideways and SparkPrep's upright text read never saw it)

The always-run tests use small made-up files. The real-file tests use the owner's own rejected files, which stay on
his computer (they're his book): they run there and skip anywhere the files aren't present.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import file_processor as fp  # noqa: E402
from pdfx_validator import (_isbn13_ok, check_cover_isbn, check_cover_template_leftovers,  # noqa: E402
                            check_interior_safety_margins, check_rgb_color)
from print_specs import calculate_spine_width_for_platform  # noqa: E402

DL = Path(r"C:\Users\legen\Downloads")
BOOK_ISBN = "9798349419829"


def need(name):
    p = DL / name
    if not p.exists():
        pytest.skip(f"owner's real file not on this machine: {name}")
    return str(p)


def _cover(path, draw):
    c = canvas.Canvas(str(path), pagesize=(20.5 * inch, 9.5 * inch))
    c.setFillColorRGB(0.08, 0.07, 0.06)
    c.rect(0, 0, 20.5 * inch, 9.5 * inch, fill=1, stroke=0)
    draw(c)
    c.showPage()
    c.save()
    return str(path)


# ---------- always-run ----------

def test_isbn13_checksum():
    assert _isbn13_ok(BOOK_ISBN) and _isbn13_ok("9780306406157")
    assert not _isbn13_ok("9798349419828") and not _isbn13_ok("97983494198")


def test_template_boxes_are_caught_and_plain_art_is_not(tmp_path):
    def boxes(c):
        c.setFillColorRGB(252 / 255, 232 / 255, 241 / 255)      # IngramSpark's pink safe area
        c.rect(2 * inch, 2 * inch, 5 * inch, 5 * inch, fill=1, stroke=0)
        c.setFillColorRGB(170 / 255, 224 / 255, 249 / 255)      # and blue bleed area
        c.rect(0, 0, 20.5 * inch, 0.3 * inch, fill=1, stroke=0)
    left = check_cover_template_leftovers(_cover(tmp_path / "boxes.pdf", boxes), True, 20.5, "IngramSpark")
    assert [f["id"] for f in left] == ["cover_template_leftovers"]
    assert "pink and blue" in left[0]["why_it_fails"]

    def art(c):
        c.setFillColorRGB(0.83, 0.69, 0.22)
        c.setFont("Helvetica-Bold", 90)
        c.drawString(11 * inch, 5 * inch, "MY BOOK")
    assert check_cover_template_leftovers(_cover(tmp_path / "art.pdf", art), True, 20.5, "IngramSpark") == []


def test_template_labels_left_in_the_artwork_are_caught(tmp_path):
    def labels(c):
        c.setFillColorRGB(0.9, 0.9, 0.9)
        c.setFont("Helvetica", 18)
        c.drawString(1 * inch, 0.6 * inch, "Dust Jacket Cover Template   Document Size: 24 x 12.5   Lightning Source")
        c.drawString(1 * inch, 1.0 * inch, "Bleed Artwork Dimensions: 20.5 x 9.5   3.25 flap   .25 wrap   Request ID")
    left = check_cover_template_leftovers(_cover(tmp_path / "labels.pdf", labels), True, 20.5, "IngramSpark")
    assert [f["id"] for f in left] == ["cover_template_leftovers"]


def test_cover_isbn_must_match_the_book(tmp_path):
    def isbn(c):
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica", 22)
        c.drawString(2 * inch, 1 * inch, "ISBN 978-0-306-40615-7")
    f = _cover(tmp_path / "isbn.pdf", isbn)
    wrong = check_cover_isbn(f, True, 20.5, BOOK_ISBN, "IngramSpark")
    assert [x["id"] for x in wrong] == ["cover_isbn_mismatch"]
    assert "978-0-3064-0615-7" in wrong[0]["title"] and "979-8-3494-1982-9" in wrong[0]["title"]
    assert check_cover_isbn(f, True, 20.5, "978-0-306-40615-7", "IngramSpark") == []     # the right one
    assert check_cover_isbn(f, True, 20.5, None, "IngramSpark") == []                     # book has no ISBN set

    def none(c):
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica", 40)
        c.drawString(2 * inch, 4 * inch, "A cover with no barcode")
    assert check_cover_isbn(_cover(tmp_path / "none.pdf", none), True, 20.5, BOOK_ISBN, "IngramSpark") == []


def test_spine_for_108_creme_pages_is_what_ingramspark_said():
    # 6/24 rejection: "INCORRECT SPINE WIDTH - SPINE SHOULD BE 0.375" FOR 108 PAGES"
    spine, confirmed = calculate_spine_width_for_platform(108, 444, "ingramspark", "hardcover_jacket")
    assert confirmed and spine == 0.375


# ---------- the owner's real rejected files ----------

def test_626_letter_size_interior_is_caught_and_fixed(tmp_path):
    src = need("legenddary mindset done done ingramsparkdonedonedone111111626.pdf")
    work = tmp_path / "in.pdf"
    shutil.copy(src, work)
    before = check_interior_safety_margins(str(work), "IngramSpark", 6, 9, max_pages=None, bleed_in=0.125)
    assert [f["id"] for f in before] == ["interior_page_size_mismatch"]
    assert "not centered" in before[0]["why_it_fails"]
    out = tmp_path / "fixed.pdf"
    fp.autofix_interior_safety_margins(str(work), str(out), 6, 9)
    assert check_interior_safety_margins(str(out), "IngramSpark", 6, 9, max_pages=None, bleed_in=0.125) == []


def test_627_cropped_template_jacket_is_caught():
    f = need("REAL JACKET TEMPLATE (1).pdf")
    left = check_cover_template_leftovers(f, True, 20.5, "IngramSpark")
    assert [x["id"] for x in left] == ["cover_template_leftovers"]


def test_good_final_jacket_has_no_template_and_the_right_isbn():
    f = need("Legenddary_Mindset_Awakening_Ingram_Jacket_FINAL_74pp.pdf")
    assert check_cover_template_leftovers(f, True, 20.438, "IngramSpark") == []
    assert check_cover_isbn(f, True, 20.438, BOOK_ISBN, "IngramSpark") == []
    wrong = check_cover_isbn(f, True, 20.438, "9780306406157", "IngramSpark")         # pretend it's another book
    assert [x["id"] for x in wrong] == ["cover_isbn_mismatch"]


def test_629_rgb_interior_comes_out_black_ink_only(tmp_path):
    import pikepdf
    from ghostscript_engine import find_ghostscript
    if not find_ghostscript():
        pytest.skip("Ghostscript not installed here")
    src = need("LegenddaryMindset INTERIORINTERIORfinal.pdf")
    work = tmp_path / "in.pdf"
    shutil.copy(src, work)
    with pikepdf.open(str(work)) as pdf:
        assert check_rgb_color(pdf, max_pages=None) is not None                          # what IngramSpark rejected
    out = tmp_path / "out.pdf"
    fp.build_interior_pdf_x1a(str(work), str(out), 6, 9, 0.125, grayscale=True)
    with pikepdf.open(str(out)) as pdf:
        assert check_rgb_color(pdf, max_pages=None) is None
    assert fp.print_conversion_reasons(str(out), grayscale=True) == []                  # 100% black ink only


def _jacket_with_spine_text(path, letter_height_in):
    """6x9 IngramSpark jacket for 78pp (20.4375 x 9.5, spine 10.0625-10.375) with the title set sideways on the spine."""
    c = canvas.Canvas(str(path), pagesize=(20.4375 * inch, 9.5 * inch))
    c.setFillColorRGB(0.08, 0.07, 0.06)
    c.rect(0, 0, 20.4375 * inch, 9.5 * inch, fill=1, stroke=0)
    c.setFillColorRGB(0.95, 0.85, 0.4)
    c.saveState()
    c.translate((10.21875 + letter_height_in / 2) * inch, 1.2 * inch)     # letters centred on the spine
    c.rotate(90)
    c.setFont("Helvetica-Bold", letter_height_in * 72 / 0.72)              # Helvetica caps are ~0.72 of the font size
    c.drawString(0, 0, "LEGENDDARY MINDSET")
    c.restoreState()
    c.showPage()
    c.save()
    return str(path)


def _spine_findings(f):
    from pdfx_validator import check_cover_safety_margins
    return [x for x in check_cover_safety_margins(f, True, 20.4375, 9.5, "IngramSpark", spine_x_in=10.0625,
                                                  spine_w_in=0.3125, page_count=78, binding="hardcover_jacket",
                                                  bleed_in=0.125)
            if x["id"].startswith("cover_spine_text")]


def test_sideways_spine_text_too_wide_is_caught(tmp_path):
    wide = _spine_findings(_jacket_with_spine_text(tmp_path / "wide.pdf", 0.8))
    assert [x["id"] for x in wide] == ["cover_spine_text_margin"] and wide[0]["severity"] == "fail"
    assert "too wide" in wide[0]["title"]


def test_sideways_spine_text_that_fits_passes(tmp_path):
    assert _spine_findings(_jacket_with_spine_text(tmp_path / "fits.pdf", 0.2)) == []


def test_1007_rejected_jacket_spine_is_caught():
    f = Path(os.path.expanduser("~")) / "OneDrive" / "Desktop" / "BOOK FILES - Legenddary Mindset" / "2 - DUST JACKET - Legenddary Mindset.pdf"
    if not f.exists():
        pytest.skip("owner's real file not on this machine")
    found = _spine_findings(str(f))
    assert [x["id"] for x in found] == ["cover_spine_text_margin"] and found[0]["severity"] == "fail"



def _fold_findings(f, is_pdf=True):
    from pdfx_validator import check_jacket_folds
    return check_jacket_folds(f, is_pdf, 20.4375, 9.5, [(3.375, 3.625), (16.8125, 17.0625)], "IngramSpark")


def test_text_on_a_flap_fold_is_caught_and_clear_text_passes(tmp_path):
    def jacket(path, x_in, text="About the author and the book"):
        c = canvas.Canvas(str(path), pagesize=(20.4375 * inch, 9.5 * inch))
        c.setFillColorRGB(0.08, 0.07, 0.06)
        c.rect(0, 0, 20.4375 * inch, 9.5 * inch, fill=1, stroke=0)
        c.setFillColorRGB(0.95, 0.9, 0.8)
        c.setFont("Helvetica", 16)
        c.drawString(x_in * inch, 5 * inch, text)
        c.setStrokeColorRGB(0.85, 0.7, 0.2)                     # a gold rule right at the fold: artwork, not text
        c.line(16.8 * inch, 0.5 * inch, 16.8 * inch, 9.0 * inch)
        c.showPage()
        c.save()
        return str(path)
    assert [f["id"] for f in _fold_findings(jacket(tmp_path / "on_fold.pdf", 1.6))] == ["cover_jacket_fold"]
    assert _fold_findings(jacket(tmp_path / "clear.pdf", 0.5, "About the author")) == []   # ends ~2.1", fold at 3.375"


def test_1007_both_old_jackets_fail_the_fold_check_and_the_rebuilt_one_passes():
    home = Path(os.path.expanduser("~"))
    old = home / "Downloads" / "Legenddary_Mindset_Awakening_Ingram_Jacket_FINAL_74pp.pdf"
    sent = home / "OneDrive" / "Desktop" / "BOOK FILES - Legenddary Mindset" / "2 - DUST JACKET - Legenddary Mindset.pdf"
    rebuilt = home / "Downloads" / "Legenddary Mindset - FIXED JACKET" / "Legenddary Mindset - Dust Jacket - REBUILT 78pp.pdf"
    if not (old.exists() and sent.exists() and rebuilt.exists()):
        pytest.skip("owner's real files not on this machine")
    assert [f["id"] for f in _fold_findings(str(old))] == ["cover_jacket_fold"]
    assert [f["id"] for f in _fold_findings(str(sent))] == ["cover_jacket_fold"]
    assert _fold_findings(str(rebuilt)) == []
    assert _spine_findings(str(rebuilt)) == []
