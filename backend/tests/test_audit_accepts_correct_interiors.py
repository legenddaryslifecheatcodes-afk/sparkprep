"""THE audit must not report a correct interior as having issues -- the free scan's count comes from it.

Distributors accept an interior at exactly trim size (no bleed), or at trim + bleed on the top, bottom and
outer edge -- never the gutter (IngramSpark's PDF checklist; KDP; SparkPrep's own export). Only Lulu asks for
bleed on all sides.
The audit used to accept only trim + bleed on all sides (6.25"x9.25" for a 6x9), and also added "Output must be PDF/X-1a" to every file, so
SparkPrep's own PDF/X-1a export came back with 3 "issues"."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_aci_"), USE_MEMORY_DB="1", JWT_SECRET="aci-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from file_processor import analyze_file, build_interior_pdf_x1a  # noqa: E402
from pdfx_validator import check_interior_safety_margins  # noqa: E402

from reportlab.lib.units import inch  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402


def manuscript(path, size, left_in=1.1, pages=2):
    c = canvas.Canvas(str(path), pagesize=size)
    for _ in range(pages):
        c.setFont("Times-Roman", 11)
        for i in range(20):
            c.drawString(left_in * inch, (8.0 - i * 0.3) * inch, "Centered body text well inside the safe margins of the page.")
        c.showPage()
    c.save()
    return str(path)


def audit(path):
    return [f["id"] for f in server._audit_file_findings(
        path, analyze_file(path), platform="ingramspark", trim_size="6x9", file_type="interior")]


@pytest.fixture()
def tmp(tmp_path):
    return tmp_path


@pytest.mark.parametrize("bleed", [0.125, 0.0])
def test_sparkprep_own_export_audits_clean(tmp, bleed):
    src = manuscript(tmp / "src.pdf", (6 * inch, 9 * inch))
    out = str(tmp / "out.pdf")
    build_interior_pdf_x1a(src, out, 6, 9, bleed, title="T", author="A")
    assert audit(out) == []


def test_real_problems_are_still_caught(tmp):
    ids = audit(manuscript(tmp / "letter.pdf", (8.5 * inch, 11 * inch)))
    assert {"interior_page_size_mismatch", "pdfx1a_not_declared", "fonts_not_embedded"} <= set(ids)
    assert "resolution_too_low" not in ids and "pdf_x1a_export" not in ids   # vector PDF: no DPI, one PDF/X finding


@pytest.mark.parametrize("size", [(6, 9), (6.125, 9.25)])
def test_every_accepted_page_shape_passes_the_size_check(tmp, size):
    path = manuscript(tmp / "p.pdf", (size[0] * inch, size[1] * inch), left_in=1.1 + (size[0] - 6))
    assert check_interior_safety_margins(path, "IngramSpark", 6, 9) == []


def test_bleed_on_the_gutter_is_only_accepted_for_lulu(tmp):
    path = manuscript(tmp / "p.pdf", (6.25 * inch, 9.25 * inch), left_in=1.35)
    assert [f["id"] for f in check_interior_safety_margins(path, "IngramSpark", 6, 9)] == ["interior_page_size_mismatch"]
    assert check_interior_safety_margins(path, "Lulu", 6, 9, all_sides_bleed_ok=True) == []


def test_margins_on_a_bled_page_are_measured_from_the_trim_edge(tmp):
    # 6.25" all-sides bleed page with no TrimBox: text 0.2" from the page edge is only 0.075" inside the
    # trim edge -- that must be flagged, even though it's 0.2" from the paper edge.
    tight = manuscript(tmp / "tight.pdf", (6.25 * inch, 9.25 * inch), left_in=0.2)
    assert [f["id"] for f in check_interior_safety_margins(tight, "Lulu", 6, 9, all_sides_bleed_ok=True)] == ["interior_safety_margin"]
    # ...and SparkPrep's own export of text that runs to the trim edge is caught too (its TrimBox is used).
    src = manuscript(tmp / "edge.pdf", (6 * inch, 9 * inch), left_in=0.05)
    out = str(tmp / "edge_out.pdf")
    build_interior_pdf_x1a(src, out, 6, 9, 0.125, title="T", author="A")
    assert "interior_safety_margin" in audit(out)
