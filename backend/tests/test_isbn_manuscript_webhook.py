"""Iteration 13 backend tests:
- ISBN validation + barcode PNG endpoints
- Project ISBN PATCH
- Manuscript templates / compose / preview
- Cover export barcode overlay
- Stripe webhook: audit_099 + subscription events
- Regression: interior/cover export still functional
"""
import os
import io
import time
import json
import asyncio
from pathlib import Path

import pytest
import requests
import pikepdf
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId


def _base_url():
    if os.environ.get("REACT_APP_BACKEND_URL"):
        return os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
    envp = Path("/app/frontend/.env")
    for ln in envp.read_text().splitlines():
        if ln.startswith("REACT_APP_BACKEND_URL="):
            return ln.split("=", 1)[1].strip().rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not configured")


BASE_URL = _base_url()
API = f"{BASE_URL}/api"
def _load_env():
    envp = Path("/app/backend/.env")
    if envp.exists():
        for ln in envp.read_text().splitlines():
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_env()
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
TS = str(int(time.time()))


# ---------- helpers ----------
async def _mongo():
    return AsyncIOMotorClient(MONGO_URL)[DB_NAME]


async def _upgrade(user_id: str, tier: str = "author"):
    db = await _mongo()
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"tier": tier, "books_this_month": 0, "exports_this_month": 0}},
    )


def upgrade(user_id, tier="author"):
    asyncio.run(_upgrade(user_id, tier))


async def _find_project(pid: str):
    db = await _mongo()
    return await db.projects.find_one({"_id": ObjectId(pid)})


def find_project(pid):
    return asyncio.run(_find_project(pid))


async def _find_audit(audit_id: str):
    db = await _mongo()
    return await db.audits.find_one({"audit_id": audit_id})


async def _find_user(uid: str):
    db = await _mongo()
    return await db.users.find_one({"_id": ObjectId(uid)})


async def _insert_txn(doc):
    db = await _mongo()
    await db.payment_transactions.insert_one(doc)


async def _insert_audit(doc):
    db = await _mongo()
    await db.audits.insert_one(doc)


def make_manuscript_pdf(path, pages=3):
    c = canvas.Canvas(path, pagesize=(6 * inch, 9 * inch))
    for i in range(pages):
        c.setFont("Helvetica", 14)
        c.drawString(72, 72, f"Page {i+1}")
        c.showPage()
    c.save()


def make_cover_png(path, w=3600, h=2400):
    Image.new("RGB", (w, h), (245, 230, 210)).save(path, "PNG", dpi=(300, 300))


# ---------- fixtures ----------
@pytest.fixture(scope="module")
def author_session():
    s = requests.Session()
    email = f"qa+trio{TS}@example.com"
    r = s.post(f"{API}/auth/register", json={"email": email, "password": "TestPass123!", "name": "QA Trio"})
    assert r.status_code == 200, r.text
    uid = r.json()["user"]["id"]
    tok = r.json()["token"]
    upgrade(uid, "author")
    s.post(f"{API}/auth/login", json={"email": email, "password": "TestPass123!"})
    return {"session": s, "user_id": uid, "email": email, "token": tok}


@pytest.fixture(scope="module")
def free_session():
    s = requests.Session()
    email = f"qa+free{TS}@example.com"
    r = s.post(f"{API}/auth/register", json={"email": email, "password": "TestPass123!"})
    assert r.status_code == 200
    return {"session": s, "user_id": r.json()["user"]["id"], "email": email, "token": r.json()["token"]}


@pytest.fixture(scope="module")
def manuscript_pdf(tmp_path_factory):
    p = tmp_path_factory.mktemp("m") / "m.pdf"
    make_manuscript_pdf(str(p), pages=3)
    return str(p)


@pytest.fixture(scope="module")
def cover_png(tmp_path_factory):
    p = tmp_path_factory.mktemp("c") / "c.png"
    make_cover_png(str(p))
    return str(p)


def _create_project(s, project_type, name):
    r = s.post(f"{API}/projects", json={
        "name": name, "platform": "kdp", "trim_size": "6x9",
        "paper_type": "cream_50lb", "binding": "paperback",
        "page_count": 200, "project_type": project_type,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload(s, pid, path, ct):
    with open(path, "rb") as f:
        r = s.post(f"{API}/projects/{pid}/upload",
                   files={"file": (os.path.basename(path), f, ct)})
    assert r.status_code == 200, r.text
    return r.json()


# ============================================================
# ISBN validate
# ============================================================
class TestIsbnValidate:
    def test_valid_isbn13(self):
        r = requests.post(f"{API}/isbn/validate", json={"isbn": "9780316148412"})
        assert r.status_code == 200
        d = r.json()
        assert d["valid"] is True
        assert d["isbn"] == "9780316148412"

    def test_invalid_check_digit(self):
        r = requests.post(f"{API}/isbn/validate", json={"isbn": "9780316148413"})
        assert r.status_code == 200
        d = r.json()
        assert d["valid"] is False
        assert "check digit" in d.get("error", "").lower()

    def test_isbn10_conversion(self):
        r = requests.post(f"{API}/isbn/validate", json={"isbn": "0316148415"})
        assert r.status_code == 200
        d = r.json()
        assert d["valid"] is True
        assert d["isbn"] == "9780316148412"


# ============================================================
# Barcode PNG endpoint
# ============================================================
class TestBarcodePng:
    def test_barcode_png(self):
        r = requests.get(f"{API}/isbn/barcode.png", params={"isbn": "9780316148412"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        img = Image.open(io.BytesIO(r.content))
        assert img.width > 200 and img.height > 200, f"Barcode too small: {img.size}"

    def test_barcode_invalid_isbn_400(self):
        r = requests.get(f"{API}/isbn/barcode.png", params={"isbn": "1234"})
        assert r.status_code == 400


# ============================================================
# PATCH /api/projects/{id}/isbn
# ============================================================
class TestProjectIsbn:
    def test_set_isbn_valid(self, author_session):
        s = author_session["session"]
        pid = _create_project(s, "cover", f"TEST_isbn_proj_{TS}")
        r = s.patch(f"{API}/projects/{pid}/isbn", json={"isbn": "9780316148412"})
        assert r.status_code == 200, r.text
        assert r.json()["isbn"] == "9780316148412"
        # Verify persistence via GET
        g = s.get(f"{API}/projects/{pid}")
        assert g.status_code == 200
        assert g.json().get("isbn") == "9780316148412"

    def test_set_isbn_invalid(self, author_session):
        s = author_session["session"]
        pid = _create_project(s, "cover", f"TEST_isbn_bad_{TS}")
        r = s.patch(f"{API}/projects/{pid}/isbn", json={"isbn": "9780316148413"})
        assert r.status_code == 400


# ============================================================
# Manuscript templates + compose + preview
# ============================================================
class TestManuscript:
    _file_id = None
    _token = None

    def test_templates_list(self):
        r = requests.get(f"{API}/manuscript/templates")
        assert r.status_code == 200
        templates = r.json()["templates"]
        keys = {t["key"] for t in templates}
        assert {"fiction_novel", "workbook", "poetry_chapbook"}.issubset(keys)

    def test_compose_fiction(self, author_session):
        s = author_session["session"]
        source_text = (
            "# Chapter One\n\nOnce upon a time in a land far away, an author began to write. "
            "The words flowed easily onto the page.\n\n"
            "# Chapter Two\n\nMuch later, the same author found their voice. "
            "The second chapter was easier than the first."
        )
        r = s.post(f"{API}/manuscript/compose", json={
            "template": "fiction_novel",
            "title": "TEST Book",
            "author": "QA Tester",
            "source_text": source_text,
            "trim_size": "6x9",
            "platform": "kdp",
        })
        assert r.status_code == 200, r.text
        d = r.json()
        assert "file_id" in d and d["file_id"].startswith("manuscript_")
        assert d["preview_url"].startswith("/api/manuscript/preview/")
        assert d["page_count"] >= 3
        assert d["template_label"] == "Fiction Novel"
        assert d["trim"] == [6.0, 9.0]
        assert d["platform"] == "kdp"
        assert d["margins"]["top"] == 0.75
        TestManuscript._file_id = d["file_id"]
        TestManuscript._token = author_session["token"]

    def test_preview_with_token(self):
        assert TestManuscript._file_id, "compose must run first"
        r = requests.get(
            f"{API}/manuscript/preview/{TestManuscript._file_id}",
            params={"token": TestManuscript._token},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/pdf")
        # Validate PDF opens
        with pikepdf.open(io.BytesIO(r.content)) as pdf:
            assert len(pdf.pages) >= 3

    def test_preview_missing_token_401(self):
        assert TestManuscript._file_id
        # Use fresh session with no cookie
        r = requests.get(f"{API}/manuscript/preview/{TestManuscript._file_id}")
        assert r.status_code == 401

    def test_compose_bad_template(self, author_session):
        r = author_session["session"].post(f"{API}/manuscript/compose", json={
            "template": "nonexistent",
            "title": "x", "source_text": "y",
        })
        assert r.status_code == 400


# ============================================================
# Cover export barcode overlay
# ============================================================
class TestCoverBarcodeOverlay:
    def test_cover_with_isbn_has_extra_image(self, cover_png, tmp_path):
        # Fresh user to avoid book-counter contention with other tests
        s = requests.Session()
        email = f"qa+coverbc{TS}@example.com"
        r = s.post(f"{API}/auth/register", json={"email": email, "password": "TestPass123!"})
        assert r.status_code == 200
        upgrade(r.json()["user"]["id"], "studio")  # studio has higher book limit
        s.post(f"{API}/auth/login", json={"email": email, "password": "TestPass123!"})

        # Project WITHOUT ISBN
        pid_no = _create_project(s, "cover", f"TEST_cover_noisbn_{TS}")
        _upload(s, pid_no, cover_png, "image/png")
        r_no = s.post(f"{API}/projects/{pid_no}/export")
        assert r_no.status_code == 200, r_no.text
        pdf_no = s.get(f"{BASE_URL}{r_no.json()['download_url']}").content

        # Project WITH ISBN
        pid_yes = _create_project(s, "cover", f"TEST_cover_isbn_{TS}")
        _upload(s, pid_yes, cover_png, "image/png")
        r_set = s.patch(f"{API}/projects/{pid_yes}/isbn", json={"isbn": "9780316148412"})
        assert r_set.status_code == 200
        r_yes = s.post(f"{API}/projects/{pid_yes}/export")
        assert r_yes.status_code == 200, r_yes.text
        pdf_yes = s.get(f"{BASE_URL}{r_yes.json()['download_url']}").content

        # Both must open with pikepdf (not corrupted)
        with pikepdf.open(io.BytesIO(pdf_no)) as pnf:
            no_res = pnf.pages[0].Resources
            no_xobj = list(no_res.XObject.keys()) if "/XObject" in no_res else []
        with pikepdf.open(io.BytesIO(pdf_yes)) as pyf:
            yes_res = pyf.pages[0].Resources
            yes_xobj = list(yes_res.XObject.keys()) if "/XObject" in yes_res else []
        print(f"XObjects no-ISBN: {len(no_xobj)}, with-ISBN: {len(yes_xobj)}")
        assert len(yes_xobj) > len(no_xobj), (
            f"Expected barcode overlay to add an image XObject on cover. "
            f"no-ISBN={no_xobj}, yes-ISBN={yes_xobj}"
        )


# ============================================================
# Stripe webhook (no signature secret set)
# ============================================================
class TestStripeWebhook:
    def test_audit_099_paid(self):
        # Insert an audit + payment_transactions row referencing it
        audit_id = f"aud_test_{TS}"
        session_id = f"cs_test_audit_{TS}"
        asyncio.run(_insert_audit({
            "audit_id": audit_id, "paid": False,
            "created_at": time.time(),
        }))
        asyncio.run(_insert_txn({
            "session_id": session_id, "product": "audit_099",
            "audit_id": audit_id, "payment_status": "unpaid",
        }))
        evt = {
            "type": "checkout.session.completed",
            "data": {"object": {"id": session_id, "subscription": None}},
        }
        r = requests.post(f"{API}/stripe/webhook", json=evt)
        assert r.status_code == 200, r.text
        assert r.json()["event_type"] == "checkout.session.completed"
        # Confirm audit flagged paid
        a = asyncio.run(_find_audit(audit_id))
        assert a and a.get("paid") is True

    def test_subscription_upgrades_user(self, free_session):
        uid = free_session["user_id"]
        session_id = f"cs_test_sub_{TS}"
        sub_id = f"sub_test_{TS}"
        asyncio.run(_insert_txn({
            "session_id": session_id,
            "product": "subscription",
            "user_id": uid,
            "tier": "pro",
            "payment_status": "unpaid",
        }))
        evt = {
            "type": "checkout.session.completed",
            "data": {"object": {"id": session_id, "subscription": sub_id}},
        }
        r = requests.post(f"{API}/stripe/webhook", json=evt)
        assert r.status_code == 200, r.text
        u = asyncio.run(_find_user(uid))
        assert u["tier"] == "pro"
        assert u.get("stripe_subscription_id") == sub_id
        assert u.get("subscription_status") == "active"

    def test_subscription_deleted_downgrades_to_free(self, free_session):
        # Reuse subscription id from previous test; ensure user gets set back
        uid = free_session["user_id"]
        sub_id = f"sub_test_{TS}"
        evt = {
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": sub_id}},
        }
        r = requests.post(f"{API}/stripe/webhook", json=evt)
        assert r.status_code == 200
        u = asyncio.run(_find_user(uid))
        assert u["tier"] == "free"
        assert u.get("subscription_status") == "canceled"

    def test_invoice_payment_failed_sets_past_due(self, author_session):
        # Assign a subscription id to author user first
        uid = author_session["user_id"]
        sub_id = f"sub_past_due_{TS}"
        asyncio.run(_mongo_set_sub(uid, sub_id))
        evt = {
            "type": "invoice.payment_failed",
            "data": {"object": {"subscription": sub_id}},
        }
        r = requests.post(f"{API}/stripe/webhook", json=evt)
        assert r.status_code == 200
        u = asyncio.run(_find_user(uid))
        assert u.get("subscription_status") == "past_due"


async def _mongo_set_sub(uid, sub_id):
    db = await _mongo()
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"stripe_subscription_id": sub_id}})


# ============================================================
# Regression: interior export multi-page + book counter
# ============================================================
class TestInteriorRegression:
    def test_interior_multi_page(self, manuscript_pdf):
        s = requests.Session()
        email = f"qa+intreg{TS}@example.com"
        r = s.post(f"{API}/auth/register", json={"email": email, "password": "TestPass123!"})
        assert r.status_code == 200
        upgrade(r.json()["user"]["id"], "author")
        s.post(f"{API}/auth/login", json={"email": email, "password": "TestPass123!"})

        pid = _create_project(s, "interior", f"TEST_intreg_{TS}")
        _upload(s, pid, manuscript_pdf, "application/pdf")
        r = s.post(f"{API}/projects/{pid}/export")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["page_count"] == 3
        assert d["pdf_standard"] == "PDF/X-1a:2001"
        assert d["books_this_month"] == 1
        # second interior => 402
        pid2 = _create_project(s, "interior", f"TEST_intreg2_{TS}")
        _upload(s, pid2, manuscript_pdf, "application/pdf")
        r2 = s.post(f"{API}/projects/{pid2}/export")
        assert r2.status_code == 402
