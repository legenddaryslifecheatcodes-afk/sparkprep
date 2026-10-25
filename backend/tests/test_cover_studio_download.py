"""Cover Studio: designing is free, the print-size file is part of the book.

A cover sent from the studio (source=cover_studio) can be previewed at screen size, but its full-size original only
downloads once the book is started. An author's own uploaded file is theirs and still downloads as before."""
import asyncio
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

os.environ.update(DATA_DIR=os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="sp_csd_"), USE_MEMORY_DB="1",
                  JWT_SECRET=os.environ.get("JWT_SECRET") or "csd-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EMAIL = "studio-download@example.com"


@pytest.fixture(autouse=True)
def book_mode(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")


@pytest.fixture(scope="module")
def client():
    if not asyncio.run(server.db.users.find_one({"email": EMAIL})):
        asyncio.run(server.db.users.insert_one({
            "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "D", "tier": "free",
            "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"}).json()["token"]
        yield c


def cover_project(client, source=None):
    pid = client.post("/api/projects", json={"name": "Studio Book", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                              "binding": "paperback", "page_count": 100, "project_type": "cover"}).json()["id"]
    w, h = 0.125 * 2 + 12 + 100 / 444, 9.25
    buf = io.BytesIO()
    Image.new("RGB", (round(w * 300), round(h * 300)), (30, 30, 120)).save(buf, "JPEG", dpi=(300, 300))
    r = client.post(f"/api/projects/{pid}/slot-upload/full_wrap", files={"file": ("cover.jpg", buf.getvalue(), "image/jpeg")},
                    data={"source": source} if source else None)
    assert r.status_code == 200, r.text
    return pid


def preview_size(client, url):
    r = client.get(url, params={"token": client.headers["Authorization"].split()[1]})
    assert r.status_code == 200, r.text
    return Image.open(io.BytesIO(r.content)).size


def test_studio_cover_original_needs_the_book(client):
    pid = cover_project(client, "cover_studio")
    r = client.get(f"/api/projects/{pid}/slot/full_wrap/original")
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "book_required"


def test_studio_cover_previews_at_screen_size_only(client):
    pid = cover_project(client, "cover_studio")
    for url in (f"/api/projects/{pid}/preview", f"/api/projects/{pid}/slot/full_wrap/preview"):
        assert max(preview_size(client, url)) <= server.STUDIO_PREVIEW_MAX_PX


def test_studio_cover_original_downloads_once_the_book_is_started(client):
    pid = cover_project(client, "cover_studio")
    asyncio.run(server.db.projects.update_one({"_id": server.ObjectId(pid)}, {"$set": {"promo_access": "full_access"}}))
    r = client.get(f"/api/projects/{pid}/slot/full_wrap/original")
    assert r.status_code == 200
    assert Image.open(io.BytesIO(r.content)).size[0] > 3000


def test_an_authors_own_upload_is_unchanged(client):
    pid = cover_project(client)
    r = client.get(f"/api/projects/{pid}/slot/full_wrap/original")
    assert r.status_code == 200
    assert max(preview_size(client, f"/api/projects/{pid}/slot/full_wrap/preview")) > server.STUDIO_PREVIEW_MAX_PX
