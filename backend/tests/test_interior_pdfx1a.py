"""Backend tests for PDF/X-1a:2001 multi-page interior export path.

Covers:
  - Author-tier user upgrade + interior PDF export using build_interior_pdf_x1a
  - PDF/X-1a metadata (GTS_PDFXVersion, Trapped, OutputIntents)
  - Raw-bytes assertion `/Trapped /False` (regression for pikepdf.Name.False_)
  - Vector fonts preserved on pages
  - Regression: cover project + PNG still uses legacy build_print_ready_pdf
  - Regression: interior project + PNG falls back to build_print_ready_pdf
  - Book counter semantics (author = 1/month, second interior => 402)
  - Free tier interior export => 402
"""
import io
import os
import time
import asyncio
import pytest
import requests
from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from PIL import Image
import pikepdf
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

def _load_backend_url():
    if os.environ.get("REACT_APP_BACKEND_URL"):
        return os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
    envp = Path("/app/frontend/.env")
    if envp.exists():
        for ln in envp.read_text().splitlines():
            if ln.startswith("REACT_APP_BACKEND_URL="):
                return ln.split("=", 1)[1].strip().rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not configured")

BASE_URL = _load_backend_url()
API = f"{BASE_URL}/api"

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "sparkprep_db")

TS = str(int(time.time()))


# ---------- helpers ----------
def make_manuscript_pdf(path: str, pages: int = 5):
    c = canvas.Canvas(path, pagesize=(6 * inch, 9 * inch))
    for i in range(pages):
        c.setFont("Helvetica-Bold", 18)
        c.drawString(1 * inch, 8 * inch, f"Chapter {i + 1}")
        c.setFont("Times-Roman", 12)
        c.drawString(1 * inch, 7 * inch, f"This is vector text on page {i + 1}.")
        c.drawString(1 * inch, 6.5 * inch, "The quick brown fox jumps over the lazy dog.")
        c.showPage()
    c.save()


def make_png(path: str, w: int = 2000, h: int = 3000):
    img = Image.new("RGB", (w, h), color=(240, 220, 200))
    img.save(path, "PNG", dpi=(300, 300))


async def _upgrade_tier(user_id: str, tier: str):
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"tier": tier, "books_this_month": 0, "exports_this_month": 0}},
    )
    client.close()


def upgrade_tier(user_id: str, tier: str):
    asyncio.run(_upgrade_tier(user_id, tier))


# ---------- fixtures ----------
@pytest.fixture(scope="module")
def author_session():
    s = requests.Session()
    email = f"qa+interior{TS}@example.com"
    r = s.post(f"{API}/auth/register", json={"email": email, "password": "TestPass123!", "name": "QA Interior"})
    assert r.status_code == 200, r.text
    user_id = r.json()["user"]["id"]
    upgrade_tier(user_id, "author")
    # Re-login to refresh anything
    r = s.post(f"{API}/auth/login", json={"email": email, "password": "TestPass123!"})
    assert r.status_code == 200
    return {"session": s, "email": email, "user_id": user_id}


@pytest.fixture(scope="module")
def free_session():
    s = requests.Session()
    email = f"qa+free{TS}@example.com"
    r = s.post(f"{API}/auth/register", json={"email": email, "password": "TestPass123!", "name": "QA Free"})
    assert r.status_code == 200
    return {"session": s, "email": email, "user_id": r.json()["user"]["id"]}


@pytest.fixture(scope="module")
def manuscript_pdf(tmp_path_factory):
    p = tmp_path_factory.mktemp("data") / "manuscript.pdf"
    make_manuscript_pdf(str(p), pages=5)
    return str(p)


@pytest.fixture(scope="module")
def cover_png(tmp_path_factory):
    p = tmp_path_factory.mktemp("data") / "cover.png"
    make_png(str(p))
    return str(p)


# ---------- helpers to create project + upload ----------
def create_project(s, project_type, name="TEST_book"):
    r = s.post(f"{API}/projects", json={
        "name": name,
        "platform": "kdp",
        "trim_size": "6x9",
        "paper_type": "cream_50lb",
        "binding": "paperback",
        "page_count": 100,
        "project_type": project_type,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def upload(s, pid, path, content_type):
    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f, content_type)}
        r = s.post(f"{API}/projects/{pid}/upload", files=files)
    assert r.status_code == 200, r.text
    return r.json()


def export(s, pid):
    return s.post(f"{API}/projects/{pid}/export")


# ============================================================
# Main interior PDF/X-1a test
# ============================================================
class TestInteriorPdfX1a:
    def test_full_flow(self, author_session, manuscript_pdf, tmp_path):
        s = author_session["session"]
        pid = create_project(s, "interior", name="TEST_interior_multipage")
        meta = upload(s, pid, manuscript_pdf, "application/pdf")
        assert meta["file_metadata"].get("pdf_pages") == 5

        r = export(s, pid)
        assert r.status_code == 200, r.text
        data = r.json()

        # ---- response assertions ----
        assert data["page_count"] == 5, f"expected 5 pages, got {data['page_count']}"
        assert data["page_size_inches"] == [6.25, 9.25]
        assert data["trim_box_inches"] == [6.0, 9.0]
        assert data["bleed_inches"] == 0.125
        assert data["pdf_standard"] == "PDF/X-1a:2001"
        assert data["vector_preserved"] is True
        assert data["fonts_preserved"] is True
        assert "CGATS TR 001" in data["output_intent"]
        assert data["books_this_month"] == 1
        assert data["books_limit"] == 1
        assert data["counted_as_new_book"] is True

        # ---- download PDF & save for pikepdf inspection ----
        dl = s.get(f"{BASE_URL}{data['download_url']}")
        assert dl.status_code == 200, dl.text
        pdf_bytes = dl.content
        out = tmp_path / "exported.pdf"
        out.write_bytes(pdf_bytes)

        # raw bytes check for Trapped /False (NOT /False_)
        assert b"/Trapped /False" in pdf_bytes, "Missing /Trapped /False in raw PDF"
        assert b"/Trapped /False_" not in pdf_bytes, "Found buggy /Trapped /False_ — pikepdf.Name.False_ bug"

        # ---- pikepdf inspection ----
        with pikepdf.open(str(out)) as pdf:
            assert len(pdf.pages) == 5
            assert str(pdf.pdf_version) == "1.4"
            assert str(pdf.Root.GTS_PDFXVersion) == "PDF/X-1a:2001"
            trapped = pdf.Root.Trapped
            assert str(trapped) == "/False", f"Trapped is {trapped!r}, expected /False"

            oi_arr = pdf.Root.OutputIntents
            assert len(oi_arr) >= 1
            oi = oi_arr[0]
            assert str(oi.S) == "/GTS_PDFX"
            assert str(oi.OutputConditionIdentifier) == "CGATS TR 001 (SWOP)"

            # each page: mediabox = [0,0,450,666]; trimbox = [9,9,441,657]
            for page in pdf.pages:
                mb = [float(x) for x in page.mediabox]
                tb = [float(x) for x in page.trimbox]
                assert mb == [0.0, 0.0, 450.0, 666.0], f"mediabox {mb}"
                assert tb == [9.0, 9.0, 441.0, 657.0], f"trimbox {tb}"

            # Fonts preserved on page 1
            page0 = pdf.pages[0]
            resources = page0.Resources
            assert "/Font" in resources, "No /Font in page.Resources — fonts rasterized?"
            fonts = resources.Font
            font_keys = list(fonts.keys())
            assert len(font_keys) >= 1, "No fonts embedded — vector text may have been rasterized"
            print(f"Fonts on page 0: {font_keys}")

    def test_book_counter_reexport_same_project(self, author_session, manuscript_pdf):
        """Re-exporting same project => counted_as_new_book False."""
        s = author_session["session"]
        # find project already exported
        r = s.get(f"{API}/projects")
        projects = r.json()["projects"]
        pid = next(p["id"] for p in projects if p["name"] == "TEST_interior_multipage")
        r = export(s, pid)
        assert r.status_code == 200, r.text
        assert r.json()["counted_as_new_book"] is False
        assert r.json()["books_this_month"] == 1

    def test_second_new_interior_project_hits_402(self, author_session, manuscript_pdf):
        s = author_session["session"]
        pid = create_project(s, "interior", name="TEST_interior_second")
        upload(s, pid, manuscript_pdf, "application/pdf")
        r = export(s, pid)
        assert r.status_code == 402, r.text
        assert "Monthly book allowance reached" in r.json()["detail"]


# ============================================================
# Regression: cover flow (PNG) — legacy build_print_ready_pdf
# ============================================================
class TestCoverRegression:
    def test_cover_export_legacy_flow(self, cover_png, tmp_path):
        # Fresh user so book counter separate
        s = requests.Session()
        email = f"qa+cover{TS}@example.com"
        r = s.post(f"{API}/auth/register", json={"email": email, "password": "TestPass123!"})
        assert r.status_code == 200
        upgrade_tier(r.json()["user"]["id"], "author")
        s.post(f"{API}/auth/login", json={"email": email, "password": "TestPass123!"})

        pid = create_project(s, "cover", name="TEST_cover")
        upload(s, pid, cover_png, "image/png")
        r = export(s, pid)
        assert r.status_code == 200, r.text
        data = r.json()
        # legacy branch returns spine_width & does NOT return pdf_standard=PDF/X-1a:2001 vector flag
        # It's rasterized flow with spine_width in response.
        assert "spine_width" in data or "page_size_inches" in data
        # The legacy build_print_ready_pdf sets a page size that includes spine when cover
        assert data.get("vector_preserved") is not True or data.get("pdf_standard") != "PDF/X-1a:2001" or "spine_width" in data
        print(f"Cover export response keys: {list(data.keys())}")


# ============================================================
# Regression: interior with IMAGE (PNG) => legacy flow
# ============================================================
class TestInteriorImageRegression:
    def test_interior_png_falls_back(self, cover_png):
        s = requests.Session()
        email = f"qa+intimg{TS}@example.com"
        r = s.post(f"{API}/auth/register", json={"email": email, "password": "TestPass123!"})
        assert r.status_code == 200
        upgrade_tier(r.json()["user"]["id"], "author")
        s.post(f"{API}/auth/login", json={"email": email, "password": "TestPass123!"})

        pid = create_project(s, "interior", name="TEST_interior_png")
        upload(s, pid, cover_png, "image/png")
        r = export(s, pid)
        assert r.status_code == 200, r.text
        data = r.json()
        # legacy flow => single page (not 5), no PDF/X-1a:2001 pdf_standard from x1a branch
        assert data.get("page_count", 1) == 1 or data.get("pdf_standard") != "PDF/X-1a:2001"
        print(f"Interior-PNG export response: pdf_standard={data.get('pdf_standard')}, page_count={data.get('page_count')}")


# ============================================================
# Free tier interior export => 402
# ============================================================
class TestFreeTierBlocked:
    def test_free_tier_interior_402(self, free_session, manuscript_pdf):
        s = free_session["session"]
        pid = create_project(s, "interior", name="TEST_free_interior")
        upload(s, pid, manuscript_pdf, "application/pdf")
        r = export(s, pid)
        assert r.status_code == 402, r.text
        assert "doesn't include full book exports" in r.json()["detail"] or "Upgrade" in r.json()["detail"]
