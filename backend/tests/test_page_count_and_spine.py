"""A cover's size depends on the page count and the spine -- so both must be right, or every cover is wrong.

  * the page count comes from the uploaded interior (a new project starts at 200);
  * changing anything that decides the size re-checks the uploaded covers (no stale verdicts);
  * a hardcover on a distributor with no published spine formula needs the real number from the
    distributor's template -- SparkPrep won't judge or export a cover around a guess;
  * IngramSpark paper thickness comes from its official Paper Specifications sheet."""
import asyncio
import io
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_pcs_"), USE_MEMORY_DB="1", JWT_SECRET="pcs-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import book_pass  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

EMAIL = "spine-author@example.com"


@pytest.fixture(autouse=True)
def book_mode(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "S", "tier": "free",
        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"}).json()["token"]
        yield c


def stored(pid):
    return asyncio.run(server.db.projects.find_one({"_id": server.ObjectId(pid)}))


def pdf(w_in, h_in, pages=1):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w_in * inch, h_in * inch))
    for _ in range(pages):
        c.drawString(1.2 * inch, 4 * inch, "Body text well inside the safe margins of the page, line one.")
        c.showPage()
    c.save()
    return buf.getvalue()


def project(client, **kw):
    body = {"name": "Spine Book", "platform": "ingramspark", "trim_size": "6x9", "paper_type": "white_50lb",
            "binding": "paperback", "page_count": 200, "project_type": "combined", **kw}
    return client.post("/api/projects", json=body).json()["id"]


def upload(client, pid, slot, data):
    r = client.post(f"/api/projects/{pid}/slot-upload/{slot}", files={"file": (f"{slot}.pdf", data, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def status(pid, slot, check_id):
    return next((c["status"] for c in stored(pid)["slots"][slot]["compliance"] if c["id"] == check_id), None)


def test_ingramspark_paper_thickness_comes_from_its_official_sheet(client):
    def spine(platform, paper, pages=200):
        return client.post("/api/specs/spine", json={"page_count": pages, "paper_type": paper, "platform": platform,
                                                     "binding": "paperback", "trim_size": "6x9"}).json()["spine_width"]
    assert spine("ingramspark", "white_50lb") == round(200 / 512, 4)       # White 50# = 512 PPI
    assert spine("ingramspark", "cream_50lb") == round(200 / 444, 4)       # Creme 50# = 444 PPI
    assert spine("ingramspark", "groundwood_38lb") == round(200 / 400, 4)  # Groundwood 38# = 400 PPI
    assert spine("kdp", "white_50lb") == round(200 / 444, 4)               # KDP unchanged


def test_page_count_comes_from_the_interior_and_the_cover_is_rechecked(client):
    pid = project(client)
    right_for_200 = 12 + 0.25 + 200 / 512                                  # sized for the default 200 pages
    upload(client, pid, "full_wrap", pdf(right_for_200, 9.25))
    assert status(pid, "full_wrap", "cover_size") == "pass"

    r = upload(client, pid, "interior", pdf(6.125, 9.25, pages=30))
    assert r["page_count_set"] == {"from": 200, "to": 30}
    assert stored(pid)["page_count"] == 30
    assert status(pid, "full_wrap", "cover_size") != "pass", "the cover was sized for 200 pages -- it must be re-checked"


def test_changing_a_spec_rechecks_the_covers(client):
    pid = project(client, page_count=100)
    upload(client, pid, "full_wrap", pdf(12 + 0.25 + 100 / 512, 9.25))
    assert status(pid, "full_wrap", "cover_size") == "pass"
    client.patch(f"/api/projects/{pid}", json={"page_count": 300})
    assert status(pid, "full_wrap", "cover_size") != "pass"
    client.patch(f"/api/projects/{pid}", json={"page_count": 100})
    assert status(pid, "full_wrap", "cover_size") == "pass"


def test_an_ingramspark_hardcover_needs_the_real_spine_number(client):
    pid = project(client, binding="hardcover_case", paper_type="cream_50lb", page_count=74)
    upload(client, pid, "full_wrap", pdf(14.194, 10.5))                    # the owner's real, accepted case size
    assert status(pid, "full_wrap", "spine_width_needed") == "fail"
    assert status(pid, "full_wrap", "cover_size") is None                  # no size verdict built on a guess

    asyncio.run(book_pass.entitlements.grant_credit(server.db, str(stored(pid)["user_id"]), "pass", dedupe_key=f"pcs:{time.time_ns()}"))
    assert client.post(f"/api/projects/{pid}/activate-book").status_code == 200
    r = client.post(f"/api/projects/{pid}/export", json={})
    assert r.status_code == 400 and "Spine Width" in r.json()["detail"]

    client.patch(f"/api/projects/{pid}", json={"spine_width_override": 0.313})   # from IngramSpark's template
    assert status(pid, "full_wrap", "spine_width_needed") is None
    assert status(pid, "full_wrap", "cover_size") == "pass"                # 14.194" x 10.5" -- matches the template


def test_the_no_account_audit_asks_for_a_hardcover_spine(client):
    base = {"platform": "ingramspark", "trim_size": "6x9", "file_type": "cover", "binding": "hardcover_case",
            "page_count": 74, "paper_type": "cream_50lb"}
    r = client.post("/api/audit/start", json=base)
    assert r.status_code == 400 and "Spine Width" in r.json()["detail"]
    assert client.post("/api/audit/start", json={**base, "spine_width": 0.313}).status_code == 200
    assert client.post("/api/audit/start", json={**base, "binding": "paperback"}).status_code == 200   # paperback: formula is published
