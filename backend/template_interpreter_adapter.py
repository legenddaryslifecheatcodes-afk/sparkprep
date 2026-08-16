"""
Wires SparkPrep's real template-interpretation engine (engines/template/...)
into this app's publisher-template upload flow, replacing the naive
"page size minus 0.25 inches" guess that used to stand in for trim
detection.

The engine reads the template PDF's own text (OCR + embedded) and vector
graphics (bleed/safe-zone color regions, spine geometry) to derive trim
size, spine width, and bleed/safe-zone thickness -- instead of assuming a
fixed 0.125" bleed applies to every publisher and every template. Each
value comes back with a provenance ("extracted" from text, "calculated"
from geometry, or "unresolved" if the template didn't contain enough
evidence) so callers can show confidence rather than presenting a guess
as a fact.
"""
from pathlib import Path

from engines.template.service import TemplateIngestionService
from libs.project_spec.repository import ProjectSpecRepository

_STORAGE_ROOT = Path(__file__).parent / "template_spec_store"
_repository = ProjectSpecRepository(str(_STORAGE_ROOT))
_service = TemplateIngestionService(_repository)


def interpret_publisher_template(template_id: str, file_path: Path, original_filename: str) -> dict:
    """Run the real template interpreter on an uploaded publisher template
    PDF. `template_id` must be unique per upload (the caller's generated
    file_id works well) -- specs are stored immutably, so re-using an id
    for a second upload will raise.

    Returns a plain dict (not the pydantic ProjectSpec) so callers don't
    need to import SparkPrep's models just to read the numbers.
    """
    spec = _service.ingest(template_id, file_path, original_filename)
    return {
        "publisher": spec.publisher,
        "trim_width": _dim(spec.trim_width),
        "trim_height": _dim(spec.trim_height),
        "spine_width": _dim(spec.spine, width_field=True),
        "bleed": _dim(spec.bleed),
        "safe_zone": _dim(spec.safe_zone),
        "document_width_in": spec.analysis.get("templateSpec", {}).get("document", {}).get("width"),
        "document_height_in": spec.analysis.get("templateSpec", {}).get("document", {}).get("height"),
    }


def _dim(d, width_field: bool = False) -> dict:
    value = d.width if width_field else d.value
    return {
        "value": value,
        "unit": d.unit,
        "provenance": d.provenance,
        "confidence": d.confidence,
    }
