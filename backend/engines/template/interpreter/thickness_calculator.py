from typing import Optional

# Bleed extends outward past trim; safe zone insets inward from trim.
# Edges within this tolerance are treated as a single uniform thickness
# rather than four independently-measured edges.
UNIFORM_TOLERANCE_IN = 0.01

# Asymmetric edges are still reported (never silently invented or hidden),
# but the confidence is discounted since a single scalar thickness is a
# weaker summary of the geometry when the edges disagree.
ASYMMETRY_CONFIDENCE_PENALTY = 0.75


class ThicknessCalculator:
    """Derives bleed extension and safe-zone inset thickness.

    This reads the normalized document/trim/bleed/safe boundary edges that
    BoundaryDetector already produces and takes the differences between
    them. It introduces no new evidence and no publisher-specific numeric
    assumptions -- it is purely arithmetic over boundaries already backed
    by geometry evidence. When trim or the region in question is missing,
    thickness is left unresolved rather than guessed.
    """

    def calculate(self, boundaries: dict, interpreted_regions: Optional[dict] = None) -> dict:
        interpreted_regions = interpreted_regions or {}
        result = {"bleed": None, "safeZone": None}

        trim = boundaries.get("trim") if boundaries else None
        if not trim:
            return result

        bleed = boundaries.get("bleed")
        if bleed:
            result["bleed"] = self._edge_thickness(
                trim, bleed, outward=True,
                confidences=(
                    self._confidence(interpreted_regions, "trim"),
                    self._confidence(interpreted_regions, "bleed"),
                ),
            )

        safe = boundaries.get("safe")
        if safe:
            result["safeZone"] = self._edge_thickness(
                trim, safe, outward=False,
                confidences=(
                    self._confidence(interpreted_regions, "trim"),
                    self._confidence(interpreted_regions, "safe"),
                ),
            )

        return result

    @staticmethod
    def _confidence(interpreted_regions: dict, key: str):
        region = interpreted_regions.get(key)
        return region.get("confidence") if region else None

    def _edge_thickness(self, trim: dict, other: dict, outward: bool, confidences) -> dict:
        if outward:
            # bleed sits outside trim on every edge
            edges = {
                "left": trim["left_in"] - other["left_in"],
                "top": trim["top_in"] - other["top_in"],
                "right": other["right_in"] - trim["right_in"],
                "bottom": other["bottom_in"] - trim["bottom_in"],
            }
        else:
            # safe zone sits inside trim on every edge
            edges = {
                "left": other["left_in"] - trim["left_in"],
                "top": other["top_in"] - trim["top_in"],
                "right": trim["right_in"] - other["right_in"],
                "bottom": trim["bottom_in"] - other["bottom_in"],
            }

        values = list(edges.values())
        average = sum(values) / len(values)
        uniform = (max(values) - min(values)) <= UNIFORM_TOLERANCE_IN

        known = [c for c in confidences if c is not None]
        confidence = min(known) if known else 0.0
        if not uniform:
            confidence *= ASYMMETRY_CONFIDENCE_PENALTY

        return {
            "value_in": round(average, 4),
            "edges_in": {k: round(v, 4) for k, v in edges.items()},
            "uniform": uniform,
            "provenance": "calculated",
            "confidence": round(confidence, 4),
        }
