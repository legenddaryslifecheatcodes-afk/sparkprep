from collections import defaultdict
from engines.template.evidence.models import Evidence

BLUE_FAMILY = {"#a0d0e0", "#b0e0f0", "#a0d0f0", "#b0e0f0"}
PINK_FAMILY = {"#f0d0e0", "#f0e0e0", "#f0d0d0", "#f0e0f0"}

def _area(ev: Evidence) -> float:
    return ev.geometry.width * ev.geometry.height if ev.geometry else 0

class EvidenceFusion:
    def fuse(self, evidence_list: list[Evidence]) -> dict:
        canvases = [e for e in evidence_list if e.type == "document-canvas" and e.geometry]
        shapes = [e for e in evidence_list if e.type == "vector-shape" and e.geometry]
        text = [e for e in evidence_list if e.type in ("text-block", "ocr-text")]
        result = {
            "document_size": self._largest_canvas(canvases),
            "vector_regions": self._regions(shapes),
            "text": [e.raw.get("text", "") for e in text if e.raw],
        }
        result["color_semantics"] = self._infer_color_semantics(shapes)
        return result

    def _largest_canvas(self, canvases):
        if not canvases:
            return None
        e = max(canvases, key=_area)
        return {**e.geometry.model_dump(), "source": e.source, "provenance": e.provenance, "confidence": e.confidence}

    def _regions(self, shapes):
        # Keep large filled shapes; tiny glyph/vector fragments are not useful as regions.
        return [e.model_dump() for e in sorted(shapes, key=_area, reverse=True)[:100]]

    def _infer_color_semantics(self, shapes):
        colors = defaultdict(float)
        for e in shapes:
            if e.color and e.geometry:
                colors[e.color] += _area(e)
        return sorted(({"color": c, "area": a} for c, a in colors.items()), key=lambda x: x["area"], reverse=True)[:12]
