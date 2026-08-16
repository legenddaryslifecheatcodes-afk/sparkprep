from engines.template.evidence.models import Evidence

PT_PER_INCH = 72.0

class BoundaryDetector:
    def detect_boundaries(self, document_canvas: Evidence, trim: dict, bleed: dict, safe: dict):
        if not document_canvas or not document_canvas.geometry:
            return {}

        doc = document_canvas.geometry
        boundaries = {
            "document": {
                "left_in": 0.0,
                "top_in": 0.0,
                "right_in": doc.width / PT_PER_INCH,
                "bottom_in": doc.height / PT_PER_INCH,
                "width_in": doc.width / PT_PER_INCH,
                "height_in": doc.height / PT_PER_INCH,
            }
        }

        for name, region in (("trim", trim), ("bleed", bleed), ("safe", safe)):
            if region:
                x, y = region["x_pt"], region["y_pt"]
                w, h = region["width_pt"], region["height_pt"]
                boundaries[name] = {
                    "left_in": x / PT_PER_INCH,
                    "top_in": y / PT_PER_INCH,
                    "right_in": (x + w) / PT_PER_INCH,
                    "bottom_in": (y + h) / PT_PER_INCH,
                    "width_in": w / PT_PER_INCH,
                    "height_in": h / PT_PER_INCH,
                }

        return boundaries
