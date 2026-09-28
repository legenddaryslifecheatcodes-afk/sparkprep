"""Hardcover cover geometry pinned to real IngramSpark Cover Template Generator
output (6x9, 74 pages, cream, spine 0.313", request CSS5443850). Every number
below was measured from the colored zones inside the actual template PDFs."""
import pytest

from print_specs import calculate_full_cover_dimensions


def test_dust_jacket_matches_ingramspark_template():
    d = calculate_full_cover_dimensions(6.0, 9.0, 0.313, 0.125, "hardcover_jacket", "ingramspark")
    assert d["total_width"] == pytest.approx(20.438, abs=0.002)
    assert d["total_height"] == pytest.approx(9.5, abs=0.002)
    assert d["panel_width"] == pytest.approx(6.438, abs=0.001)
    assert d["panel_height"] == pytest.approx(9.25, abs=0.001)
    # Template zone starts at x=3.062"; panel edges there: back 6.687, spine 13.125/13.438, front flap fold 20.125
    origin = 3.062
    assert origin + d["back_x"] == pytest.approx(6.687, abs=0.002)
    assert origin + d["spine_x"] == pytest.approx(13.125, abs=0.002)
    assert origin + d["front_x"] == pytest.approx(13.438, abs=0.002)
    assert origin + d["front_flap_x"] == pytest.approx(20.125, abs=0.002)


def test_case_laminate_matches_ingramspark_template():
    d = calculate_full_cover_dimensions(6.0, 9.0, 0.313, 0.625, "hardcover_case", "ingramspark")
    assert d["total_width"] == pytest.approx(14.194, abs=0.002)
    assert d["total_height"] == pytest.approx(10.5, abs=0.002)
    # Template zone starts at x=3.307"; spine sits between the two blue panels at 10.247-10.561
    origin = 3.307
    assert origin + d["spine_x"] == pytest.approx(10.247, abs=0.003)
    assert origin + d["front_x"] == pytest.approx(10.561, abs=0.003)
