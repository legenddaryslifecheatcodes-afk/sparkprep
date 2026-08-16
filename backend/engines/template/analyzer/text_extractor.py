from pathlib import Path
import fitz
from engines.template.evidence.models import Evidence, Geometry

class TextExtractor:
    def extract(self, pdf_path: str | Path) -> list[Evidence]:
        evidence: list[Evidence] = []
        with fitz.open(pdf_path) as doc:
            for page_number, page in enumerate(doc):
                blocks = page.get_text("blocks")
                for block in blocks:
                    text = str(block[4]).strip()
                    if not text:
                        continue
                    evidence.append(Evidence(
                        type="text-block",
                        geometry=Geometry(x=block[0], y=block[1], width=block[2]-block[0], height=block[3]-block[1], page=page_number),
                        confidence=0.98,
                        source="text",
                        provenance="page.get_text(blocks)",
                        raw={"text": text},
                    ))
        return evidence
