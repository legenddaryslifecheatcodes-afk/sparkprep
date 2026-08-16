from pathlib import Path
from collections import Counter
import fitz
from engines.template.evidence.models import Evidence, Geometry

class ImageExtractor:
    """Low-resolution rendered evidence collector.

    This does not treat every pixel as a region. It samples the page and records
    dominant colors, which is useful for templates whose labels/regions are
    encoded visually but not as text.
    """
    def __init__(self, dpi: int = 72, max_colors: int = 12):
        self.dpi = dpi
        self.max_colors = max_colors

    def extract(self, pdf_path: str | Path) -> list[Evidence]:
        evidence: list[Evidence] = []
        with fitz.open(pdf_path) as doc:
            for page_number, page in enumerate(doc):
                pix = page.get_pixmap(dpi=self.dpi, colorspace=fitz.csRGB, alpha=False)
                # Quantize to a small color grid without PIL/numpy so the foundation
                # stays lightweight.
                samples = pix.samples
                counts = Counter()
                for i in range(0, len(samples), 3 * 8):
                    rgb = tuple(samples[i:i+3])
                    if len(rgb) == 3:
                        bucket = tuple((v // 16) * 16 for v in rgb)
                        counts[bucket] += 1
                total = max(1, sum(counts.values()))
                for rgb, count in counts.most_common(self.max_colors):
                    if count / total < 0.01:
                        continue
                    color = "#" + "".join(f"{v:02x}" for v in rgb)
                    evidence.append(Evidence(
                        type="dominant-color",
                        color=color,
                        confidence=min(0.99, 0.50 + count / total),
                        source="image",
                        provenance=f"rendered-pixel-sample:{self.dpi}dpi",
                        raw={"sampleFraction": count / total, "page": page_number},
                    ))
        return evidence
