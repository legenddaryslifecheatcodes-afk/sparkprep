"""IngramSpark hardcover spines, worked out from IngramSpark's own cover templates (owner's
Downloads\\special templates, 2026-09-30 and 2026-10-05) -- every number below is printed on a real template.
Creme (9 books) and White (6 books) are both solved; each matches every template exactly."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from print_specs import (calculate_full_cover_dimensions, calculate_spine_width_for_platform,  # noqa: E402
                         even_page_count, fmt_in, spine_source)

CREME, WHITE = 444, 512        # IngramSpark's paper sheet: Creme 50# = 444 PPI, White 50# = 512 PPI
# (pages asked for, spine, case width, jacket width) -- straight off IngramSpark's templates (6x9; heights 10.50 / 9.50)
CREME_TEMPLATES = [
    (74, "0.313", "14.194", "20.438"),
    (76, "0.313", "14.194", "20.438"),
    (86, "0.375", "14.256", None),
    (108, "0.375", "14.256", None),
    (122, "0.438", "14.319", "20.563"),
    (197, "0.625", "14.506", "20.750"),     # asked for 197 -- IngramSpark builds it as 198
    (300, "0.813", "14.694", "20.938"),
    (500, "1.250", "15.131", "21.375"),
]
WHITE_TEMPLATES = [
    (74, "0.250", "14.131", "20.375"),
    (76, "0.250", "14.131", "20.375"),      # the decider: the other fitting rule said 0.313
    (108, "0.313", "14.194", None),
    (200, "0.500", "14.381", "20.625"),
    (400, "0.875", "14.756", "21.000"),
    (500, "1.063", "14.944", None),
]


@pytest.mark.parametrize("ppi,pages,spine,case_w,jacket_w",
                         [(CREME, *t) for t in CREME_TEMPLATES] + [(WHITE, *t) for t in WHITE_TEMPLATES])
def test_spine_and_sizes_match_ingramspark_templates(ppi, pages, spine, case_w, jacket_w):
    s, confirmed = calculate_spine_width_for_platform(pages, ppi, "ingramspark", "hardcover_case")
    assert confirmed and fmt_in(s) == spine
    case = calculate_full_cover_dimensions(6, 9, s, 0.625, "hardcover_case", "ingramspark")
    assert (fmt_in(case["total_width"]), fmt_in(case["total_height"])) == (case_w, "10.500")
    js, _ = calculate_spine_width_for_platform(pages, ppi, "ingramspark", "hardcover_jacket")
    assert js == s                                                    # jacket spine = case spine, every template
    if jacket_w:
        jacket = calculate_full_cover_dimensions(6, 9, js, 0.125, "hardcover_jacket", "ingramspark")
        assert (fmt_in(jacket["total_width"]), fmt_in(jacket["total_height"])) == (jacket_w, "9.500")


def test_both_papers_come_from_templates_now():
    assert spine_source("ingramspark", "hardcover_case", CREME) == "ingramspark_templates"
    assert spine_source("ingramspark", "hardcover_jacket", WHITE) == "ingramspark_templates"
    assert spine_source("ingramspark", "hardcover_case", 400) == "template_needed"   # groundwood: no templates yet


def test_page_count_is_built_even_for_ingramspark():
    assert even_page_count(197, "ingramspark") == 198 and even_page_count(198, "ingramspark") == 198
    assert even_page_count(197, "kdp") == 197


def test_inches_print_like_ingramspark():
    assert fmt_in(0.4375) == "0.438" and fmt_in(14.3185) == "14.319" and fmt_in(0.8125) == "0.813"
