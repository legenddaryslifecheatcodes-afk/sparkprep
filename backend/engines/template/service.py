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
        extracted["publisher"] = self._infer_publisher(fused)
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
    def _infer_publisher(fused):
        text = "\n".join(fused.get("text", []))
        if "Lightning Source" in text or "Lightning urce" in text:
            return "Lightning Source / IngramSpark ecosystem"
        return "unknown"
