"""Studio covers stay with the book: the Cover Studio design is saved with it, and when the interior changes the
page count (and so the spine), the book says the studio cover needs re-fitting instead of stretching it."""
import asyncio
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

os.environ.update(DATA_DIR=os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="sp_refit_"), USE_MEMORY_DB="1",
                  JWT_SECRET=os.environ.get("JWT_SECRET") or "refit-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EMAIL = "refit@example.com"
OTHER = "refit-other@example.com"


@pytest.fixture(scope="module")
def client():
    for email in (EMAIL, OTHER):
        if not asyncio.run(server.db.users.find_one({"email": email})):
            asyncio.run(server.db.users.insert_one({
                "email": email, "password_hash": server.hash_password("TestPass123!"), "name": "R", "tier": "free",
                "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        yield c


def auth(client, email=EMAIL):
    return {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": email, "password": "TestPass123!"}).json()["token"]}


def book_with_cover(client, h, source="cover_studio"):
    pid = client.post("/api/projects", headers=h, json={"name": "Refit Book", "platform": "kdp", "trim_size": "6x9",
                      "paper_type": "white_50lb", "binding": "paperback", "page_count": 100, "project_type": "combined"}).json()["id"]
    w, h_in = 0.125 * 2 + 12 + 100 * 0.002252, 9.25
    buf = io.BytesIO()
    Image.new("RGB", (round(w * 300), round(h_in * 300)), (30, 30, 120)).save(buf, "JPEG", dpi=(300, 300))
    r = client.post(f"/api/projects/{pid}/slot-upload/full_wrap", headers=h, files={"file": ("cover.jpg", buf.getvalue(), "image/jpeg")},
                    data={"source": source} if source else None)
    assert r.status_code == 200, r.text
    return pid


def test_studio_cover_remembers_its_spine_and_flags_a_page_count_change(client):
    h = auth(client)
    pid = book_with_cover(client, h)
    p = client.get(f"/api/projects/{pid}", headers=h).json()
    assert p["cover_refit"] is None
    assert client.patch(f"/api/projects/{pid}", headers=h, json={"page_count": 300}).status_code == 200
    refit = client.get(f"/api/projects/{pid}", headers=h).json()["cover_refit"]
    assert refit["from_page_count"] == 100 and refit["to_page_count"] == 300
    assert refit["to_spine_width"] > refit["from_spine_width"]
    assert refit["has_design"] is False


def test_an_uploaded_cover_is_never_flagged_for_the_studio(client):
    h = auth(client)
    pid = book_with_cover(client, h, source=None)
    client.patch(f"/api/projects/{pid}", headers=h, json={"page_count": 300})
    assert client.get(f"/api/projects/{pid}", headers=h).json()["cover_refit"] is None


def test_design_saves_with_the_book_and_only_for_its_owner(client):
    h = auth(client)
    pid = book_with_cover(client, h)
    assert client.get(f"/api/projects/{pid}/cover-studio", headers=h).json()["design"] is None
    design = {"snap": {"objects": [{"type": "rect"}]}, "words": {"title": "Refit Book"}, "genre": "thriller"}
    r = client.put(f"/api/projects/{pid}/cover-studio", headers={**h, "content-type": "application/json"}, content=json.dumps(design))
    assert r.status_code == 200, r.text
    got = client.get(f"/api/projects/{pid}/cover-studio", headers=h).json()["design"]
    assert got["words"]["title"] == "Refit Book" and got["snap"]["objects"][0]["type"] == "rect"
    assert client.get(f"/api/projects/{pid}", headers=h).json()["has_cover_design"] is True
    other = auth(client, OTHER)
    assert client.get(f"/api/projects/{pid}/cover-studio", headers=other).status_code == 404
    assert client.put(f"/api/projects/{pid}/cover-studio", headers={**other, "content-type": "application/json"},
                      content=json.dumps(design)).status_code == 404


def test_bad_designs_are_refused(client):
    h = {**auth(client), "content-type": "application/json"}
    pid = book_with_cover(client, auth(client))
    assert client.put(f"/api/projects/{pid}/cover-studio", headers=h, content="not json").status_code == 400
    assert client.put(f"/api/projects/{pid}/cover-studio", headers=h, content=json.dumps({"snap": "x"})).status_code == 400


def test_deleting_the_book_deletes_its_design(client):
    h = auth(client)
    pid = book_with_cover(client, h)
    client.put(f"/api/projects/{pid}/cover-studio", headers={**h, "content-type": "application/json"}, content=json.dumps({"snap": {}}))
    path = server._cover_design_path(pid)
    assert path.exists()
    assert client.delete(f"/api/projects/{pid}", headers=h).status_code == 200
    assert not path.exists()
