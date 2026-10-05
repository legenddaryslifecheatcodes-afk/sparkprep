"""The plain case under a dust jacket gets the same fixes as the jacket -- SparkPrep brings it to spec itself
instead of asking for a new file (owner, 2026-10-05): Repair Bay converts its colors, AI Upscale its resolution."""
import asyncio
import io
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_case_"), USE_MEMORY_DB="1", JWT_SECRET="case-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import book_pass  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

EMAIL = "case-author@example.com"


@pytest.fixture(autouse=True)
def book_mode(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "K", "tier": "free",
        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"}).json()["token"]
        yield c


def status(compliance, check_id):
    return next((c["status"] for c in compliance if c["id"] == check_id), None)


def test_the_case_cover_is_fixed_by_sparkprep_not_sent_back(client):
    pid = client.post("/api/projects", json={"name": "Case Book", "platform": "ingramspark", "trim_size": "6x9",
                                             "paper_type": "cream_50lb", "binding": "hardcover_jacket", "page_count": 74,
                                             "project_type": "cover"}).json()["id"]
    uid = str(asyncio.run(server.db.users.find_one({"email": EMAIL}))["_id"])
    asyncio.run(book_pass.entitlements.grant_credit(server.db, uid, "pass", dedupe_key=f"case:{time.time_ns()}"))
    assert client.post(f"/api/projects/{pid}/activate-book").status_code == 200

    # a low-resolution, RGB case at the right shape (14.194" x 10.5" for a 0.313" spine) -- about 150 DPI
    w_in, h_in, dpi = 14.194, 10.5, 150
    buf = io.BytesIO()
    Image.new("RGB", (round(w_in * dpi), round(h_in * dpi)), (40, 30, 90)).save(buf, "JPEG", dpi=(dpi, dpi))
    r = client.post(f"/api/projects/{pid}/slot-upload/case_wrap", files={"file": ("case.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200, r.text
    before = r.json()["compliance"]
    assert status(before, "colorspace") == "warning" and status(before, "dpi") in ("warning", "fail")

    # Repair Bay on the case: colors converted, independently verified
    r = client.post(f"/api/projects/{pid}/autofix/verified?stream=false&slot=case_wrap")
    assert r.status_code == 200, r.text
    case = asyncio.run(server.db.projects.find_one({"_id": server.ObjectId(pid)}))["slots"]["case_wrap"]
    assert status(case["compliance"], "colorspace") == "pass"

    # AI Upscale on the case: up to the exact 300 DPI size for the case
    r = client.post(f"/api/projects/{pid}/ai-enhance/case_wrap")
    assert r.status_code == 200, r.text
    assert status(r.json()["compliance"], "dpi") == "pass"
    assert status(r.json()["compliance"], "cover_size") == "pass"          # still exactly the case's size
