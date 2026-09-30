"""Every finished book gets a report; only a book whose FINAL files pass every check earns
"The SparkPrep Certified Complete Publisher Preflight Report" (owner's rule, 2026-09-29).
Anything else gets the plainer "SparkPrep Preflight Report" naming what's still open."""
import asyncio
import io
import os
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_cert_"), USE_MEMORY_DB="1", JWT_SECRET="cert-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import book_pass  # noqa: E402
import fitz  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402
from file_processor import rgb_array_to_cmyk_array  # noqa: E402

EMAIL = "cert-author@example.com"
PAGES = 40


@pytest.fixture(autouse=True)
def book_mode(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "C", "tier": "free",
        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"}).json()["token"]
        yield c


def interior_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6.125 * inch, 9.25 * inch))
    for i in range(PAGES):
        c.setFont("Times-Roman", 11)
        for line in range(18):
            c.drawString(1.1 * inch, (8.0 - line * 0.35) * inch, f"Page {i + 1}: body text well inside the safe margins.")
        c.showPage()
    c.save()
    return buf.getvalue()


def cover_tiff(dpi):
    w_in, h_in = 12 + 0.25 + PAGES / 512, 9.25                   # IngramSpark white 50# = 512 PPI
    rgb = np.full((round(h_in * dpi), round(w_in * dpi), 3), 235, dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb_array_to_cmyk_array(rgb), mode="CMYK").save(buf, "TIFF", dpi=(dpi, dpi), compression="tiff_lzw")
    return buf.getvalue()


def export_book(client, cover_dpi):
    pid = client.post("/api/projects", json={"name": f"Cert Book {cover_dpi}", "platform": "ingramspark", "trim_size": "6x9",
                                             "paper_type": "white_50lb", "binding": "paperback", "page_count": 200,
                                             "project_type": "combined"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/slot-upload/interior", files={"file": ("book.pdf", interior_pdf(), "application/pdf")}).status_code == 200
    assert client.post(f"/api/projects/{pid}/slot-upload/full_wrap", files={"file": ("cover.tif", cover_tiff(cover_dpi), "image/tiff")}).status_code == 200
    uid = str(asyncio.run(server.db.users.find_one({"email": EMAIL}))["_id"])
    asyncio.run(book_pass.entitlements.grant_credit(server.db, uid, "pass", dedupe_key=f"cert:{time.time_ns()}"))
    assert client.post(f"/api/projects/{pid}/activate-book").status_code == 200
    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 200, r.text
    data = r.json()
    dl = client.get(data["download_url"], params={"token": client.headers["Authorization"].split(" ", 1)[1]})
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    return data, zf


def report_text(zf, name_part):
    names = [n for n in zf.namelist() if name_part in n]
    assert len(names) == 1, zf.namelist()
    doc = fitz.open(stream=zf.read(names[0]), filetype="pdf")
    return " ".join(p.get_text() for p in doc)


def test_a_clean_book_earns_sparkprep_certified(client):
    data, zf = export_book(client, cover_dpi=300)
    assert data["certified"] is True, data.get("still_open")
    assert data["certificate_id"].startswith("SPC-")
    text = report_text(zf, "SparkPrep_Certified_Report")
    assert "The SparkPrep Certified Complete Publisher Preflight Report" in text
    assert data["certificate_id"] in text
    assert f"{PAGES} of {PAGES} pages checked" in text                 # says exactly what was checked
    assert "printer makes the final acceptance decision" in text        # never promises acceptance
    assert asyncio.run(server.db.certificates.find_one({"certificate_id": data["certificate_id"]}))


def test_anything_left_open_gets_the_standard_report_instead(client):
    data, zf = export_book(client, cover_dpi=260)                      # exports, but soft for print
    assert data["certified"] is False and data["certificate_id"] is None
    assert any("Resolution" in t or "DPI" in t for t in data["still_open"])
    text = report_text(zf, "SparkPrep_Preflight_Report")
    assert "SparkPrep Preflight Report" in text and "Still open" in text
    assert "Certified Complete Publisher Preflight" not in text
    assert not [n for n in zf.namelist() if "Certified_Report" in n]


def test_an_unfixed_issue_comes_with_do_it_yourself_steps(client):
    data, zf = export_book(client, cover_dpi=260)
    text = report_text(zf, "SparkPrep_Preflight_Report")
    assert "How to fix it yourself" in text and "300 DPI" in text      # the book's report teaches the fix


def test_anything_sparkprep_cant_fix_is_kept_as_an_unsolved_case(client, monkeypatch):
    data, _ = export_book(client, cover_dpi=260)
    case = asyncio.run(server.db.unsolved_cases.find_one({"source": "export", "solved": False}))
    assert case and any("Resolution" in o["title"] or "DPI" in o["title"] for o in case["open"])
    assert case["platform"] == "ingramspark" and case["page_count"] == PAGES
    assert case["file_facts"]["full_wrap"]["dpi_x"] == 260            # the details of what happened...
    assert "files" not in case and "stored_filename" not in str(case)  # ...never the customer's file itself

    monkeypatch.setattr(server, "ADMIN_EMAIL", EMAIL)                  # the owner's admin page
    listing = client.get("/api/admin/unsolved-cases").json()
    assert any(c["case_id"] == case["case_id"] for c in listing["cases"])
    assert client.post(f"/api/admin/unsolved-cases/{case['case_id']}/solved").json()["ok"] is True
    assert not any(c["case_id"] == case["case_id"] for c in client.get("/api/admin/unsolved-cases").json()["cases"])
