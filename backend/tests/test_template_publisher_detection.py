"""Publisher (distributor) detection for uploaded cover/jacket templates.

engines/template/service.py::_infer_publisher had zero test coverage before
this -- these lock in the real markers found by inspecting actual templates
downloaded from KDP and IngramSpark (2026-09-27), and the routing between the
fast whole-page check and the slower corner-crop OCR fallback.
"""
import pikepdf
import pytesseract
from reportlab.pdfgen import canvas

from engines.template.service import TemplateIngestionService


def _blank_pdf(path, w=1476, h=684):
    c = canvas.Canvas(str(path), pagesize=(w, h))
    c.showPage()  # reportlab writes zero pages without this, even with nothing drawn
    c.save()


def _set_producer(path, producer):
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.docinfo["/Producer"] = producer
        pdf.save(path)


def test_ingramspark_detected_from_fast_path_text():
    # No template_path needed -- this is the cheap whole-page text check,
    # already computed before _infer_publisher is ever called.
    fused = {"text": ["some page content", "Lightning Source", "more content"]}
    assert TemplateIngestionService._infer_publisher(fused) == "ingramspark"


def test_ingramspark_detected_from_known_ocr_garble_variant():
    fused = {"text": ["Lightning urce"]}
    assert TemplateIngestionService._infer_publisher(fused) == "ingramspark"


def test_kdp_detected_from_producer_metadata(tmp_path):
    path = tmp_path / "kdp.pdf"
    _blank_pdf(path)
    _set_producer(path, "http://bfo.com/products/pdf?version=2.29.1-r49327M")
    assert TemplateIngestionService._infer_publisher({"text": []}, path) == "kdp"


def test_kdp_metadata_match_skips_the_slow_corner_ocr_fallback(tmp_path, monkeypatch):
    path = tmp_path / "kdp.pdf"
    _blank_pdf(path)
    _set_producer(path, "http://bfo.com/products/pdf?version=2.29.1-r49327M")

    def fail_if_called(*a, **k):
        raise AssertionError("corner OCR fallback should not run once metadata already matched")
    monkeypatch.setattr(TemplateIngestionService, "_corner_has_lightning_source", staticmethod(fail_if_called))
    assert TemplateIngestionService._infer_publisher({"text": []}, path) == "kdp"


def test_ingramspark_detected_via_corner_fallback_when_fast_path_misses(tmp_path, monkeypatch):
    """Reproduces the real gap found in a real IngramSpark dust-jacket template:
    the pipeline's normal 150 DPI whole-page OCR read "Lightning" off the
    corner logo but garbled "Source" into nothing, so the fast text check
    alone missed it. The corner-crop fallback (cropped, then re-read at
    300 DPI) is what catches it -- this pins that routing without depending
    on real OCR accuracy, which the dedicated corner-fallback test covers."""
    path = tmp_path / "jacket.pdf"
    _blank_pdf(path, w=1476, h=684)  # no bfo.com producer -- metadata check falls through

    monkeypatch.setattr(pytesseract, "image_to_data", lambda image, **k: {
        "text": ["Lightning", "Source", "Dust", "Jacket", "Cover", "Template"],
    })
    assert TemplateIngestionService._infer_publisher({"text": []}, path) == "ingramspark"


def test_returns_unknown_when_nothing_matches(tmp_path, monkeypatch):
    path = tmp_path / "unrelated.pdf"
    _blank_pdf(path)
    monkeypatch.setattr(pytesseract, "image_to_data", lambda image, **k: {"text": ["just", "some", "cover", "art"]})
    assert TemplateIngestionService._infer_publisher({"text": []}, path) == "unknown"


def test_corner_ocr_failure_does_not_raise(tmp_path, monkeypatch):
    """Fingerprinting errors must never block the customer's upload -- same
    fail-open principle used everywhere else this app runs OCR."""
    path = tmp_path / "broken.pdf"
    _blank_pdf(path)

    def boom(*a, **k):
        raise RuntimeError("tesseract exploded")
    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    assert TemplateIngestionService._infer_publisher({"text": []}, path) == "unknown"
