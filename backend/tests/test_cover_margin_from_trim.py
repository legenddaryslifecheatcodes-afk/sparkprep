"""The cover safe area is measured from the TRIM (where the book is cut), not the edge of the file.

IngramSpark: "All text should be a minimum of 0.25 inches (6mm) away from the trim line, or edge of hard
cover board". The check used to measure from the file's edge -- the bleed outside the trim counted as
safe space, so a title 0.19" from the cut passed on a paperback, and on a 0.625" case wrap text right at
the board edge passed. Found while building the SparkPrep demo (2026-09-29)."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from pdfx_validator import check_cover_safety_margins, ocr_status  # noqa: E402
from file_processor import autofix_cover_safe_margin  # noqa: E402

pytestmark = pytest.mark.skipif(not ocr_status().get("available"), reason="needs Tesseract OCR")
DPI = 250


def _font(size_px):
    for name in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size_px)
        except OSError:
            continue
    pytest.skip("no TrueType font available")


def cover_with_title(w_in, h_in, title_top_from_file_edge_in):
    img = Image.new("RGB", (round(w_in * DPI), round(h_in * DPI)), (30, 25, 70))
    d = ImageDraw.Draw(img)
    font = _font(int(0.5 * DPI))
    x = int((w_in * 0.62) * DPI)
    # draw so the glyphs' real top lands where asked (fonts carry space above the capitals)
    top_offset = d.textbbox((0, 0), "MIDNIGHT", font=font)[1]
    d.text((x, int(title_top_from_file_edge_in * DPI) - top_offset), "MIDNIGHT", font=font, fill=(245, 214, 120))
    path = Path(tempfile.mkdtemp()) / "cover.png"
    img.save(path, dpi=(DPI, DPI))
    return str(path)


def outer(findings):
    return next((f for f in findings if f["id"] == "cover_safety_margin"), None)


def test_paperback_text_is_measured_from_the_trim_not_the_file_edge():
    path = cover_with_title(12.34, 9.25, 0.31)                        # 0.31" from file edge = ~0.19" from the cut
    f = outer(check_cover_safety_margins(path, False, 12.34, 9.25, "IngramSpark", bleed_in=0.125))
    assert f and 0.15 <= f["pinpoint"]["margin_in"] <= 0.23
    assert "from trim" in f["title"]


def test_case_wrap_text_inside_the_wrap_is_flagged():
    # a 0.625" case wrap: text 0.7" from the file edge is only ~0.075" from the board edge
    path = cover_with_title(14.194, 10.5, 0.70)
    f = outer(check_cover_safety_margins(path, False, 14.194, 10.5, "IngramSpark", bleed_in=0.625))
    assert f and f["severity"] == "fail" and f["pinpoint"]["margin_in"] < 0.125


def test_the_auto_fix_moves_text_back_inside_the_safe_area():
    w, h, bleed = 12.34, 9.25, 0.125
    path = cover_with_title(w, h, 0.31)
    f = outer(check_cover_safety_margins(path, False, w, h, "IngramSpark", bleed_in=bleed))
    fixed = str(Path(path).with_name("fixed.png"))
    autofix_cover_safe_margin(path, fixed, f["pinpoint"]["margin_in"] + bleed, w, h, target_margin_in=0.27 + bleed)
    after = outer(check_cover_safety_margins(fixed, False, w, h, "IngramSpark", bleed_in=bleed))
    assert after is None, after and after["title"]
