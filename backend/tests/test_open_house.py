"""The 24-hour Open House: free for everyone, no sign-up, then back to normal on its own (see open_house.py).
Runs in-process against the in-memory database; no Stripe calls."""
import asyncio
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="sp_oh_"), USE_MEMORY_DB="1",
                  JWT_SECRET=os.environ.get("JWT_SECRET") or "oh-test-" + "x" * 32,
                  ADMIN_EMAIL=os.environ.get("ADMIN_EMAIL") or "root-admin@example.com")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import open_house  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EMAIL = "openhouse-reader@example.com"
ADMIN = "openhouse-admin@example.com"


def _iso(dt):
    return dt.isoformat()


@pytest.fixture(autouse=True)
def book_mode(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")
    monkeypatch.setattr(server, "ADMIN_EMAIL", ADMIN)          # read once at import, by whichever test file came first
    monkeypatch.delenv("OPEN_HOUSE", raising=False)
    server.db.settings._docs.clear()
    server.db.guest_guard._docs.clear()
    open_house.reset_cache()
    yield
    open_house.reset_cache()


def window_now(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("OPEN_HOUSE_START", _iso(now - timedelta(hours=1)))
    monkeypatch.setenv("OPEN_HOUSE_END", _iso(now + timedelta(hours=23)))


def window_over(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("OPEN_HOUSE_START", _iso(now - timedelta(hours=25)))
    monkeypatch.setenv("OPEN_HOUSE_END", _iso(now - timedelta(hours=1)))


@pytest.fixture(scope="module")
def client():
    for email, tier in ((EMAIL, "free"), (ADMIN, "studio")):
        if not asyncio.run(server.db.users.find_one({"email": email})):
            asyncio.run(server.db.users.insert_one({
                "email": email, "password_hash": server.hash_password("TestPass123!"), "name": "R", "tier": tier,
                "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        yield c


def login(client, email=EMAIL):
    r = client.post("/api/auth/login", json={"email": email, "password": "TestPass123!"})
    return {"Authorization": "Bearer " + r.json()["token"]}


def make_project(client, headers):
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
    pid = client.post("/api/projects", headers=headers, json={
        "name": "Open House Book", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
        "binding": "paperback", "page_count": 100, "project_type": "interior"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/slot-upload/interior", headers=headers,
                    files={"file": ("b.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 200, r.text
    return pid


def test_the_clock_decides_by_default():
    start, end = open_house.window()
    assert start.isoformat() == "2026-10-19T12:00:00-07:00" and end - start == timedelta(hours=24)
    assert open_house._decide("auto", start) is True
    assert open_house._decide("auto", end) is False                     # ends exactly at noon Tuesday
    assert open_house._decide("auto", start - timedelta(seconds=1)) is False
    assert open_house._decide("off", start) is False and open_house._decide("on", end) is True


def test_export_is_free_during_the_open_house_and_locked_again_after(client, monkeypatch):
    h = login(client)
    window_now(monkeypatch)
    pid = make_project(client, h)
    assert client.get("/api/open-house").json()["active"] is True
    r = client.post(f"/api/projects/{pid}/export", headers=h)
    assert r.status_code == 200, r.text
    assert client.post(f"/api/projects/{pid}/interior-check/run", headers=h).status_code == 200

    window_over(monkeypatch); open_house.reset_cache()
    r = client.post(f"/api/projects/{pid}/export", headers=h)
    assert r.status_code == 402 and r.json()["detail"]["code"] == "book_required"
    assert client.post(f"/api/projects/{pid}/interior-check/run", headers=h).status_code == 402
    assert asyncio.run(server.db.users.find_one({"email": EMAIL})).get("tier") == "free"   # nothing was saved to the account


def test_admin_switch_turns_it_off_now_and_the_env_kill_switch_beats_everything(client, monkeypatch):
    window_now(monkeypatch)
    admin = login(client, ADMIN)
    assert client.post("/api/admin/open-house", json={"mode": "off"}, headers=login(client)).status_code == 403
    r = client.post("/api/admin/open-house", json={"mode": "off"}, headers=admin)
    assert r.status_code == 200 and r.json()["active"] is False
    assert client.post("/api/auth/guest").status_code == 403
    assert client.post("/api/admin/open-house", json={"mode": "auto"}, headers=admin).json()["active"] is True

    monkeypatch.setenv("OPEN_HOUSE", "off")
    client.post("/api/admin/open-house", json={"mode": "on"}, headers=admin)
    assert client.get("/api/open-house").json()["active"] is False


def test_guests_need_no_sign_up_and_can_keep_their_work_by_adding_an_email(client, monkeypatch):
    window_now(monkeypatch)
    r = client.post("/api/auth/guest")
    assert r.status_code == 200, r.text
    g = {"Authorization": "Bearer " + r.json()["token"]}
    assert r.json()["user"]["is_guest"] is True
    pid = make_project(client, g)
    assert client.post(f"/api/projects/{pid}/export", headers=g).status_code == 200

    r = client.post("/api/auth/guest/claim", headers=g, json={"email": "kept-my-book@example.com", "password": "LongEnough1"})
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"email": "kept-my-book@example.com", "password": "LongEnough1"})
    h = {"Authorization": "Bearer " + r.json()["token"]}
    assert [p["id"] for p in client.get("/api/projects", headers=h).json()["projects"]] == [pid]
    assert client.post("/api/auth/guest/claim", headers=h, json={"email": "x@example.com", "password": "LongEnough1"}).status_code == 400


def test_guest_sign_ups_are_capped_per_connection_and_closed_outside_the_window(client, monkeypatch):
    window_now(monkeypatch)
    hdr = {"x-forwarded-for": "203.0.113.9"}
    for _ in range(server.GUEST_MAX_PER_IP):
        assert client.post("/api/auth/guest", headers=hdr).status_code == 200
    assert client.post("/api/auth/guest", headers=hdr).status_code == 429
    window_over(monkeypatch); open_house.reset_cache()
    assert client.post("/api/auth/guest", headers={"x-forwarded-for": "203.0.113.10"}).status_code == 403


def test_no_account_audit_shows_full_results_during_the_open_house_only(client, monkeypatch):
    aid = client.post("/api/audit/start", json={"platform": "kdp", "trim_size": "6x9", "file_type": "interior"}).json()["audit_id"]
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch))
    c.drawString(1.1 * inch, 7 * inch, "Body text."); c.showPage(); c.save()
    assert client.post(f"/api/audit/{aid}/upload", files={"file": ("b.pdf", buf.getvalue(), "application/pdf")}).status_code == 200

    window_over(monkeypatch); open_house.reset_cache()
    assert client.get(f"/api/audit/{aid}").json()["paid"] is False
    window_now(monkeypatch); open_house.reset_cache()
    page = client.get(f"/api/audit/{aid}").json()
    assert page["paid"] is True and page["full_report"] is not None
    assert asyncio.run(server.db.audits.find_one({"audit_id": aid})).get("paid") is False   # not saved as bought
