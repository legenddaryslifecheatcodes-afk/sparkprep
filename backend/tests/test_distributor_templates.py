"""Every cover size SparkPrep builds must match the distributor's OWN generated template -- each distributor
keeps its own spec, so each is pinned here against a real template the owner received (6x9 book, 74 pages
unless noted). Picking one distributor must only ever apply that distributor's numbers."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from print_specs import (PAPER_TYPES, calculate_full_cover_dimensions,  # noqa: E402
                         calculate_spine_width_for_platform, paper_ppi)


def cover(platform, binding, spine):
    f = calculate_full_cover_dimensions(6, 9, spine, 0.125, binding, platform)
    return round(f["total_width"], 3), round(f["total_height"], 3), f


@pytest.mark.parametrize("spine,expected", [
    (0.313, (14.194, 10.5)),   # IngramSpark case laminate template, Creme, 74 pages
    (0.375, (14.256, 10.5)),   # IngramSpark case laminate template, Creme, 108 pages
])
def test_ingramspark_case_laminate(spine, expected):
    # Exact to the thousandth -- the size SparkPrep shows the customer must read the same as the template.
    assert cover("ingramspark", "hardcover_case", spine)[:2] == expected


def test_ingramspark_dust_jacket():
    assert cover("ingramspark", "hardcover_jacket", 0.313)[:2] == (20.438, 9.5)


def test_barnes_noble_dust_jacket():
    w, h, f = cover("barnes_noble", "hardcover_jacket", 0.312)
    assert (w, h) == (20.451, 9.5)
    assert abs(f["spine_x"] - 10.066) < 0.005 and abs(f["front_x"] - 10.378) < 0.005   # spine where B&N draws it


def test_lulu_dust_jacket():
    spine, confirmed = calculate_spine_width_for_platform(74, paper_ppi(PAPER_TYPES["white_50lb"], "lulu"), "lulu", "hardcover_jacket")
    assert (spine, confirmed) == (0.25, True)                   # Lulu's hardcover table, not the paperback formula
    w, h, f = cover("lulu", "hardcover_jacket", spine)
    assert (w, h) == (20.0, 9.75)
    assert (f["back_flap_x"], f["back_x"], f["spine_x"], f["front_x"], f["front_flap_x"]) == (0.25, 3.75, 9.875, 10.125, 16.5)


def test_hardcover_spines_that_are_not_published_are_not_guessed():
    # IngramSpark's own templates: Creme, 74 pages = 0.313", 108 pages = 0.375" -- no pages-per-inch formula
    # gives both, so SparkPrep must flag its number as unconfirmed (and ask the user for the real one).
    for platform in ("ingramspark", "kdp", "barnes_noble"):
        for binding in ("hardcover_case", "hardcover_jacket"):
            ppi = 512 if platform == "ingramspark" else 444   # IngramSpark Creme (444) is solved -- see test_ingramspark_hardcover_spine
            assert calculate_spine_width_for_platform(74, ppi, platform, binding)[1] is False
