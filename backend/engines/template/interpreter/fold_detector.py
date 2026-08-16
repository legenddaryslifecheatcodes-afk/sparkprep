from typing import Dict

class FoldDetector:
    def detect_folds(self, panels: Dict[str, dict]):
        folds = {}
        if "spine" in panels:
            s = panels["spine"]
            folds["spine_fold"] = {"position_in": s["width_in"] / 2.0}
        if "left_flap" in panels:
            folds["left_flap_fold"] = {"position_in": panels["left_flap"]["width_in"]}
        if "right_flap" in panels:
            folds["right_flap_fold"] = {"position_in": panels["right_flap"]["width_in"]}
        return folds
