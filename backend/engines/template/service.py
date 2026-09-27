from pathlib import Path
from engines.template.evidence.collectors import EvidenceCollector
from engines.template.evidence.fusion import EvidenceFusion
from engines.template.extractor.spec_extractor import SpecExtractor
from engines.template.builder.project_spec_builder import ProjectSpecBuilder
from libs.project_spec.repository import ProjectSpecRepository
import hashlib

class TemplateIngestionService:
    def __init__(self, repository: ProjectSpecRepository):
        self.repository = repository
        self.collector = EvidenceCollector()
        self.fusion = EvidenceFusion()
        self.extractor = SpecExtractor()
        self.builder = ProjectSpecBuilder()

    def ingest(self, template_id: str, template_path: Path, original_filename: str):
        evidence = self.collector.collect(template_path)
        fused = self.fusion.fuse(evidence)
        extracted = self.extractor.extract(fused, evidence)
        extracted["publisher"] = self._infer_publisher(fused, template_path)
        analysis = {
            "evidenceCount": len(evidence),
            "textEvidenceCount": sum(e.source == "text" for e in evidence),
            "geometryEvidenceCount": sum(e.source == "geometry" for e in evidence),
            "imageEvidenceCount": sum(e.source == "image" for e in evidence),
            "fusedEvidence": fused,
        }
        sha256 = hashlib.sha256(template_path.read_bytes()).hexdigest()
        spec = self.builder.build(template_id, original_filename, sha256, template_path.stat().st_size, extracted, analysis)
        self.repository.save(spec)
        return spec

    @staticmethod
    def _infer_publisher(fused, template_path: Path = None) -> str:
        """Returns a real PLATFORMS key ("kdp", "ingramspark", ...) when a real,
        verified marker is found, else "unknown" -- never a guess. Each check here
        was found by inspecting an actual template downloaded from that platform's
        own tool, not assumed from a filename or a hand-built template.

        A template's own page TEXT is one place to look (Lightning Source is
        IngramSpark's parent print company and shows up in some of their real
        templates), but it isn't the only place: a file's PDF metadata records
        which software produced it, and KDP's own cover-calculator tool has its
        own distinct signature there (bfo.com's PDF library) that's far more
        reliable than anything in the visible page content -- confirmed against
        a real file downloaded from KDP's calculator on 2026-09-27.
        """
        text = "\n".join(fused.get("text", []))
        if "Lightning Source" in text or "Lightning urce" in text:
            return "ingramspark"
        if template_path is not None:
            try:
                import fitz
                with fitz.open(template_path) as doc:
                    producer = (doc.metadata or {}).get("producer") or ""
                if "bfo.com" in producer:
                    return "kdp"
            except Exception:
                pass
            if TemplateIngestionService._corner_has_lightning_source(template_path):
                return "ingramspark"
        return "unknown"

    @staticmethod
    def _corner_has_lightning_source(template_path: Path) -> bool:
        """Fallback for a real gap found by testing against an actual IngramSpark
        dust-jacket template: the whole-page OCR the pipeline already runs (at
        150 DPI, fast, good enough for everything else it's used for) read
        "Lightning" off their corner logo but garbled "Source" into nothing --
        confirmed directly: 300 DPI over the FULL page reads it correctly but
        takes 90s on a large jacket page, far too slow for what should be a
        quick "upload template, see your specs" step. Both real IngramSpark
        files inspected place this logo in the bottom-left margin, so cropping
        to just that corner before going to 300 DPI keeps the pixel count (and
        so the OCR time) small -- 5s on that same file, confirmed directly --
        while still catching what the fast pass alone misses.
        """
        import fitz
        import pytesseract
        from PIL import Image
        try:
            with fitz.open(template_path) as doc:
                page = doc[0]
                w, h = page.rect.width, page.rect.height
                clip = fitz.Rect(0, h * 0.80, w * 0.22, h)
                pix = page.get_pixmap(dpi=300, colorspace=fitz.csRGB, alpha=False, clip=clip)
                image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            # image_to_string (not used here) breaks each detected region onto its
            # own line in --psm 11 mode, splitting "Lightning" and "Source" apart
            # even when they sit right next to each other -- image_to_data's word
            # list, joined with spaces, matches how the rest of this pipeline
            # already builds text evidence (see OCRExtractor).
            data = pytesseract.image_to_data(image, config="--psm 11", output_type=pytesseract.Output.DICT)
            text = " ".join(t for t in data["text"] if t.strip())
            return "Lightning Source" in text or "Lightning urce" in text
        except Exception:
            return False
