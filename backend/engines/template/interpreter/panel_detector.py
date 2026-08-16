from typing import Dict, Optional
from engines.template.evidence.models import Evidence

class PanelDetector:
    def detect_panels(self, document_canvas: Evidence, trim_regions: Dict[str, list],
                      spine_region: Optional[Evidence], flap_regions: Dict[str, list]):
        panels = {}
        doc = document_canvas.geometry
        if not doc:
            return panels

        trim_ev = trim_regions.get("trim", [None])[0]
        if trim_ev and trim_ev.geometry:
            trim = trim_ev.geometry
            panels["trim"] = {
                "x": trim.x, "y": trim.y,
                "width_in": trim.width / 72.0,
                "height_in": trim.height / 72.0,
                "unit": "in",
            }

        if spine_region and spine_region.geometry:
            spine = spine_region.geometry
            panels["spine"] = {
                "x": spine.x, "y": spine.y,
                "width_in": spine.width / 72.0,
                "height_in": spine.height / 72.0,
                "unit": "in",
            }

        for side in ("left", "right"):
            candidates = flap_regions.get(side) or []
            if not candidates:
                continue
            ev = candidates[0]
            if ev and ev.geometry:
                g = ev.geometry
                panels[f"{side}_flap"] = {
                    "x": g.x, "y": g.y,
                    "width_in": g.width / 72.0,
                    "height_in": g.height / 72.0,
                    "unit": "in",
                }
        return panels
