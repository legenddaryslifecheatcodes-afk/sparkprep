from typing import List, Optional
from engines.template.evidence.models import Evidence

class SpineDetector:
    def detect_spine(self, regions: dict) -> Optional[Evidence]:
        candidates: List[Evidence] = regions.get("other", [])
        best = None
        best_ratio = 0.0
        for ev in candidates:
            if not ev.geometry:
                continue
            g = ev.geometry
            if g.height > g.width and g.width > 0:
                ratio = g.height / g.width
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = ev
        return best
