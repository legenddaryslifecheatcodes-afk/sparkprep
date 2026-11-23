"""The independent checker (2026-11-23): separate code, a different PDF reader,
its own process -- and when it disagrees, the verified Auto-Fix is rolled back."""
import ast
import io
import os
import sys
from pathlib import Path

import pikepdf
import pytest
from PIL import Image
from reportlab.pdfgen import canvas

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import independent_check  # noqa: E402
from independent_check import compare, run_independent_check  # noqa: E402

APP_MODULES = {p.stem for p in BACKEND.glob("*.py")} | {"autofix_agents", "book_pass", "engines", "libs"}


def test_checker_shares_no_code_with_the_app():
    """Nothing in independent_checker/ may import a SparkPrep module -- that's what makes it independent."""
    for f in (BACKEND / "independent_checker").glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for n in names:
                top = n.split(".")[0]
                assert top == "independent_checker" or top not in APP_MODULES, f"{f.name} imports app module {n}"
                assert top not in ("pikepdf", "fitz", "pymupdf"), f"{f.name} uses the main scanner's PDF library {n}"


def _pdf(path, size=(432, 648), x=72, font="Helvetica"):
    c = canvas.Canvas(str(path), pagesize=size)
    c.setFont(font, 12)
    c.drawString(x, 300, "Some body text for the page")
    c.showPage()
    c.save()


def _checks(result):
    return {c["id"]: c for c in result["checks"]}


def test_runs_in_its_own_process_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    seen = {}
    real_run = independent_check.subprocess.run

    def spy(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return real_run(cmd, **kw)
    monkeypatch.setattr(independent_check.subprocess, "run", spy)
    f = tmp_path / "a.pdf"
    _pdf(f)
    result = run_independent_check(str(f), "pdf", {"trim_w_in": 6, "trim_h_in": 9})
    assert seen["cmd"][1:] == ["-E", "-s", "-m", "independent_checker"]
    assert "MONGO_URL" not in seen["env"] and "ANTHROPIC_API_KEY" not in seen["env"]
    assert _checks(result)["opens"]["passed"] is True


def test_finds_pdf_problems_its_own_way(tmp_path):
    f = tmp_path / "a.pdf"
    _pdf(f, size=(612, 792))  # US Letter, not 6x9
    with pikepdf.open(f, allow_overwriting_input=True) as pdf:
        pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=pikepdf.Array(), D=pikepdf.Dictionary())
        pdf.save(f)
    c = _checks(run_independent_check(str(f), "pdf", {"trim_w_in": 6, "trim_h_in": 9, "bleed_in": 0.125}))
    assert c["fonts"]["passed"] is False and "Helvetica" in c["fonts"]["evidence"]
    assert c["pdfx"]["passed"] is False
    assert c["layers"]["passed"] is False
    assert c["page_size"]["passed"] is False


def test_margins_measured_from_rendered_ink(tmp_path):
    import shutil
    if not shutil.which("gs"):
        pytest.skip("Ghostscript not installed")
    ok, tight = tmp_path / "ok.pdf", tmp_path / "tight.pdf"
    _pdf(ok, x=72)
    _pdf(tight, x=6)   # text starts 1/12" from the edge
    expect = {"trim_w_in": 6, "trim_h_in": 9, "bleed_in": 0.125, "check_margins": True}
    assert _checks(run_independent_check(str(ok), "pdf", expect))["margins"]["passed"] is True
    assert _checks(run_independent_check(str(tight), "pdf", expect))["margins"]["passed"] is False


def test_image_resolution_colour_and_ink(tmp_path):
    f = tmp_path / "c.tif"
    Image.new("CMYK", (600, 900), (255, 255, 255, 255)).save(f)
    c = _checks(run_independent_check(str(f), "image", {"w_in": 6, "h_in": 9, "ink_limit_pct": 300}))
    assert c["resolution"]["passed"] is False and "100 DPI" in c["resolution"]["evidence"]
    assert c["color_cmyk"]["passed"] is True
    assert c["ink_limit"]["passed"] is False


def test_unreadable_or_crashed_checker_never_counts_as_a_pass(tmp_path):
    f = tmp_path / "junk.pdf"
    f.write_bytes(b"not a pdf")
    result = run_independent_check(str(f), "pdf", {})
    verdict = compare([{"id": "fonts_not_embedded"}], {}, result)
    assert verdict["agree"] is False
    assert compare([], {}, {"checks": [], "error": "boom"})["agree"] is False


def test_compare_blocks_unfixed_targets_and_regressions():
    before = {"checks": [{"id": "fonts", "label": "Fonts", "passed": False, "evidence": "x"},
                         {"id": "layers", "label": "Layers", "passed": True, "evidence": "none"},
                         {"id": "opens", "label": "Opens", "passed": True, "evidence": ""}]}
    fixed = {"checks": [{"id": "fonts", "label": "Fonts", "passed": True, "evidence": "ok"},
                        {"id": "layers", "label": "Layers", "passed": True, "evidence": "none"},
                        {"id": "opens", "label": "Opens", "passed": True, "evidence": ""}]}
    assert compare([{"id": "fonts_not_embedded"}], before, fixed)["agree"] is True
    still = {"checks": [dict(fixed["checks"][0], passed=False)] + fixed["checks"][1:]}
    assert compare([{"id": "fonts_not_embedded"}], before, still)["agree"] is False
    broke = {"checks": fixed["checks"][:1] + [dict(fixed["checks"][1], passed=False)] + fixed["checks"][2:]}
    v = compare([{"id": "fonts_not_embedded"}], before, broke)
    assert v["agree"] is False and v["regressions"][0]["id"] == "layers"
    # A finding it doesn't measure (cover text margins, read by OCR) is listed, not faked.
    assert compare([{"id": "cover_safety_margin"}], before, fixed)["not_judged"] == ["cover_safety_margin"]
