"""The found-and-fixed report must render every repair_log shape that exists in the live
database. Entries written before 2026-09-28 stored `remaining` as whole issue objects
(Agent 3's shape) instead of ids, which crashed the report and with it the export."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from report_export import generate_repair_report_pdf  # noqa: E402

FOUND = [{"id": "colorspace", "label": "Color Space (RGB)", "message": "File is in RGB"},
         {"id": "dpi", "label": "Resolution (250 DPI)", "message": "DPI between 200-299"}]


def _entry(remaining):
    return {"at": "2026-09-27T10:00:00+00:00", "slot": "full_wrap", "status": "partial", "found": FOUND,
            "resolved": ["colorspace"], "remaining": remaining, "health_before": 60, "health_after": 80}


def _render(tmp_path, entry):
    out = tmp_path / "report.pdf"
    generate_repair_report_pdf(repair_log=[entry], final_compliance=[], project_meta={"title": "T", "platform": "IngramSpark",
                                                                                         "trim_size": "6x9"}, output_path=str(out))
    return out


def test_old_entries_with_issue_objects_still_render(tmp_path):
    old = _entry([{"id": "dpi", "label": "Resolution (250 DPI)", "message": "soft", "status": "warning", "engine": "upscale"}])
    assert _render(tmp_path, old).stat().st_size > 0


def test_new_entries_with_ids_render(tmp_path):
    assert _render(tmp_path, _entry(["dpi"])).stat().st_size > 0
