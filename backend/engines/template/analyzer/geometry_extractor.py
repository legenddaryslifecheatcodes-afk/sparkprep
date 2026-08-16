from pathlib import Path
import fitz
from engines.template.evidence.models import Evidence, Geometry


def rgb_hex(color):
    if not color:
        return None
    vals = [max(0, min(1, float(v))) for v in color]
    return "#" + "".join(f"{round(v*255):02x}" for v in vals[:3])

class GeometryExtractor:
    def extract(self, pdf_path: str | Path) -> list[Evidence]:
        evidence: list[Evidence] = []
        with fitz.open(pdf_path) as doc:
            for page_number, page in enumerate(doc):
                rect = page.rect
                evidence.append(Evidence(
                    type="document-canvas",
                    geometry=Geometry(x=0, y=0, width=rect.width, height=rect.height, page=page_number),
                    confidence=1.0,
                    source="geometry",
                    provenance="page.rect",
                ))
                for index, drawing in enumerate(page.get_drawings()):
                    r = drawing.get("rect")
                    if not r or r.width <= 0 or r.height <= 0:
                        continue
                    fill = rgb_hex(drawing.get("fill"))
                    stroke = rgb_hex(drawing.get("color"))
                    # PyMuPDF drawing type is not the string "rect"; filled rectangles
                    # are represented by their item geometry and drawing rect.
                    evidence.append(Evidence(
                        type="vector-shape",
                        geometry=Geometry(x=r.x0, y=r.y0, width=r.width, height=r.height, page=page_number),
                        color=fill or stroke,
                        confidence=0.90 if fill else 0.80,
                        source="geometry",
                        provenance="page.get_drawings",
                        raw={"index": index, "type": drawing.get("type"), "fill": drawing.get("fill"), "stroke": drawing.get("color"), "itemCount": len(drawing.get("items", []))},
                    ))
        return evidence
