"""Tests for $0.99 Print Failure Audit anonymous product."""
import os
import io
import pytest
import requests
from PIL import Image

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="session")
def small_png(tmp_path_factory):
    p = tmp_path_factory.mktemp("img") / "sample.png"
    img = Image.new("RGB", (600, 900), "white")
    img.save(str(p), "PNG")
    return str(p)


# ---------- /api/audit/start ----------
class TestAuditStart:
    def test_start_ok(self):
        r = requests.post(f"{API}/audit/start", json={"platform": "kdp", "trim_size": "6x9"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "audit_id" in data and isinstance(data["audit_id"], str) and len(data["audit_id"]) > 10

    def test_start_bad_platform(self):
        r = requests.post(f"{API}/audit/start", json={"platform": "bogus", "trim_size": "6x9"})
        assert r.status_code == 400, r.text

    def test_start_bad_trim(self):
        r = requests.post(f"{API}/audit/start", json={"platform": "kdp", "trim_size": "9999x9999"})
        assert r.status_code == 400, r.text


# ---------- /api/audit/{id}/upload ----------
class TestAuditUpload:
    @pytest.fixture(scope="class")
    def audit_id(self):
        r = requests.post(f"{API}/audit/start", json={"platform": "kdp", "trim_size": "6x9"})
        return r.json()["audit_id"]

    def test_upload_png(self, audit_id, small_png):
        with open(small_png, "rb") as f:
            r = requests.post(
                f"{API}/audit/{audit_id}/upload",
                files={"file": ("sample.png", f, "image/png")},
            )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["audit_id"] == audit_id
        s = data["summary"]
        for k in ("total_issues", "critical_failures", "warnings", "estimated_fix_minutes", "rejection_risk"):
            assert k in s, f"Missing summary key: {k}"
        assert isinstance(data["preview"], list)
        # 600x900 raster => low DPI vs 6x9 trim, should have some findings
        for item in data["preview"]:
            for req_k in ("id", "severity", "title", "why_it_fails", "one_click_fix"):
                assert req_k in item
            assert len(item["why_it_fails"]) <= 121, f"why_it_fails too long: {len(item['why_it_fails'])}"
            # Preview must NOT leak paid fields
            for forbidden in ("fix_steps", "publisher_rule", "pinpoint"):
                assert forbidden not in item, f"Preview leaked {forbidden}"

    def test_upload_bad_ext(self, audit_id, tmp_path):
        bad = tmp_path / "x.txt"
        bad.write_text("hello")
        with open(bad, "rb") as f:
            r = requests.post(
                f"{API}/audit/{audit_id}/upload",
                files={"file": ("x.txt", f, "text/plain")},
            )
        assert r.status_code == 400, r.text


# ---------- /api/audit/{id} GET, before/after paid ----------
class TestAuditGetAndPaidPath:
    @pytest.fixture(scope="class")
    def audit_id(self, small_png):
        r = requests.post(f"{API}/audit/start", json={"platform": "kdp", "trim_size": "6x9"})
        aid = r.json()["audit_id"]
        with open(small_png, "rb") as f:
            requests.post(f"{API}/audit/{aid}/upload", files={"file": ("s.png", f, "image/png")})
        return aid

    def test_get_before_paid(self, audit_id):
        r = requests.get(f"{API}/audit/{audit_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["paid"] is False
        assert data["full_report"] is None
        assert data["preview"] is not None
        assert data["summary"] is not None

    def test_get_after_manual_paid(self, audit_id):
        # Flip paid=true directly in Mongo via admin route? None exists. Use motor via env.
        from pymongo import MongoClient
        mongo_url = os.environ["MONGO_URL"]
        db_name = os.environ.get("DB_NAME", "test_database")
        client = MongoClient(mongo_url)
        db = client[db_name]
        res = db.audits.update_one({"audit_id": audit_id}, {"$set": {"paid": True}})
        assert res.matched_count == 1, "audit doc not found in db.audits"

        r = requests.get(f"{API}/audit/{audit_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["paid"] is True
        assert isinstance(data["full_report"], list)
        assert len(data["full_report"]) >= 1
        for f in data["full_report"]:
            for k in ("fix_steps", "publisher_rule", "pinpoint"):
                assert k in f, f"Full report missing {k}"


# ---------- /api/audit/{id}/checkout ----------
class TestAuditCheckout:
    def test_checkout_creates_session(self, small_png):
        # Fresh audit
        r = requests.post(f"{API}/audit/start", json={"platform": "kdp", "trim_size": "6x9"})
        aid = r.json()["audit_id"]
        with open(small_png, "rb") as f:
            requests.post(f"{API}/audit/{aid}/upload", files={"file": ("s.png", f, "image/png")})

        r = requests.post(f"{API}/audit/{aid}/checkout", json={"origin_url": BASE_URL})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "checkout.stripe.com" in data["checkout_url"]
        assert data["session_id"].startswith("cs_")

        # Verify payment_transactions record
        from pymongo import MongoClient
        client = MongoClient(os.environ["MONGO_URL"])
        db = client[os.environ.get("DB_NAME", "test_database")]
        tx = db.payment_transactions.find_one({"session_id": data["session_id"]})
        assert tx is not None
        assert tx["product"] == "audit_099"
        assert tx["amount"] == 99

        # Verify unpaid session
        v = requests.get(f"{API}/audit/{aid}/verify", params={"session_id": data["session_id"]})
        assert v.status_code == 200, v.text
        vdata = v.json()
        assert vdata["paid"] is False


# ---------- Regression checks (subset) ----------
class TestRegressions:
    def test_specs(self):
        r = requests.get(f"{API}/specs")
        assert r.status_code == 200
        assert "platforms" in r.json()

    def test_register_and_project(self, tmp_path):
        import time
        email = f"qa+audit{int(time.time())}@example.com"
        r = requests.post(f"{API}/auth/register", json={"email": email, "password": "test1234", "name": "QA"})
        assert r.status_code == 200, r.text
        tok = r.json()["token"]
        H = {"Authorization": f"Bearer {tok}"}

        # Create project
        r = requests.post(f"{API}/projects", json={
            "name": "TEST_regression", "platform": "kdp", "trim_size": "6x9",
            "paper_type": "white_50lb", "binding": "paperback", "page_count": 200,
            "project_type": "cover",
        }, headers=H)
        assert r.status_code == 200
        pid = r.json()["id"]

        # List
        assert requests.get(f"{API}/projects", headers=H).status_code == 200

        # Upload PNG
        png = tmp_path / "c.png"
        Image.new("RGB", (1800, 2700), "white").save(str(png), "PNG")
        with open(png, "rb") as f:
            r = requests.post(f"{API}/projects/{pid}/upload", files={"file": ("c.png", f, "image/png")}, headers=H)
        assert r.status_code == 200, r.text

        # Autofix
        assert requests.post(f"{API}/projects/{pid}/autofix", headers=H).status_code == 200
        # Export
        assert requests.post(f"{API}/projects/{pid}/export", headers=H).status_code == 200
        # Subscriptions checkout
        r = requests.post(f"{API}/payments/checkout", json={"tier": "pro", "origin_url": BASE_URL}, headers=H)
        assert r.status_code == 200
        assert "checkout.stripe.com" in r.json()["checkout_url"]

    def test_blurb_regression(self):
        import time
        email = f"qa+blurb{int(time.time())}@example.com"
        r = requests.post(f"{API}/auth/register", json={"email": email, "password": "test1234", "name": "QA"})
        assert r.status_code == 200
        tok = r.json()["token"]
        r = requests.post(f"{API}/ai/blurb", json={"title": "Regression Blurb"},
                          headers={"Authorization": f"Bearer {tok}"}, timeout=90)
        assert r.status_code == 200, r.text
        assert len(r.json().get("variations", [])) == 3
