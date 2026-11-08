"""Site traffic tracker (owner's ask, 2026-11-08): page views from /track.js land in one place, bots are kept
apart from real people, no raw IP is stored, and only the owner can read the numbers."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_traffic_"), USE_MEMORY_DB="1", JWT_SECRET="traffic-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import traffic  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ADMIN = "traffic-owner@example.com"
CHROME = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"


def test_source_and_bot_rules():
    assert traffic.classify_source("https://www.google.com/") == ("Google", "Search")
    assert traffic.classify_source("https://chatgpt.com/c/1") == ("ChatGPT", "AI")
    assert traffic.classify_source("", "tiktok") == ("TikTok", "Social")
    assert traffic.classify_source("")[1] == "Direct"
    assert traffic.classify_source("https://sparkprep.legenddary.com/guides/")[1] == "Internal"
    assert traffic.is_bot("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
    assert traffic.is_bot("Mozilla/5.0 HeadlessChrome/120")
    assert not traffic.is_bot(CHROME)
    assert traffic._clean_path("/audit/3f2a9c1e-1111-2222-3333-444455556666/report?x=1") == "/audit/:id/report"


def test_page_views_are_counted_and_owner_only(monkeypatch):
    asyncio.run(server.db.users.insert_one({"email": ADMIN, "password_hash": server.hash_password("RightPass123!"),
                                            "name": "T", "tier": "free", "created_at": datetime.now(timezone.utc).isoformat(),
                                            "exports_this_month": 0, "books_this_month": 0}))
    monkeypatch.setattr(server, "ADMIN_EMAIL", ADMIN)
    with TestClient(server.app) as c:
        person = {"x-forwarded-for": "10.1.1.1", "user-agent": CHROME, "content-type": "text/plain"}
        bot = {"x-forwarded-for": "10.1.1.2", "user-agent": "Googlebot/2.1", "content-type": "text/plain"}
        import json
        c.post("/api/pv", content=json.dumps({"p": "/", "r": "https://www.google.com/"}), headers=person)
        c.post("/api/pv", content=json.dumps({"p": "/guides/", "r": "https://sparkprep.legenddary.com/"}), headers=person)
        c.post("/api/pv", content=json.dumps({"p": "/free-starter-kit/", "e": "kit_download", "l": "zip"}), headers=person)
        c.post("/api/pv", content=json.dumps({"p": "/"}), headers=bot)
        assert c.post("/api/pv", content="not json", headers=person).json() == {"ok": False}


        docs = server.db.pageviews._docs
        assert all("10.1.1" not in str(d) for d in docs)   # raw IP never saved

        assert c.get("/api/admin/traffic").status_code in (401, 403)
        tok = c.post("/api/auth/login", json={"email": ADMIN, "password": "RightPass123!"}).json()["token"]
        r = c.get("/api/admin/traffic?days=7", headers={"Authorization": f"Bearer {tok}"}).json()
        t = r["totals"]
        assert t["visitors"] == 1 and t["page_views"] == 2 and t["bot_views"] == 1
        assert ["Google", 1] in r["sources"]
        assert ["/guides/", 1] in r["top_pages"]
        assert r["events"][0]["event"] == "kit_download"
        assert r["bots"][0][0] == "Googlebot"
