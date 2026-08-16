from typing import Dict, List
from engines.template.evidence.models import Evidence, Geometry

class FlapDetector:
    def detect_flaps(self, document_canvas: Evidence, regions: dict) -> Dict[str, List[Evidence]]:
        flaps = {"left": [], "right": []}
        doc = document_canvas.geometry
        if not doc:
            return flaps

        center = doc.width / 2.0
        for ev in regions.get("other", []):
            g: Geometry = ev.geometry
            if not g:
                continue
            if g.height > g.width and g.width > doc.width * 0.1:
                (flaps["left"] if g.x < center else flaps["right"]).append(ev)
        return flaps
