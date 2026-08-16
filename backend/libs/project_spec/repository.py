import json
from pathlib import Path
from libs.common.settings import settings
from libs.project_spec.models import ProjectSpec

class ProjectSpecRepository:
    """Filesystem persistence for v0.1.

    The interface is intentionally isolated so PostgreSQL/JSONB can replace it later.
    """

    def __init__(self, root=None):
        self.root = Path(root or settings.storage_root) / "projects"

    def save(self, spec: ProjectSpec):
        path = self.root / spec.id / "specs" / f"v{spec.version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise ValueError("ProjectSpec version already exists and is immutable.")
        path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")

    def get_latest(self, project_id):
        directory = self.root / project_id / "specs"
        if not directory.exists():
            return None
        versions = sorted(directory.glob("v*.json"), key=lambda p: int(p.stem[1:]))
        if not versions:
            return None
        return ProjectSpec.model_validate_json(versions[-1].read_text(encoding="utf-8"))
