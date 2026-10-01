"""IngramSpark hardcover spines, worked out from IngramSpark's own cover templates (owner's
Downloads\\special templates, 2026-09-30) -- every number below is printed on a real template.
Creme is solved (all 7 books); White still asks the customer for the number until its 400/500
templates settle which of two fitting explanations is right."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from print_specs import (calculate_full_cover_dimensions, calculate_spine_width_for_platform,  # noqa: E402
                         even_page_count, fmt_in, spine_source)

CREME = 444
# (pages asked for, spine, case width, jacket width) -- straight off IngramSpark's templates (6x9; heights 10.50 / 9.50)
CREME_TEMPLATES = [
    (74, "0.313", "14.194", "20.438"),
    (108, "0.375", "14.256", None),
    (122, "0.438", "14.319", "20.563"),
    (197, "0.625", "14.506", "20.750"),     # asked for 197 -- IngramSpark builds it as 198
    (300, "0.813", "14.694", "20.938"),
    (500, "1.250", "15.131", "21.375"),
]


@pytest.mark.parametrize("pages,spine,case_w,jacket_w", CREME_TEMPLATES)
def test_creme_spine_and_sizes_match_ingramspark_templates(pages, spine, case_w, jacket_w):
    s, confirmed = calculate_spine_width_for_platform(pages, CREME, "ingramspark", "hardcover_case")
    assert confirmed and fmt_in(s) == spine
    case = calculate_full_cover_dimensions(6, 9, s, 0.625, "hardcover_case", "ingramspark")
    assert (fmt_in(case["total_width"]), fmt_in(case["total_height"])) == (case_w, "10.500")
    js, _ = calculate_spine_width_for_platform(pages, CREME, "ingramspark", "hardcover_jacket")
    assert js == s                                                    # jacket spine = case spine, every template
    if jacket_w:
        jacket = calculate_full_cover_dimensions(6, 9, js, 0.125, "hardcover_jacket", "ingramspark")
        assert (fmt_in(jacket["total_width"]), fmt_in(jacket["total_height"])) == (jacket_w, "9.500")


@pytest.mark.parametrize("pages,spine,case_w,jacket_w", [(74, "0.250", "14.131", "20.375"), (108, "0.313", "14.194", None),
                                                         (200, "0.500", "14.381", "20.625")])
def test_white_sizes_match_templates_given_its_spine(pages, spine, case_w, jacket_w):
    s = float(spine)                                                 # white: the customer enters the template's spine
    case = calculate_full_cover_dimensions(6, 9, s, 0.625, "hardcover_case", "ingramspark")
    assert fmt_in(case["total_width"]) == case_w
    if jacket_w:
        assert fmt_in(calculate_full_cover_dimensions(6, 9, s, 0.125, "hardcover_jacket", "ingramspark")["total_width"]) == jacket_w


def test_white_is_not_guessed_yet():
    assert calculate_spine_width_for_platform(200, 512, "ingramspark", "hardcover_case")[1] is False
    assert spine_source("ingramspark", "hardcover_case", 512) == "template_needed"
    assert spine_source("ingramspark", "hardcover_case", 444) == "ingramspark_templates"


def test_page_count_is_built_even_for_ingramspark():
    assert even_page_count(197, "ingramspark") == 198 and even_page_count(198, "ingramspark") == 198
    assert even_page_count(197, "kdp") == 197


def test_inches_print_like_ingramspark():
    assert fmt_in(0.4375) == "0.438" and fmt_in(14.3185) == "14.319" and fmt_in(0.8125) == "0.813"
