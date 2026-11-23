"""Auto-Fix embeds fonts and adds the print colour profile itself (2026-11-23):
these used to be left to the customer as manual instructions."""
import shutil

import pikepdf
import pytest
from reportlab.pdfgen import canvas

from file_processor import stamp_pdfx_output_intent
from ghostscript_engine import convert_to_pdfx1a, find_ghostscript, font_substitutions
from pdfx_validator import run_pdf_structure_audit

needs_gs = pytest.mark.skipif(not find_ghostscript(), reason="Ghostscript not installed")


def _pdf_with_unembedded_fonts(path, rename_times_to=None):
    c = canvas.Canvas(str(path), pagesize=(432, 648))
    c.setFont("Helvetica", 12)
    c.drawString(72, 500, "Hello world")
    c.setFont("Times-Roman", 12)
    c.drawString(72, 450, "Times text")
    c.showPage()
    c.save()
    if rename_times_to:
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            for _, f in pdf.pages[0].Resources.Font.items():
                if str(f.BaseFont) == "/Times-Roman":
                    f.BaseFont = pikepdf.Name("/" + rename_times_to)
            pdf.save(path)


def _ids(path):
    return {f["id"] for f in run_pdf_structure_audit(str(path))}


def test_font_substitutions_ignores_standard_fonts_and_real_matches():
    out = (
        "Loading font Helvetica (or substitute) from /usr/share/gs/Font/NimbusSans-Regular\n"
        "Loading font ABCDEF+Garamond (or substitute) from /usr/share/gs/Font/NimbusSans-Regular\n"
        "Loading font NimbusRoman-Bold (or substitute) from /usr/share/gs/Font/NimbusRoman-Bold\n"
    )
    assert font_substitutions(out) == [{"font": "Garamond", "substitute": "NimbusSans-Regular"}]


@needs_gs
def test_autofix_pass_embeds_fonts_and_adds_colour_profile(tmp_path):
    src, out = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _pdf_with_unembedded_fonts(src)
    assert {"fonts_not_embedded", "pdfx1a_not_declared"} <= _ids(src)

    result = convert_to_pdfx1a(str(src), str(out))
    assert result["font_substitutions"] == []  # standard fonts: exact-width copies, nothing to warn about
    stamp = stamp_pdfx_output_intent(str(out), "GRACoL 2013")
    assert _ids(out) == set()
    with pikepdf.open(out) as pdf:
        intent = pdf.Root.OutputIntents[0]
        assert str(intent.OutputConditionIdentifier) == "CGATS TR 006 (GRACoL2013)"
        assert str(pdf.Root.GTS_PDFXVersion) == "PDF/X-1a:2001"
    assert stamp["color_profile"] == "Coated GRACoL 2013"


@needs_gs
def test_missing_custom_font_is_embedded_but_reported(tmp_path):
    src, out = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _pdf_with_unembedded_fonts(src, rename_times_to="GaramondPremrPro")
    result = convert_to_pdfx1a(str(src), str(out))
    assert "fonts_not_embedded" not in _ids(out)
    assert [x["font"] for x in result["font_substitutions"]] == ["GaramondPremrPro"]


def test_stamp_keeps_page_geometry(tmp_path):
    src = tmp_path / "in.pdf"
    _pdf_with_unembedded_fonts(src)
    with pikepdf.open(src) as pdf:
        before = [list(p.mediabox) for p in pdf.pages]
    stamp_pdfx_output_intent(str(src))
    with pikepdf.open(src) as pdf:
        assert [list(p.mediabox) for p in pdf.pages] == before
        assert [list(p.trimbox) for p in pdf.pages] == before
        assert "/DestOutputProfile" not in pdf.Root.OutputIntents[0]
        assert str(pdf.Root.OutputIntents[0].RegistryName) == "http://www.color.org"
