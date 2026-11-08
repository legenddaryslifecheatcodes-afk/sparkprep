"""The Holiday Free Audit: one free standard audit per signed-in account, switchable by env. In-memory DB, no Stripe."""
import asyncio
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="sp_fa_"), USE_MEMORY_DB="1",
                  JWT_SECRET=os.environ.get("JWT_SECRET") or "fa-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

USERS = ("free-audit-a@example.com", "free-audit-b@example.com")


@pytest.fixture(autouse=True)
def book_mode(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")
    monkeypatch.delenv("FREE_AUDIT", raising=False)
    monkeypatch.setenv("FREE_AUDIT_END", "2999-01-01T00:00:00+00:00")
    server.db.free_audit_claims._docs.clear()


@pytest.fixture(scope="module")
def client():
    for email in USERS:
        if not asyncio.run(server.db.users.find_one({"email": email})):
            asyncio.run(server.db.users.insert_one({
                "email": email, "password_hash": server.hash_password("TestPass123!"), "name": "F", "tier": "free",
                "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        yield c


def login(client, email=USERS[0]):
    r = client.post("/api/auth/login", json={"email": email, "password": "TestPass123!"})
    return {"Authorization": "Bearer " + r.json()["token"]}


def uploaded_audit(client):
    from PIL import Image
    aid = client.post("/api/audit/start", json={"platform": "kdp", "trim_size": "6x9", "file_type": "interior"}).json()["audit_id"]
    buf = io.BytesIO()
    Image.new("RGB", (300, 450), "white").save(buf, "PNG")
    r = client.post(f"/api/audit/{aid}/upload", files={"file": ("page.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    return aid


def test_pricing_shows_offer(client):
    fa = client.get("/api/pricing").json()["free_audit"]
    assert fa["active"] is True and fa["per_account"] == 1
    assert client.get("/api/pricing").json()["audit"]["price_cents"] > 0      # the paid price is unchanged


def test_needs_an_account(client):
    aid = uploaded_audit(client)
    assert client.post(f"/api/audit/{aid}/claim-free").status_code == 401
    assert client.get(f"/api/audit/{aid}").json()["paid"] is False


def test_one_free_audit_per_account(client):
    h = login(client)
    first, second = uploaded_audit(client), uploaded_audit(client)
    r = client.post(f"/api/audit/{first}/claim-free", headers=h)
    assert r.status_code == 200 and r.json()["free"] is True
    a = client.get(f"/api/audit/{first}").json()
    assert a["paid"] is True and a["full_report"] is not None
    assert a["credit_cents"] == 0                       # free is not a payment: no credit toward a book
    assert client.get("/api/free-audit/status", headers=h).json()["used"] is True
    assert client.post(f"/api/audit/{second}/claim-free", headers=h).status_code == 409
    assert client.get(f"/api/audit/{second}").json()["paid"] is False
    # a different account still gets its own
    assert client.post(f"/api/audit/{second}/claim-free", headers=login(client, USERS[1])).status_code == 200


def test_switch_off_and_end_date(client, monkeypatch):
    h = login(client)
    aid = uploaded_audit(client)
    monkeypatch.setenv("FREE_AUDIT", "off")
    assert client.get("/api/pricing").json()["free_audit"]["active"] is False
    assert client.post(f"/api/audit/{aid}/claim-free", headers=h).status_code == 410
    monkeypatch.setenv("FREE_AUDIT", "auto")
    monkeypatch.setenv("FREE_AUDIT_END", "2020-01-01T00:00:00+00:00")
    assert client.post(f"/api/audit/{aid}/claim-free", headers=h).status_code == 410
    monkeypatch.setenv("FREE_AUDIT", "on")
    assert client.post(f"/api/audit/{aid}/claim-free", headers=h).status_code == 200
