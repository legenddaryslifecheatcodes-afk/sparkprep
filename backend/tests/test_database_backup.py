"""Backups are only worth something if they restore (owner's rule, 2026-09-29: no lockout or accident can
ever cost SparkPrep its customers). Back up -> restore into an empty database -> everything is back, exactly."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_backup_"), USE_MEMORY_DB="1", JWT_SECRET="backup-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from backup_restore import read_backup, restore_into  # noqa: E402
from bson import ObjectId  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ADMIN = "backup-owner@example.com"


def seed():
    now = datetime.now(timezone.utc).isoformat()
    for email in (ADMIN, "customer@example.com"):
        asyncio.run(server.db.users.insert_one({"email": email, "password_hash": server.hash_password("RightPass123!"),
                                                "name": "B", "tier": "free", "created_at": now,
                                                "exports_this_month": 0, "books_this_month": 0}))
    uid = str(asyncio.run(server.db.users.find_one({"email": "customer@example.com"}))["_id"])
    asyncio.run(server.db.projects.insert_one({"user_id": uid, "name": "Customer Book", "platform": "ingramspark"}))
    asyncio.run(server.db.payment_transactions.insert_one({"session_id": "cs_1", "user_id": uid, "amount": 74.99}))


def test_a_backup_restores_everything_exactly(monkeypatch):
    seed()
    monkeypatch.setattr(server, "ADMIN_EMAIL", ADMIN)
    with TestClient(server.app) as c:
        tok = c.post("/api/auth/login", json={"email": ADMIN, "password": "RightPass123!"}).json()["token"]
        r = c.get("/api/admin/backup", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
        payload = read_backup(r.content)

        other = c.post("/api/auth/login", json={"email": "customer@example.com", "password": "RightPass123!"}).json()["token"]
        assert c.get("/api/admin/backup", headers={"Authorization": f"Bearer {other}"}).status_code == 403   # owner only

    # the backup holds everything in the database (other tests' data included, when run together)
    for name in ("users", "projects", "payment_transactions"):
        assert payload["counts"][name] == asyncio.run(getattr(server.db, name).count_documents({})) >= 1
    fresh = server.MemoryDatabase()
    report = asyncio.run(restore_into(fresh, payload))
    assert report["users"] == f"restored {payload['counts']['users']}"
    back = asyncio.run(fresh.users.find_one({"email": "customer@example.com"}))
    original = asyncio.run(server.db.users.find_one({"email": "customer@example.com"}))
    assert isinstance(back["_id"], ObjectId) and back["_id"] == original["_id"]      # same account ids -> projects still link
    assert server.verify_password("RightPass123!", back["password_hash"])            # customers can still log in
    assert asyncio.run(fresh.projects.find_one({"name": "Customer Book"}))["user_id"] == str(original["_id"])

    # restoring over a database that already has data leaves it alone unless told to replace
    again = asyncio.run(restore_into(fresh, payload))
    assert again["users"].startswith("skipped")
    assert asyncio.run(fresh.users.count_documents({})) == payload["counts"]["users"]


def test_nightly_backups_keep_the_last_seven(monkeypatch):
    for day in range(1, 10):
        (server.BACKUP_DIR / f"sparkprep-backup-2026-09-{day:02d}.json.gz").write_bytes(b"old")
    path = asyncio.run(server._write_nightly_backup())
    kept = sorted(p.name for p in server.BACKUP_DIR.glob("sparkprep-backup-*.json.gz"))
    assert len(kept) == server.BACKUP_KEEP and path.name in kept
    assert read_backup(path.read_bytes())["format"] == "sparkprep-backup-v1"
