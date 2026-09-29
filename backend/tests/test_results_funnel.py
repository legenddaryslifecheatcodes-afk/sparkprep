"""The owner's funnel under the book model: free demo -> $1.99 results -> the product.

  * free: the real scan runs, but only "issues found / how many" leaves the server -- enforced server-side;
  * $1.99 ("See My Results"): full results for that project. Detection only -- repairs stay locked;
  * the book: Repair Bay and every fix. The $1.99 is credited toward it.

Stripe is simulated; the app runs in-process against the in-memory database."""
import asyncio
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_rf_"), USE_MEMORY_DB="1", JWT_SECRET="rf-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import book_pass  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

EMAIL = "funnel-author@example.com"


class Obj(dict):
    __getattr__ = dict.get


@pytest.fixture(autouse=True)
def book_mode(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)


@pytest.fixture
def stripe_fake(monkeypatch):
    st = {"objects": {}}

    def create(**kw):
        sid = f"cs_rf_{time.time_ns()}"
        s = Obj(id=sid, url=f"https://checkout.stripe.test/{sid}", payment_status="unpaid", status="open", metadata=kw.get("metadata", {}))
        st["objects"][sid] = s
        st.setdefault("last", kw)
        st["last"] = kw
        return s

    monkeypatch.setattr(server.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(server.stripe.checkout.Session, "create", create)
    monkeypatch.setattr(server.stripe.checkout.Session, "retrieve", lambda sid: st["objects"][sid])
    monkeypatch.setattr(server.stripe.Coupon, "create", lambda **kw: Obj(id="coup_rf", **kw))
    return st


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "F", "tier": "free",
        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"}).json()["token"]
        yield c


def uid():
    return str(asyncio.run(server.db.users.find_one({"email": EMAIL}))["_id"])


def rgb_cover_project(client):
    """A cover whose real scan finds an issue (it's RGB)."""
    pid = client.post("/api/projects", json={"name": "Funnel Book", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                              "binding": "paperback", "page_count": 100, "project_type": "cover"}).json()["id"]
    spine = 100 / 444
    w, h = 0.125 * 2 + 12 + spine, 9.25
    buf = io.BytesIO()
    Image.new("RGB", (round(w * 300), round(h * 300)), (200, 30, 30)).save(buf, "JPEG", dpi=(300, 300))
    r = client.post(f"/api/projects/{pid}/slot-upload/full_wrap", files={"file": ("c.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200, r.text
    return pid, r.json()


def clean_interior_project(client):
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    pid = client.post("/api/projects", json={"name": "Clean Book", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                              "binding": "paperback", "page_count": 100, "project_type": "interior"}).json()["id"]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch))
    for _ in range(2):
        c.setFont("Times-Roman", 11)
        for i in range(20):
            c.drawString(1.1 * inch, (8.0 - i * 0.3) * inch, "Centered body text well inside the safe margins of the page.")
        c.showPage()
    c.save()
    r = client.post(f"/api/projects/{pid}/slot-upload/interior", files={"file": ("b.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 200, r.text
    return pid, r.json()


def pay(client, session_id):
    body = json.dumps({"id": "evt", "type": "checkout.session.completed",
                       "data": {"object": {"id": session_id, "payment_status": "paid", "status": "complete"}}})
    assert client.post("/api/stripe/webhook", content=body, headers={"content-type": "application/json"}).status_code == 200


REPAIRS = [("post", "/autofix/verified?stream=false"), ("post", "/autofix"), ("post", "/autofix/confirm"),
           ("post", "/final-review"), ("post", "/ai-enhance/full_wrap")]


def test_free_scan_is_real_but_only_the_count_leaves_the_server(client):
    pid, upload = rgb_cover_project(client)
    assert upload["compliance"] == [] and upload["results_locked"] is True
    assert upload["results_summary"]["issues"] >= 1

    p = client.get(f"/api/projects/{pid}").json()
    assert p["results_locked"] is True and p["owns_product"] is False
    assert p["slots"]["full_wrap"]["compliance"] == [] and p["compliance"] == []
    assert p["results_summary"]["issues"] == upload["results_summary"]["issues"]
    assert "Color space" in p["results_summary"]["checks_run"]
    text = json.dumps(p) + json.dumps(client.get("/api/projects").json())
    assert "RGB" not in text and "colors may shift" not in text          # no result detail anywhere in the payloads
    # ...while the real scan really is stored, ready to unlock
    stored = asyncio.run(server.db.projects.find_one({"_id": server.ObjectId(pid)}))
    assert any(c["status"] != "pass" for c in stored["slots"]["full_wrap"]["compliance"])


def test_a_clean_file_is_reported_honestly_as_clean(client):
    pid, upload = clean_interior_project(client)
    assert upload["results_locked"] is True and upload["results_summary"]["issues"] == 0


def test_no_repairs_without_the_book(client):
    pid, _ = rgb_cover_project(client)
    for method, path in REPAIRS:
        r = getattr(client, method)(f"/api/projects/{pid}{path}")
        assert r.status_code == 402, (path, r.status_code, r.text)
        assert r.json()["detail"]["code"] == "book_required"


def test_1_99_unlocks_results_only_and_is_credited_toward_the_book(client, stripe_fake):
    pid, _ = rgb_cover_project(client)
    r = client.post(f"/api/projects/{pid}/results-unlock/checkout", json={"origin_url": "https://sparkprep.legenddary.com"})
    assert r.status_code == 200, r.text
    co = r.json()
    assert stripe_fake["last"]["line_items"][0]["price_data"]["unit_amount"] == book_pass.audit_price_cents(server.AUDIT_PRICE_CENTS)
    assert stripe_fake["last"]["success_url"].startswith(f"https://sparkprep.legenddary.com/editor/{pid}?results_session=")
    assert client.get(f"/api/projects/{pid}").json()["results_locked"] is True       # not until Stripe confirms

    pay(client, co["session_id"])
    p = client.get(f"/api/projects/{pid}").json()
    assert p["results_locked"] is False and p["owns_product"] is False
    assert any(c["id"] == "colorspace" and c["status"] != "pass" for c in p["slots"]["full_wrap"]["compliance"])
    assert client.post(f"/api/projects/{pid}/results-unlock/checkout", json={"origin_url": "x"}).json() == {"already_unlocked": True}

    for method, path in REPAIRS:                                            # detection only: still no repairs
        assert getattr(client, method)(f"/api/projects/{pid}{path}").status_code == 402, path

    r = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com", "audit_id": co["audit_id"]})
    assert r.status_code == 200, r.text
    assert r.json()["audit_credit_cents"] == 199


def test_the_book_unlocks_repairs_and_results(client):
    pid, _ = rgb_cover_project(client)
    asyncio.run(book_pass.entitlements.grant_credit(server.db, uid(), "pass", dedupe_key=f"rf:{time.time_ns()}"))
    assert client.post(f"/api/projects/{pid}/activate-book").status_code == 200
    p = client.get(f"/api/projects/{pid}").json()
    assert p["results_locked"] is False and p["owns_product"] is True
    r = client.post(f"/api/projects/{pid}/autofix/verified?stream=false")
    assert r.status_code == 200, r.text
