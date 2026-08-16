from pathlib import Path
from engines.template.evidence.models import Evidence

class EvidenceCollector:
    def __init__(self, image_dpi: int = 72, ocr_dpi: int = 150):
        # Imported here, not at module load time: analyzer's own modules
        # import from engines.template.evidence (this package), so
        # importing analyzer's classes at the top of this file creates a
        # circular dependency depending on which side gets imported first.
        # Deferring the import to first use breaks the cycle.
        from engines.template.analyzer.text_extractor import TextExtractor
        from engines.template.analyzer.geometry_extractor import GeometryExtractor
        from engines.template.analyzer.image_extractor import ImageExtractor
        from engines.template.analyzer.ocr_extractor import OCRExtractor

        self.text = TextExtractor()
        self.geometry = GeometryExtractor()
        self.image = ImageExtractor(dpi=image_dpi)
        self.ocr = OCRExtractor(dpi=ocr_dpi)

    def collect(self, pdf_path: str | Path) -> list[Evidence]:
        return [*self.text.extract(pdf_path), *self.geometry.extract(pdf_path), *self.image.extract(pdf_path), *self.ocr.extract(pdf_path)]
