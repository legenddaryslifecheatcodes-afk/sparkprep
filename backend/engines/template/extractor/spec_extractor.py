import re
from engines.template.evidence.models import Evidence
from engines.template.interpreter.region_classifier import RegionClassifier
from engines.template.interpreter.region_interpreter import RegionInterpreter
from engines.template.interpreter.boundary_detector import BoundaryDetector
from engines.template.interpreter.spine_detector import SpineDetector
from engines.template.interpreter.flap_detector import FlapDetector
from engines.template.interpreter.panel_detector import PanelDetector
from engines.template.interpreter.fold_detector import FoldDetector
from engines.template.interpreter.thickness_calculator import ThicknessCalculator

_DIM = r"(?P<w>\d+(?:\.\d+)?)\s*[x×]\s*(?P<h>\d+(?:\.\d+)?)"
_MM_DIM = r"(?P<w>\d+(?:\.\d+)?)\s*mm\s*[x×]\s*(?P<h>\d+(?:\.\d+)?)\s*mm"
MM_PER_INCH = 25.4
# Publisher trim sizes are conventionally quoted to the nearest 1/8in.
# Rounding a millimeter-derived value to this increment corrects for OCR
# and unit-conversion noise without hard-coding any specific trim size.
TRIM_ROUNDING_IN = 0.125


def _round_to_increment(value: float, increment: float) -> float:
    return round(round(value / increment) * increment, 3)

class SpecExtractor:
    def extract(self, fused_evidence: dict, raw_evidence: list[Evidence]):
        document_canvas = self._document_canvas(fused_evidence)
        regions = RegionClassifier().classify(raw_evidence)
        spine_ev = SpineDetector().detect_spine(regions)
        flaps = FlapDetector().detect_flaps(document_canvas, regions) if document_canvas else {"left": [], "right": []}
        panels = PanelDetector().detect_panels(document_canvas, regions, spine_ev, flaps) if document_canvas else {}
        folds = FoldDetector().detect_folds(panels)

        interpreter = RegionInterpreter()
        bleed = interpreter.interpret_bleed(regions["bleed"])
        safe = interpreter.interpret_safe(regions["safe"])

        text = "\n".join(fused_evidence.get("text", []))
        ocr = self._extract_text_specs(text)

        trim = self._resolve_trim(regions["trim"], bleed, ocr, interpreter)
        boundaries = BoundaryDetector().detect_boundaries(document_canvas, trim, bleed, safe) if document_canvas else {}
        thickness = ThicknessCalculator().calculate(
            boundaries, {"trim": trim, "bleed": bleed, "safe": safe}
        )
        # These publisher templates print their own legend: bleed (blue) is
        # the outermost printable area, and safe (pink) must stay *inside*
        # the blue -- i.e. safe nests inside bleed directly, not inside a
        # separate trim cut line (there is no filled trim shape to measure
        # against; trim marks are thin black crop-lines, not a region).
        # Measure safe-zone thickness against bleed instead of trim so it
        # reflects what the template actually says, reusing the same
        # generic edge-difference calculator with bleed substituted in as
        # the reference boundary.
        if boundaries.get("bleed") and boundaries.get("safe"):
            safe_result = ThicknessCalculator().calculate(
                {"trim": boundaries["bleed"], "safe": boundaries["safe"]},
                {"trim": bleed, "safe": safe},
            )
            thickness["safeZone"] = safe_result["safeZone"]

        document = fused_evidence.get("document_size")
        if document:
            document = dict(document)
            document["width_in"] = document["width"] / 72.0
            document["height_in"] = document["height"] / 72.0

        spine = ocr.get("spine_width")
        if spine is None and "spine" in panels:
            spine = panels["spine"]["width_in"]

        bleed_thickness = thickness.get("bleed")
        safe_thickness = thickness.get("safeZone")

        return {
            "document": document,
            "boundaries": boundaries,
            "panels": panels,
            "folds": folds,
            "thickness": thickness,
            "regions": {
                key: [e.model_dump(mode="json") for e in value]
                for key, value in regions.items()
            },
            "interpretedRegions": {"bleed": bleed, "safe": safe, "trim": trim},
            "templateSpec": {
                "document": {
                    "width": document["width_in"] if document else None,
                    "height": document["height_in"] if document else None,
                    "unit": "in",
                },
                "trim": {
                    "width": ocr.get("trim_width"),
                    "height": ocr.get("trim_height"),
                    "unit": "in" if ocr.get("trim_width") else None,
                    "provenance": "extracted" if ocr.get("trim_width") else "unresolved",
                    "confidence": 0.93 if ocr.get("trim_width") else 0.0,
                },
                "spine": {
                    "width": spine,
                    "unit": "in" if spine is not None else None,
                    "provenance": "extracted" if ocr.get("spine_width") is not None else ("geometry" if spine is not None else "unresolved"),
                    "confidence": 0.95 if ocr.get("spine_width") is not None else (0.90 if spine is not None else 0.0),
                },
                "bleed": {
                    "type": "blue-region",
                    "evidenceCount": len(regions["bleed"]),
                    "thicknessIn": bleed_thickness["value_in"] if bleed_thickness else None,
                    "provenance": bleed_thickness["provenance"] if bleed_thickness else ("inferred" if regions["bleed"] else "unresolved"),
                    "confidence": bleed_thickness["confidence"] if bleed_thickness else (0.70 if regions["bleed"] else 0.0),
                },
                "safeZone": {
                    "type": "pink-region",
                    "evidenceCount": len(regions["safe"]),
                    "thicknessIn": safe_thickness["value_in"] if safe_thickness else None,
                    "provenance": safe_thickness["provenance"] if safe_thickness else ("inferred" if regions["safe"] else "unresolved"),
                    "confidence": safe_thickness["confidence"] if safe_thickness else (0.70 if regions["safe"] else 0.0),
                },
                "flap": ocr.get("flap"),
                "panel": ocr.get("panel"),
                "contentType": ocr.get("content_type"),
                "paperType": ocr.get("paper_type"),
                "pageCount": ocr.get("page_count"),
                "fileType": ocr.get("file_type"),
                "boundaries": boundaries,
            },
        }

    @staticmethod
    def _resolve_trim(trim_regions, bleed, ocr, interpreter):
        """Prefer a trim rectangle computed from the extracted trim size,
        centered within the detected bleed region. These publisher
        templates don't draw an explicit filled trim-line shape -- the
        naive largest-white-shape match (interpret_trim) tends to latch
        onto an unrelated small white element (e.g. a barcode placeholder)
        instead. Centering a known trim size inside bleed is the standard
        print convention (bleed extends a uniform allowance beyond the cut
        line on every side) and uses only values already independently
        verified: the text-extracted trim size and the geometry-detected
        bleed region. Falls back to the geometry-only match when either
        input is missing, or when bleed isn't actually larger than the
        extracted trim (a sign one of those inputs is itself wrong, where
        centering would only produce a worse, nonsensical box).
        """
        trim_width, trim_height = ocr.get("trim_width"), ocr.get("trim_height")
        if trim_width and trim_height and bleed:
            width_pt, height_pt = trim_width * 72.0, trim_height * 72.0
            if bleed["width_pt"] >= width_pt and bleed["height_pt"] >= height_pt:
                return {
                    "x_pt": bleed["x_pt"] + (bleed["width_pt"] - width_pt) / 2,
                    "y_pt": bleed["y_pt"] + (bleed["height_pt"] - height_pt) / 2,
                    "width_pt": width_pt, "height_pt": height_pt,
                    "width_in": trim_width, "height_in": trim_height,
                    "provenance": "calculated",
                    "confidence": 0.85,
                }
        return interpreter.interpret_trim(trim_regions)

    @staticmethod
    def _find_unlabeled_inch_value(compact: str) -> float | None:
        """First bare decimal number that isn't part of a W x H pair, an
        mm value, or a labeled flap/wrap figure. See the comment where
        this is called for why that identifies spine width on these
        templates, and why it's a heuristic rather than a guarantee."""
        for m in re.finditer(r"\d+\.\d+", compact):
            start, end = m.span()
            before, after = compact[:start], compact[end:]
            if re.match(r"\s*[x×]\s*\d", after):
                continue  # first half of an "N x N" pair
            if re.search(r"[x×]\s*$", before[-4:]):
                continue  # second half of an "N x N" pair
            if re.match(r"\s*mm\b", after, re.I):
                continue  # already-labeled millimeter value
            if re.match(r"\s*(flap|wrap)\b", after, re.I):
                continue  # labeled flap/wrap measurement
            return float(m.group())
        return None

    @staticmethod
    def _document_canvas(fused):
        d = fused.get("document_size")
        if not d:
            return None
        class Canvas:
            geometry = type("Geometry", (), {
                "x": d.get("x", 0), "y": d.get("y", 0),
                "width": d["width"], "height": d["height"]
            })()
        return Canvas()

    @staticmethod
    def _extract_text_specs(text: str):
        result = {}
        compact = re.sub(r"\s+", " ", text)

        # Trim size: prefer an explicit "Trim Size: W x H" label -- it is
        # already in inches and its digit order is unambiguous. Fall back to
        # a millimeter dimension pair (publisher templates commonly print
        # trim in both units, and OCR sometimes drops a rotated/small
        # label). The mm pair's printed order isn't reliably width-first,
        # so we assign the smaller value to width and the larger to height:
        # a case-bound/dust-jacketed book cover is essentially always
        # portrait, so this holds for standard trim sizes without hardcoding
        # any of them.
        m = re.search(r"Trim\s+Size\s*:?\s*" + _DIM, compact, re.I)
        if m:
            result["trim_width"] = float(m.group("w"))
            result["trim_height"] = float(m.group("h"))
        else:
            m = re.search(_MM_DIM, compact, re.I)
            if m:
                a_in = float(m.group("w")) / MM_PER_INCH
                b_in = float(m.group("h")) / MM_PER_INCH
                width, height = sorted(
                    _round_to_increment(v, TRIM_ROUNDING_IN) for v in (a_in, b_in)
                )
                result["trim_width"] = width
                result["trim_height"] = height

        # Spine width: trust an explicit "Spine ..." label when present.
        m = re.search(
            r"Spine(?:\s*Width)?\s*:?\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mm|in|inch|inches)\b",
            compact, re.I,
        )
        if m:
            value = float(m.group("value"))
            unit = m.group("unit").lower()
            result["spine_width"] = round(value / MM_PER_INCH, 3) if unit == "mm" else round(value, 3)
        else:
            # These templates print the spine width as a bare inch figure
            # with no label of its own (unlike flap/wrap, which are always
            # labeled) -- geometrically it's the one measurement on the
            # diagram that isn't a width-x-height pair, an mm value, or a
            # labeled flap/wrap figure. This is a heuristic, not a
            # guarantee: SpineDetector's geometry-based detection (used as
            # the fallback in extract()) remains the more principled source
            # and should be preferred if it can be made reliable on real
            # (glyph-noisy) vector geometry.
            value = SpecExtractor._find_unlabeled_inch_value(compact)
            if value is not None:
                result["spine_width"] = value

        m = re.search(r"(?P<w>\d+(?:\.\d+)?)\s+flap\b", compact, re.I)
        if m:
            result["flap"] = {"width": float(m.group("w")), "unit": "in"}

        # Panel dimensions have no reliable text label on these templates.
        # Rather than match one fixture's specific numbers, leave this
        # unresolved from text; PanelDetector already derives panel
        # geometry independently from the vector evidence.

        for key, pattern in {
            "content_type": r"Content Type:\s*(.*?)(?=Page Count:|Paper Type:|Trim Size:|File Type:|$)",
            "paper_type": r"Paper Type:\s*(.*?)(?=ISBN:|Trim Size:|File Type:|$)",
            "file_type": r"File Type:\s*(.*?)(?=Request ID:|Bleed Artwork|Lightning|Dust Jacket|Document Size:|$)",
        }.items():
            m = re.search(pattern, text, re.I)
            if m:
                result[key] = m.group(1).strip()
        m = re.search(r"Page Count:\s*(\d+)", text, re.I)
        if m:
            result["page_count"] = int(m.group(1))
        return result
