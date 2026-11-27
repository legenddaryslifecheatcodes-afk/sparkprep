"""Site Insights (owner's ask, 2026-11-27): the server notes the steps a visitor reaches and the trouble they hit,
without changing any answer, and only the owner can read the dashboard numbers."""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_insights_"), USE_MEMORY_DB="1", JWT_SECRET="insights-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import site_insights  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ADMIN = "insights-owner@example.com"
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0 Safari/537.36"


def _settle():
    time.sleep(0.2)   # recording happens in a background task after the answer is sent


def test_rules():
    assert site_insights.step_for("POST", "/api/audit/abc123/upload", 200) == "uploaded"
    assert site_insights.step_for("POST", "/api/audit/abc123/upload", 500) is None
    assert site_insights.step_for("POST", "/api/audit/abc/claim-free-email", 200) == "saw_results"
    assert site_insights.step_for("POST", "/api/auth/register", 200) == "signed_up"
    assert site_insights.step_for("POST", "/api/payments/book-pass", 200) == "opened_checkout"
    assert site_insights.step_for("POST", "/api/projects/p1/results-unlock/checkout", 200) == "opened_checkout"
    assert site_insights.step_for("GET", "/api/projects", 200) is None
    assert site_insights.is_trouble("POST", "/api/ai/cover-plan", 402)
    assert not site_insights.is_trouble("GET", "/api/auth/me", 401)        # just "not logged in", normal
    assert site_insights.friendly_api("/api/ai/cover-plan") == "AI cover designer"
    assert "[email]" in site_insights.scrub("failed for jo@example.com")
    assert site_insights._detail_from_body(b'{"detail": "Needs the Author plan."}') == "Needs the Author plan."


def test_summary_funnel_and_drop():
    day = "2026-11-27"
    views = [
        {"day": day, "at": f"{day}T10:00:0{i}+00:00", "visitor": f"v{i}", "event": "pageview", "path": "/",
         "source": "Google", "source_kind": "Search", "device": "Phone"} for i in range(4)
    ] + [{"day": day, "at": f"{day}T10:01:00+00:00", "visitor": "v0", "event": "pageview", "path": "/audit",
          "source": "", "source_kind": "Internal", "device": "Phone"}]
    steps = [{"day": day, "at": f"{day}T10:02:00+00:00", "visitor": "v0", "step": "uploaded", "path": "/api/audit/:id/upload"}]
    trouble = [{"day": day, "at": f"{day}T10:03:00+00:00", "visitor": "v1", "kind": "api", "status": 402,
                "where": "AI cover designer", "message": "Needs a plan.", "path": "/api/ai/cover-plan", "page": ""}]
    out = site_insights.summarize(views, steps, trouble, paid=0, day_list=[day],
                                  now=datetime(2026, 11, 27, 10, 5, tzinfo=timezone.utc))
    f = {r["key"]: r["count"] for r in out["funnel"]}
    assert f["visited"] == 4 and f["opened_check"] == 1 and f["uploaded"] == 1 and f["paid"] == 0
    assert out["biggest_drop"] == "opened_check"            # 4 visited, only 1 opened the free check
    assert out["totals"]["hit_trouble"] == 1 and out["totals"]["took_a_step"] == 1
    assert out["trouble"][0]["where"] == "AI cover designer" and out["trouble"][0]["visitors"] == 1
    assert out["by_source"][0]["name"] == "Google" and out["by_source"][0]["took_step"] == 1
    assert ("Home page", 3) in out["exit_pages"]
    assert any(e["type"] == "trouble" for jr in out["journeys"] for e in jr["events"])
    assert out["headlines"]


def test_watcher_records_and_owner_only(monkeypatch):
    asyncio.run(server.db.users.insert_one({"email": ADMIN, "password_hash": server.hash_password("RightPass123!"),
                                            "name": "T", "tier": "free", "created_at": datetime.now(timezone.utc).isoformat(),
                                            "exports_this_month": 0, "books_this_month": 0}))
    monkeypatch.setattr(server, "ADMIN_EMAIL", ADMIN)
    person = {"x-forwarded-for": "10.9.9.9", "user-agent": CHROME}
    before_views = list(server.db.pageviews._docs)
    try:
        _watcher_flow(person)
    finally:   # leave the shared in-memory database as we found it, so other tests' counts aren't changed
        server.db.pageviews._docs[:] = before_views
        server.db.site_steps._docs[:] = []
        server.db.site_trouble._docs[:] = []


def _watcher_flow(person):
    with TestClient(server.app) as c:
        c.post("/api/pv", content=json.dumps({"p": "/register", "r": "https://www.tiktok.com/"}),
               headers={**person, "content-type": "text/plain"})
        r = c.post("/api/auth/register", json={"email": "new-author@example.com", "password": "GoodPass123!"}, headers=person)
        assert r.status_code == 200                          # the watcher never changes an answer
        tok = r.json()["token"]
        r2 = c.post("/api/ai/cover-plan", json={"title": "My Book", "layouts": ["classic"], "styles": ["noir"]},
                    headers={**person, "Authorization": f"Bearer {tok}"})
        assert r2.status_code == 402
        c.post("/api/trouble", content=json.dumps({"k": "crash", "m": "TypeError: x is undefined", "p": "/cover-studio"}),
               headers={**person, "content-type": "text/plain"})
        c.post("/api/auth/register", json={"email": "bot@example.com", "password": "GoodPass123!"},
               headers={"x-forwarded-for": "10.9.9.8", "user-agent": "python-requests/2.31"})
        _settle()

        steps = server.db.site_steps._docs
        troubles = server.db.site_trouble._docs
        assert [s["step"] for s in steps] == ["signed_up"]   # the bot's sign-up isn't counted
        assert any(t["status"] == 402 and t["where"] == "AI cover designer" and t["message"] for t in troubles)
        assert any(t["kind"] == "crash" for t in troubles)
        assert all("10.9.9" not in str(d) for d in steps + troubles)   # raw IP never saved
        assert all("new-author@example.com" not in str(d) for d in troubles)

        assert c.get("/api/admin/insights").status_code in (401, 403)
        assert c.get("/api/admin/insights", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
        atok = c.post("/api/auth/login", json={"email": ADMIN, "password": "RightPass123!"}).json()["token"]
        data = c.get("/api/admin/insights?days=1", headers={"Authorization": f"Bearer {atok}"}).json()
        f = {row["key"]: row["count"] for row in data["funnel"]}
        assert f["visited"] >= 1 and f["signed_up"] == 1
        assert data["totals"]["hit_trouble"] >= 1
        assert data["trouble"] and data["journeys"]
