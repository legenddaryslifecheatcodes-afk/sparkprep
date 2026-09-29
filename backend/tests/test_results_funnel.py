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
    # Genuinely print-ready: SparkPrep's own PDF/X-1a export of that manuscript (the raw reportlab PDF
    # really does have issues -- no PDF/X declaration, fonts not embedded).
    from file_processor import build_interior_pdf_x1a
    d = tempfile.mkdtemp()
    src, out = os.path.join(d, "src.pdf"), os.path.join(d, "out.pdf")
    Path(src).write_bytes(buf.getvalue())
    build_interior_pdf_x1a(src, out, 6, 9, 0.125, title="Clean Book", author="A")
    r = client.post(f"/api/projects/{pid}/slot-upload/interior", files={"file": ("b.pdf", Path(out).read_bytes(), "application/pdf")})
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


def test_one_audit_the_editor_unlock_gives_the_same_full_report(client, stripe_fake):
    pid, _ = rgb_cover_project(client)
    co = client.post(f"/api/projects/{pid}/results-unlock/checkout", json={"origin_url": "https://sparkprep.legenddary.com"}).json()
    unpaid = client.get(f"/api/audit/{co['audit_id']}").json()
    assert unpaid["full_report"] is None and "preview" not in unpaid
    pay(client, co["session_id"])

    report = client.get(f"/api/audit/{co['audit_id']}").json()            # the same report page the /audit flow uses
    assert report["paid"] and report["full_report"]
    for f in report["full_report"]:
        assert f["title"].startswith("Cover: ") and f["publisher_rule"] and f["why_it_fails"]
        assert not any(k in f for k in ("fix_steps", "fix_tools", "est_fix_minutes"))
    pdf = client.get(f"/api/audit/{co['audit_id']}/report")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_no_free_preview_before_paying_for_the_no_account_audit(client):
    aid = client.post("/api/audit/start", json={"platform": "kdp", "trim_size": "6x9", "file_type": "interior"}).json()["audit_id"]
    buf = io.BytesIO()
    Image.new("RGB", (900, 1350), (200, 40, 40)).save(buf, "JPEG")
    up = client.post(f"/api/audit/{aid}/upload", files={"file": ("page.jpg", buf.getvalue(), "image/jpeg")}).json()
    assert set(up["summary"]) == {"total_issues"} and up["summary"]["total_issues"] > 0 and "preview" not in up
    got = client.get(f"/api/audit/{aid}").json()
    assert set(got["summary"]) == {"total_issues"} and "preview" not in got and got["full_report"] is None
    assert "color_mode" not in got["file_metadata"] and "dpi_x" not in got["file_metadata"]
    text = json.dumps(got)
    assert "RGB" not in text and "Resolution" not in text


def three_page_interior_with_a_page_3_problem():
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch))
    for page in range(3):
        c.setFont("Times-Roman", 11)
        for i in range(20):
            x = 0.05 * inch if page == 2 else 1.1 * inch          # page 3's text runs into the trim edge
            c.drawString(x, (8.0 - i * 0.3) * inch, "Body text line that should sit inside the safe margins of the page.")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_prices_follow_the_99_day_special(client, monkeypatch):
    from datetime import timedelta
    p = client.get("/api/pricing").json()
    assert (p["audit"]["price_cents"], p["audit"]["regular_cents"]) == (199, 999)
    assert (p["advanced_audit"]["price_cents"], p["advanced_audit"]["regular_cents"]) == (999, 1999)
    assert p["special"]["active"] is True
    monkeypatch.setattr(book_pass.config, "SPECIAL_END", datetime.now(timezone.utc) - timedelta(seconds=1))
    p = client.get("/api/pricing").json()
    assert p["audit"]["price_cents"] == 999 and p["advanced_audit"]["price_cents"] == 1999 and not p["special"]["active"]


def test_advanced_audit_checks_every_page_and_an_upgrade_costs_only_the_difference(client, stripe_fake):
    aid = client.post("/api/audit/start", json={"platform": "kdp", "trim_size": "6x9", "file_type": "interior"}).json()["audit_id"]
    assert client.post(f"/api/audit/{aid}/upload", files={"file": ("book.pdf", three_page_interior_with_a_page_3_problem(), "application/pdf")}).status_code == 200

    std = client.post(f"/api/audit/{aid}/checkout", json={"origin_url": "https://sparkprep.legenddary.com"}).json()
    assert std["amount_cents"] == 199
    pay(client, std["session_id"])
    standard = client.get(f"/api/audit/{aid}").json()
    assert standard["level"] == "standard" and standard["can_upgrade"] is True
    assert not any("margin" in f["id"] for f in standard["full_report"]), "page 1 is clean; the standard audit only checks page 1"

    up = client.post(f"/api/audit/{aid}/checkout", json={"origin_url": "https://sparkprep.legenddary.com", "level": "advanced"}).json()
    assert up["amount_cents"] == 999 - 199                                     # only the difference
    pay(client, up["session_id"])
    advanced = client.get(f"/api/audit/{aid}").json()
    assert advanced["level"] == "advanced" and advanced["can_upgrade"] is False
    assert any("margin" in f["id"] for f in advanced["full_report"]), "the page-3 problem must be caught by the Advanced Audit"

    r = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com", "audit_id": aid})
    assert r.status_code == 200 and r.json()["audit_credit_cents"] == 999      # everything paid is credited


def test_advanced_audit_is_for_interiors(client, stripe_fake):
    aid = client.post("/api/audit/start", json={"platform": "kdp", "trim_size": "6x9", "file_type": "cover", "binding": "paperback",
                                                "page_count": 100, "paper_type": "white_50lb"}).json()["audit_id"]
    r = client.post(f"/api/audit/{aid}/checkout", json={"origin_url": "https://sparkprep.legenddary.com", "level": "advanced"})
    assert r.status_code == 400 and "interior" in r.json()["detail"]


def test_editor_can_buy_the_advanced_audit_directly(client, stripe_fake):
    pid, _ = clean_interior_project(client)
    r = client.post(f"/api/projects/{pid}/results-unlock/checkout", json={"origin_url": "https://sparkprep.legenddary.com", "level": "advanced"})
    assert r.status_code == 200 and r.json()["amount_cents"] == 999


def test_the_book_unlocks_repairs_and_results(client):
    pid, _ = rgb_cover_project(client)
    asyncio.run(book_pass.entitlements.grant_credit(server.db, uid(), "pass", dedupe_key=f"rf:{time.time_ns()}"))
    assert client.post(f"/api/projects/{pid}/activate-book").status_code == 200
    p = client.get(f"/api/projects/{pid}").json()
    assert p["results_locked"] is False and p["owns_product"] is True
    r = client.post(f"/api/projects/{pid}/autofix/verified?stream=false")
    assert r.status_code == 200, r.text
