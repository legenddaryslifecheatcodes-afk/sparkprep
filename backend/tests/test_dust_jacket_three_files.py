"""A Hardcover with Dust Jacket book is three files on IngramSpark: case, jacket, interior.
Runs the real app in-process against the in-memory database, with the owner's real book
numbers (6x9, spine 0.313"): jacket 20.438 x 9.5, case 14.194 x 10.5."""
import asyncio
import io
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_dj_"), USE_MEMORY_DB="1", JWT_SECRET="dj-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

EMAIL = "jacket-author@example.com"
JACKET = (20.438, 9.5)
CASE = (14.194, 10.5)


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "J", "tier": "free",
        "beta_active": True, "created_at": datetime.now(timezone.utc).isoformat(),
        "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        r = c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"})
        c.headers["Authorization"] = "Bearer " + r.json()["token"]
        yield c


@pytest.fixture(autouse=True)
def legacy_pricing(monkeypatch):
    monkeypatch.delenv("SPARKPREP_PRICING_MODEL", raising=False)


def jpeg(w_in, h_in, dpi=300):
    buf = io.BytesIO()
    Image.new("RGB", (round(w_in * dpi), round(h_in * dpi)), (20, 20, 20)).save(buf, "JPEG", quality=80, dpi=(dpi, dpi))
    return buf.getvalue()


def interior_pdf():
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch))
    for _ in range(2):
        c.setFont("Times-Roman", 11)
        for i in range(20):
            c.drawString(1.1 * inch, (8.0 - i * 0.3) * inch, "Centered body text well inside the safe margins of the page.")
        c.showPage()
    c.save()
    return buf.getvalue()


def make_project(client, binding="hardcover_jacket"):
    pid = client.post("/api/projects", json={"name": "Jacket Book", "platform": "ingramspark", "trim_size": "6x9",
                                              "paper_type": "cream_50lb", "binding": binding, "page_count": 74,
                                              "project_type": "combined"}).json()["id"]
    asyncio.run(server.db.projects.update_one({"_id": server.ObjectId(pid)}, {"$set": {"spine_width_override": 0.313}}))
    return pid


def upload(client, pid, slot, name, data, mime):
    return client.post(f"/api/projects/{pid}/slot-upload/{slot}", files={"file": (name, data, mime)})


def check(resp, cid):
    return next((c for c in resp.json()["compliance"] if c["id"] == cid), None)


def test_case_slot_is_refused_for_a_book_without_a_jacket(client):
    pid = make_project(client, binding="paperback")
    r = upload(client, pid, "case_wrap", "case.jpg", jpeg(*CASE), "image/jpeg")
    assert r.status_code == 400
    assert "Dust Jacket" in r.json()["detail"]


def test_wrong_shaped_case_fails_the_size_check_instead_of_being_stretched(client):
    pid = make_project(client)
    r = upload(client, pid, "case_wrap", "case.jpg", jpeg(18, 12.5), "image/jpeg")   # built on the template PAGE size
    assert r.status_code == 200, r.text
    assert check(r, "cover_size")["status"] == "fail"
    r = upload(client, pid, "case_wrap", "case.jpg", jpeg(*CASE), "image/jpeg")
    assert check(r, "cover_size")["status"] == "pass"


def test_wrong_size_pdf_jacket_fails_the_size_check(client):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(24 * 72, 12.5 * 72)); c.showPage(); c.save()
    r = upload(client, make_project(client), "full_wrap", "jacket.pdf", buf.getvalue(), "application/pdf")
    assert r.status_code == 200, r.text
    assert check(r, "cover_size")["status"] == "fail"
    assert '20.438" x 9.500"' in check(r, "cover_size")["message"]


def pdf_page(w_in, h_in):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w_in * 72, h_in * 72))
    c.setFillColorRGB(0.1, 0.1, 0.1); c.rect(0, 0, w_in * 72, h_in * 72, fill=1, stroke=0)
    c.setFillColorRGB(0.85, 0.7, 0.3); c.drawString(72, 72, "Legenddary Mindset")
    c.showPage(); c.save()
    return buf.getvalue()


def test_pdf_covers_export(client):
    """Authors mostly deliver covers as PDFs. Export used to open every cover with PIL, which can't
    read a PDF, so a PDF jacket or case failed the whole export."""
    pid = make_project(client)
    assert upload(client, pid, "interior", "book.pdf", interior_pdf(), "application/pdf").status_code == 200
    assert check(upload(client, pid, "full_wrap", "jacket.pdf", pdf_page(*JACKET), "application/pdf"), "cover_size")["status"] == "pass"
    assert check(upload(client, pid, "case_wrap", "case.pdf", pdf_page(*CASE), "application/pdf"), "cover_size")["status"] == "pass"
    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 200, r.text
    assert r.json()["cover"]["page_size_inches"] == pytest.approx(list(JACKET), abs=0.002)
    assert r.json()["case"]["page_size_inches"] == pytest.approx(list(CASE), abs=0.002)


def test_dust_jacket_book_exports_case_jacket_and_interior_together(client):
    pid = make_project(client)
    assert upload(client, pid, "interior", "book.pdf", interior_pdf(), "application/pdf").status_code == 200
    r = upload(client, pid, "full_wrap", "jacket.jpg", jpeg(*JACKET), "image/jpeg")
    assert check(r, "cover_size")["status"] == "pass"
    assert check(r, "dpi")["status"] == "pass"   # exactly 300 DPI must not be called "soft" over pixel rounding

    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 404 and "case" in r.json()["detail"].lower()          # case still missing

    assert check(upload(client, pid, "case_wrap", "case.jpg", jpeg(*CASE), "image/jpeg"), "cover_size")["status"] == "pass"
    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bundled"] and {"cover", "case", "interior"} <= set(body)
    assert body["cover"]["page_size_inches"] == pytest.approx(list(JACKET), abs=0.002)
    assert body["case"]["page_size_inches"] == pytest.approx(list(CASE), abs=0.002)

    with zipfile.ZipFile(server.EXPORT_DIR / body["export_name"]) as zf:
        names = sorted(zf.namelist())
    assert names == ["Jacket Book_case.pdf", "Jacket Book_cover.pdf", "Jacket Book_interior.pdf"]
