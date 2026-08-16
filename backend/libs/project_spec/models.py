from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

ProvenanceType = Literal["extracted", "calculated", "inferred", "user_confirmed", "unresolved"]

class Provenance(BaseModel):
    provenance: ProvenanceType
    confidence: float = Field(ge=0.0, le=1.0)
    source_text: str | None = None

    @classmethod
    def from_field(cls, field):
        return cls(provenance=field.provenance, confidence=field.confidence, source_text=field.source_text)

class DimensionSpec(BaseModel):
    value: float | None = None
    unit: str | None = None
    provenance: ProvenanceType = "unresolved"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @classmethod
    def from_field(cls, field):
        return cls(value=field.value, unit=field.unit, provenance=field.provenance, confidence=field.confidence)

class SpineSpec(BaseModel):
    width: float | None = None
    unit: str | None = None
    provenance: ProvenanceType = "unresolved"
    confidence: float = 0.0

    @classmethod
    def from_field(cls, field):
        return cls(width=field.value, unit=field.unit, provenance=field.provenance, confidence=field.confidence)

class ColorSpec(BaseModel):
    mode: str | None = None
    provenance: ProvenanceType = "unresolved"
    confidence: float = 0.0

    @classmethod
    def from_field(cls, field):
        return cls(mode=field.value, provenance=field.provenance, confidence=field.confidence)

class PdfSpec(BaseModel):
    standard: str | None = None
    provenance: ProvenanceType = "unresolved"
    confidence: float = 0.0

    @classmethod
    def from_field(cls, field):
        return cls(standard=field.value, provenance=field.provenance, confidence=field.confidence)

class MarginSpec(BaseModel):
    top: DimensionSpec | None = None
    bottom: DimensionSpec | None = None
    inside: DimensionSpec | None = None
    outside: DimensionSpec | None = None

    @classmethod
    def unresolved(cls):
        d = DimensionSpec()
        return cls(top=d, bottom=d, inside=d, outside=d)

class SourceTemplate(BaseModel):
    file_id: str
    original_filename: str
    file_hash: str
    uploaded_at: datetime
    file_size: int

class ProjectSpec(BaseModel):
    id: str
    version: int = Field(ge=1)
    publisher: str
    source_template: SourceTemplate
    trim_width: DimensionSpec
    trim_height: DimensionSpec
    bleed: DimensionSpec
    safe_zone: DimensionSpec
    spine: SpineSpec
    color: ColorSpec
    pdf: PdfSpec
    margins: MarginSpec
    provenance: dict[str, Provenance]
    analysis: dict
