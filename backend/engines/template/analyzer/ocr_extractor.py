from pathlib import Path
import fitz
from PIL import Image
import pytesseract
from engines.template.evidence.models import Evidence, Geometry

class OCRExtractor:
    def __init__(self, dpi: int = 150):
        self.dpi = dpi

    def extract(self, pdf_path: str | Path) -> list[Evidence]:
        evidence = []
        with fitz.open(pdf_path) as doc:
            for page_number, page in enumerate(doc):
                pix = page.get_pixmap(dpi=self.dpi, colorspace=fitz.csRGB, alpha=False)
                image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                data = pytesseract.image_to_data(image, config="--psm 11", output_type=pytesseract.Output.DICT)
                words = []
                for i, text in enumerate(data["text"]):
                    text = text.strip()
                    if not text:
                        continue
                    conf = float(data["conf"][i]) / 100.0
                    x, y, w, h = [int(data[k][i]) for k in ("left", "top", "width", "height")]
                    words.append(text)
                    evidence.append(Evidence(
                        type="ocr-word",
                        geometry=Geometry(x=x * 72 / self.dpi, y=y * 72 / self.dpi, width=w * 72 / self.dpi, height=h * 72 / self.dpi, page=page_number),
                        confidence=max(0.0, min(1.0, conf)),
                        source="image",
                        provenance=f"tesseract:{self.dpi}dpi",
                        raw={"text": text},
                    ))
                if words:
                    evidence.append(Evidence(
                        type="ocr-text",
                        confidence=0.85,
                        source="image",
                        provenance=f"tesseract:{self.dpi}dpi",
                        raw={"text": " ".join(words)},
                    ))
        return evidence
