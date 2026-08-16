from typing import List, Optional
from engines.template.evidence.models import Evidence

PT_PER_INCH = 72.0

class RegionInterpreter:
    def _largest(self, regions: List[Evidence]) -> Optional[Evidence]:
        valid = [ev for ev in regions if ev.geometry]
        if not valid:
            return None
        return max(valid, key=lambda ev: ev.geometry.width * ev.geometry.height)

    def _region(self, regions: List[Evidence], provenance: str):
        ev = self._largest(regions)
        if not ev:
            return None
        g = ev.geometry
        return {
            "x_pt": g.x,
            "y_pt": g.y,
            "width_pt": g.width,
            "height_pt": g.height,
            "width_in": g.width / PT_PER_INCH,
            "height_in": g.height / PT_PER_INCH,
            "provenance": provenance,
            "confidence": ev.confidence,
        }

    def interpret_bleed(self, bleed_regions):
        return self._region(bleed_regions, "geometry+image")

    def interpret_safe(self, safe_regions):
        return self._region(safe_regions, "geometry+image")

    def interpret_trim(self, trim_regions):
        return self._region(trim_regions, "geometry")
