"""Tests for iteration 4: /api/ai/blurb, /api/projects/{id}/template-upload, and regressions."""
import os
import time
import io
import pytest
import requests
from reportlab.pdfgen import canvas
from PIL import Image

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if os.environ.get("REACT_APP_BACKEND_URL") else "https://sparkprep-print.preview.emergentagent.com"
API = f"{BASE_URL}/api"

TS = int(time.time())
EMAIL = f"qa+{TS}@example.com"
PW = "test1234"


@pytest.fixture(scope="session")
def auth():
    r = requests.post(f"{API}/auth/register", json={"email": EMAIL, "password": PW, "name": "QA"})
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
    tok = r.json()["token"]
    return {"token": tok, "headers": {"Authorization": f"Bearer {tok}"}}


@pytest.fixture(scope="session")
def project(auth):
    r = requests.post(f"{API}/projects", json={
        "name": "TEST_QA_Project", "platform": "kdp", "trim_size": "6x9",
        "paper_type": "white_50lb", "binding": "paperback", "page_count": 200,
        "project_type": "cover",
    }, headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="session")
def small_pdf(tmp_path_factory):
    p = tmp_path_factory.mktemp("pdf") / "sample.pdf"
    c = canvas.Canvas(str(p), pagesize=(6 * 72, 9 * 72))
    c.drawString(72, 72, "Test")
    c.showPage()
    c.save()
    return str(p)


@pytest.fixture(scope="session")
def small_jpg(tmp_path_factory):
    p = tmp_path_factory.mktemp("img") / "sample.jpg"
    img = Image.new("RGB", (1800, 2700), "white")
    img.save(str(p), "JPEG", dpi=(300, 300))
    return str(p)


# ---------- AI Blurb ----------
class TestBlurb:
    def test_blurb_no_auth(self):
        r = requests.post(f"{API}/ai/blurb", json={"title": "Test"})
        assert r.status_code == 401

    def test_blurb_full(self, auth):
        r = requests.post(f"{API}/ai/blurb", json={
            "title": "The Long Print", "genre": "Literary Thriller",
            "page_count": 320, "themes": "obsession, secrets"
        }, headers=auth["headers"], timeout=90)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "tagline" in data
        assert isinstance(data["tagline"], str) and len(data["tagline"]) > 0
        assert "variations" in data
        assert len(data["variations"]) == 3, f"Expected 3 variations, got {len(data['variations'])}"
        tones = {v.get("tone", "").lower() for v in data["variations"]}
        # at least the three requested tones present
        for v in data["variations"]:
            assert "copy" in v and isinstance(v["copy"], str)
            wc = len(v["copy"].split())
            assert 50 <= wc <= 250, f"variation word count {wc} out of range"

    def test_blurb_title_only(self, auth):
        r = requests.post(f"{API}/ai/blurb", json={"title": "Solo Title"}, headers=auth["headers"], timeout=90)
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data.get("variations", [])) == 3


# ---------- Template upload ----------
class TestTemplateUpload:
    def test_upload_pdf(self, auth, project, small_pdf):
        with open(small_pdf, "rb") as f:
            r = requests.post(
                f"{API}/projects/{project['id']}/template-upload",
                files={"file": ("tmpl.pdf", f, "application/pdf")},
                headers=auth["headers"],
            )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "template_id" in data and "metadata" in data
        dt = data["detected_trim"]
        assert dt is not None, "PDF should yield detected_trim"
        for k in ("raw_width_inches", "raw_height_inches", "estimated_trim_width", "estimated_trim_height"):
            assert k in dt
        # Verify persistence
        g = requests.get(f"{API}/projects/{project['id']}", headers=auth["headers"])
        assert g.status_code == 200
        gp = g.json()
        assert gp.get("publisher_template") == data["template_id"]
        assert gp.get("publisher_template_metadata") is not None

    def test_upload_bad_ext(self, auth, project, tmp_path):
        bad = tmp_path / "x.txt"
        bad.write_text("hello")
        with open(bad, "rb") as f:
            r = requests.post(
                f"{API}/projects/{project['id']}/template-upload",
                files={"file": ("x.txt", f, "text/plain")},
                headers=auth["headers"],
            )
        assert r.status_code == 400, r.text


# ---------- Regressions ----------
class TestRegressions:
    def test_projects_list(self, auth):
        r = requests.get(f"{API}/projects", headers=auth["headers"])
        assert r.status_code == 200
        assert "projects" in r.json()

    def test_upload_jpg(self, auth, project, small_jpg):
        with open(small_jpg, "rb") as f:
            r = requests.post(
                f"{API}/projects/{project['id']}/upload",
                files={"file": ("cover.jpg", f, "image/jpeg")},
                headers=auth["headers"],
            )
        assert r.status_code == 200, r.text
        data = r.json()
        assert "file_metadata" in data and "compliance" in data

    def test_autofix(self, auth, project):
        r = requests.post(f"{API}/projects/{project['id']}/autofix", headers=auth["headers"])
        assert r.status_code == 200, r.text

    def test_export(self, auth, project):
        r = requests.post(f"{API}/projects/{project['id']}/export", headers=auth["headers"])
        assert r.status_code == 200, r.text
        assert "download_url" in r.json()

    def test_checkout_pro(self, auth):
        r = requests.post(f"{API}/payments/checkout", json={
            "tier": "pro", "origin_url": BASE_URL,
        }, headers=auth["headers"])
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["checkout_url"].startswith("https://checkout.stripe.com")
