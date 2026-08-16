from typing import List
from engines.template.evidence.models import Evidence

# These are starting classifier colors observed in the current fixture set.
# They are intentionally isolated here so they can later be learned from
# template evidence rather than becoming downstream publisher rules.
BLEED_COLORS = {"#0099ff", "#00a0ff", "#abe1fa"}
SAFE_COLORS = {"#ff66cc", "#ff6fcf", "#fde9f1"}
TRIM_COLORS = {"#ffffff", "#fdfdfd"}

class RegionClassifier:
    def classify(self, evidence: List[Evidence]):
        regions = {"bleed": [], "safe": [], "trim": [], "spine": [], "flap": [], "other": []}

        for ev in evidence:
            if ev.type not in {"vector-rectangle", "vector-shape"} or not ev.color:
                continue
            color = ev.color.lower()
            if color in BLEED_COLORS:
                regions["bleed"].append(ev)
            elif color in SAFE_COLORS:
                regions["safe"].append(ev)
            elif color in TRIM_COLORS:
                regions["trim"].append(ev)
            else:
                regions["other"].append(ev)
        return regions
