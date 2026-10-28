"""Free tool pages ask for an email before the calculator opens (owner's ask, 2026-10-28). The emails must land
in one list the owner can download as a spreadsheet, and only the owner can download it."""
import asyncio
import csv
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_leads_"), USE_MEMORY_DB="1", JWT_SECRET="leads-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ADMIN = "leads-owner@example.com"


def _account(email, **extra):
    asyncio.run(server.db.users.insert_one({"email": email, "password_hash": server.hash_password("RightPass123!"),
                                            "name": "L", "tier": "free", "created_at": datetime.now(timezone.utc).isoformat(),
                                            "exports_this_month": 0, "books_this_month": 0, **extra}))


def test_free_tool_emails_are_saved_and_downloadable(monkeypatch):
    _account(ADMIN)
    _account("author-with-account@example.com")
    _account("guest-abc@" + server.GUEST_EMAIL_DOMAIN, is_guest=True)
    monkeypatch.setattr(server, "ADMIN_EMAIL", ADMIN)
    with TestClient(server.app) as c:
        h = {"x-forwarded-for": "10.9.9.1"}
        assert c.post("/api/leads", json={"email": "New.Author@Example.com", "source": "kdp-cover-calculator"}, headers=h).json() == {"ok": True}
        c.post("/api/leads", json={"email": "new.author@example.com", "source": "book-spine-width-calculator"}, headers=h)
        assert c.post("/api/leads", json={"email": "not-an-email", "source": "x"}, headers=h).status_code == 422

        tok = c.post("/api/auth/login", json={"email": ADMIN, "password": "RightPass123!"}).json()["token"]
        auth = {"Authorization": f"Bearer {tok}"}
        r = c.get("/api/admin/leads.csv", headers=auth)
        assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
        rows = {row["email"]: row for row in csv.DictReader(io.StringIO(r.text))}
        lead = rows["new.author@example.com"]                       # same person twice = one row
        assert lead["where_from"] == "kdp-cover-calculator book-spine-width-calculator" and lead["times_used"] == "2"
        assert rows["author-with-account@example.com"]["has_account"] == "yes"
        assert not any(e.endswith(server.GUEST_EMAIL_DOMAIN) for e in rows)   # guests have no real email

        assert c.get("/api/admin/leads", headers=auth).json()["from_free_tools"] >= 1
        other = c.post("/api/auth/login", json={"email": "author-with-account@example.com", "password": "RightPass123!"}).json()["token"]
        assert c.get("/api/admin/leads.csv", headers={"Authorization": f"Bearer {other}"}).status_code == 403
        assert c.get("/api/admin/leads.csv").status_code in (401, 403)


def test_one_connection_cannot_flood_the_list():
    with TestClient(server.app) as c:
        h = {"x-forwarded-for": "10.9.9.2"}
        codes = [c.post("/api/leads", json={"email": f"bot{i}@example.com", "source": "x"}, headers=h).status_code
                 for i in range(server.LEAD_MAX_PER_IP + 1)]
        assert codes[:-1] == [200] * server.LEAD_MAX_PER_IP and codes[-1] == 429
