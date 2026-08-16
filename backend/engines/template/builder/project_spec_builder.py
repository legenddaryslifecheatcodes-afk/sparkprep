from datetime import datetime, timezone
from libs.project_spec.models import ProjectSpec, SourceTemplate, DimensionSpec, SpineSpec, ColorSpec, PdfSpec, MarginSpec, Provenance

class ProjectSpecBuilder:
    def build(self, template_id, original_filename, sha256, file_size, extracted, analysis):
        t = extracted.get("templateSpec", {})
        trim = t.get("trim", {})
        spine = t.get("spine", {})
        bleed = t.get("bleed", {})
        safe = t.get("safeZone", {})
        thickness = extracted.get("thickness", {}) or {}
        bleed_thickness = thickness.get("bleed")
        safe_thickness = thickness.get("safeZone")
        now = datetime.now(timezone.utc)

        def prov(obj, fallback="unresolved"):
            return Provenance(
                provenance="extracted" if obj.get("width") is not None else fallback,
                confidence=obj.get("confidence", 0.0),
            )

        return ProjectSpec(
            id=template_id,
            version=1,
            publisher=extracted.get("publisher", "unknown"),
            source_template=SourceTemplate(
                file_id=template_id,
                original_filename=original_filename,
                file_hash=sha256,
                uploaded_at=now,
                file_size=file_size,
            ),
            trim_width=DimensionSpec(
                value=trim.get("width"),
                unit="in",
                provenance="extracted" if trim.get("width") is not None else "unresolved",
                confidence=trim.get("confidence", 0.0),
            ),
            trim_height=DimensionSpec(
                value=trim.get("height"),
                unit="in",
                provenance="extracted" if trim.get("height") is not None else "unresolved",
                confidence=trim.get("confidence", 0.0),
            ),
            bleed=DimensionSpec(
                value=bleed_thickness["value_in"] if bleed_thickness else None,
                unit="in",
                provenance=bleed_thickness["provenance"] if bleed_thickness else ("inferred" if bleed.get("evidenceCount", 0) else "unresolved"),
                confidence=bleed_thickness["confidence"] if bleed_thickness else (0.70 if bleed.get("evidenceCount", 0) else 0.0),
            ),
            safe_zone=DimensionSpec(
                value=safe_thickness["value_in"] if safe_thickness else None,
                unit="in",
                provenance=safe_thickness["provenance"] if safe_thickness else ("inferred" if safe.get("evidenceCount", 0) else "unresolved"),
                confidence=safe_thickness["confidence"] if safe_thickness else (0.70 if safe.get("evidenceCount", 0) else 0.0),
            ),
            spine=SpineSpec(
                width=spine.get("width"),
                unit="in",
                provenance=spine.get("provenance", "unresolved"),
                confidence=spine.get("confidence", 0.0),
            ),
            color=ColorSpec(mode=None, provenance="unresolved", confidence=0.0),
            pdf=PdfSpec(
                standard="PDF",
                provenance="extracted" if extracted.get("fileType") else "unresolved",
                confidence=0.60 if extracted.get("fileType") else 0.0,
            ),
            margins=MarginSpec.unresolved(),
            provenance={
                "trimWidth": prov({**trim, "width": trim.get("width")}),
                "trimHeight": Provenance(provenance="extracted" if trim.get("height") is not None else "unresolved", confidence=trim.get("confidence", 0.0)),
                "bleed": Provenance(
                    provenance=bleed_thickness["provenance"] if bleed_thickness else ("inferred" if bleed.get("evidenceCount", 0) else "unresolved"),
                    confidence=bleed_thickness["confidence"] if bleed_thickness else (0.70 if bleed.get("evidenceCount", 0) else 0.0),
                ),
                "safeZone": Provenance(
                    provenance=safe_thickness["provenance"] if safe_thickness else ("inferred" if safe.get("evidenceCount", 0) else "unresolved"),
                    confidence=safe_thickness["confidence"] if safe_thickness else (0.70 if safe.get("evidenceCount", 0) else 0.0),
                ),
                "spineWidth": Provenance(provenance=spine.get("provenance", "unresolved"), confidence=spine.get("confidence", 0.0)),
                "pdfStandard": Provenance(provenance="extracted" if extracted.get("fileType") else "unresolved", confidence=0.60 if extracted.get("fileType") else 0.0),
            },
            analysis={
                **analysis,
                "templateSpec": t,
                "geometry": {
                    "document": extracted.get("document"),
                    "boundaries": extracted.get("boundaries"),
                    "regions": extracted.get("regions"),
                    "interpretedRegions": extracted.get("interpretedRegions"),
                    "panels": extracted.get("panels"),
                    "folds": extracted.get("folds"),
                    "thickness": extracted.get("thickness"),
                },
                "engine": "template-engine-v0.5",
            },
        )
