from typing import Any, Literal
from pydantic import BaseModel, Field

Source = Literal["text", "geometry", "image", "fused"]
ProvenanceType = Literal["extracted", "calculated", "inferred", "user_confirmed", "unresolved"]

class Geometry(BaseModel):
    x: float
    y: float
    width: float
    height: float
    unit: Literal["pt", "in", "mm", "px"] = "pt"
    page: int = 0

class Evidence(BaseModel):
    type: str
    geometry: Geometry | None = None
    color: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source: Source
    provenance: str
    raw: dict[str, Any] | None = None
