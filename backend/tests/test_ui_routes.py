import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "sparkprep_test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import oldserver


def test_root_serves_ui_and_health_endpoint():
    client = TestClient(oldserver.app)

    root_response = client.get("/")
    assert root_response.status_code == 200
    assert "text/html" in root_response.headers["content-type"].lower()

    health_response = client.get("/api/health")
    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"
