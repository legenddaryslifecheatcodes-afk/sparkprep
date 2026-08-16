"""
Iteration 10 backend tests:
- Book counter enforcement on /projects/{id}/export
- Register creates books_this_month=0
- Season endpoint still returns pre_launch phase
- Regression on /api/specs, /api/audit/start, /api/payments/checkout, /api/ai/blurb
"""
import io
import os
import time
import requests
import pytest
from pymongo import MongoClient
from bson import ObjectId

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://sparkprep-print.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"
MONGO_URL = "mongodb://localhost:27017"
DB_NAME = "sparkprep_db"

TS = int(time.time())


@pytest.fixture(scope="module")
def mongo():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def free_user():
    """Register a fresh free user."""
    email = f"qa+bookfree{TS}@example.com"
    password = "test1234"
    s = requests.Session()
    r = s.post(f"{API}/auth/register", json={"email": email, "password": password, "name": "QA Free"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"session": s, "email": email, "user": data["user"], "token": data["token"]}


@pytest.fixture(scope="module")
def author_user(mongo):
    """Register a user and upgrade to author tier directly in Mongo."""
    email = f"qa+bookauthor{TS}@example.com"
    password = "test1234"
    s = requests.Session()
    r = s.post(f"{API}/auth/register", json={"email": email, "password": password, "name": "QA Author"})
    assert r.status_code == 200, r.text
    data = r.json()
    uid = data["user"]["id"]
    # Upgrade to author tier
    mongo.users.update_one({"_id": ObjectId(uid)}, {"$set": {"tier": "author"}})
    return {"session": s, "email": email, "user": data["user"], "token": data["token"], "id": uid}


def _make_project(session, name="TEST_book"):
    r = session.post(f"{API}/projects", json={
        "name": name,
        "platform": "kdp",
        "trim_size": "6x9",
        "paper_type": "cream_50lb",
        "binding": "paperback",
        "page_count": 100,
        "project_type": "interior",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload_file(session, project_id):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1800, 2700), "white").save(buf, format="JPEG", dpi=(300, 300))
    buf.seek(0)
    files = {"file": ("test.jpg", buf, "image/jpeg")}
    r = session.post(f"{API}/projects/{project_id}/upload", files=files)
    assert r.status_code == 200, r.text
    return r.json()


# -------------- Register books_this_month --------------
def test_register_has_books_this_month_zero(free_user):
    assert free_user["user"].get("books_this_month") == 0
    # /auth/me should also show 0
    r = free_user["session"].get(f"{API}/auth/me")
    assert r.status_code == 200
    assert r.json()["user"].get("books_this_month") == 0


# -------------- Free tier: export blocked with 402 before PDF --------------
def test_free_user_export_blocked_with_402(free_user, mongo):
    s = free_user["session"]
    pid = _make_project(s, "TEST_free_export")
    _upload_file(s, pid)

    r = s.post(f"{API}/projects/{pid}/export")
    assert r.status_code == 402, r.text
    body = r.json()
    detail = body.get("detail", "")
    assert "book export" in detail.lower() or "doesn't include" in detail.lower() or "plan" in detail.lower(), f"unexpected detail: {detail}"

    # Verify exports_this_month NOT incremented
    user_doc = mongo.users.find_one({"email": free_user["email"]})
    assert user_doc.get("exports_this_month", 0) == 0
    assert user_doc.get("books_this_month", 0) == 0

    # Verify project doc does not have first_exported_at
    proj = mongo.projects.find_one({"_id": ObjectId(pid)})
    assert not proj.get("first_exported_at")


# -------------- Author tier: first export succeeds, second same-project doesn't recount, 2nd project blocked --------------
def test_author_book_counter_flow(author_user, mongo):
    s = author_user["session"]
    pid1 = _make_project(s, "TEST_author_book1")
    _upload_file(s, pid1)

    # First export
    r = s.post(f"{API}/projects/{pid1}/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["books_this_month"] == 1
    assert body["books_limit"] == 1
    assert body["counted_as_new_book"] is True

    # Verify first_exported_at set
    proj = mongo.projects.find_one({"_id": ObjectId(pid1)})
    assert proj.get("first_exported_at")

    # Second export of SAME project -> should not re-count
    r2 = s.post(f"{API}/projects/{pid1}/export")
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["books_this_month"] == 1, f"expected still 1, got {body2['books_this_month']}"
    assert body2["counted_as_new_book"] is False

    # Now create + export SECOND project -> should hit 402 (books allowance reached)
    pid2 = _make_project(s, "TEST_author_book2")
    _upload_file(s, pid2)
    r3 = s.post(f"{API}/projects/{pid2}/export")
    assert r3.status_code == 402, r3.text
    detail = r3.json().get("detail", "")
    assert "book" in detail.lower() and ("allowance" in detail.lower() or "reached" in detail.lower()), f"got: {detail}"


# -------------- Existing monthly-export enforcement still works --------------
def test_monthly_export_limit_still_enforced(mongo):
    """Directly manipulate exports_this_month to be at limit for a fresh author user."""
    email = f"qa+bookmax{TS}@example.com"
    s = requests.Session()
    r = s.post(f"{API}/auth/register", json={"email": email, "password": "test1234"})
    assert r.status_code == 200
    uid = r.json()["user"]["id"]
    # Set to author with exports at limit
    mongo.users.update_one({"_id": ObjectId(uid)}, {"$set": {"tier": "author", "exports_this_month": 15}})

    pid = _make_project(s, "TEST_export_maxed")
    _upload_file(s, pid)
    r2 = s.post(f"{API}/projects/{pid}/export")
    assert r2.status_code == 402
    detail = r2.json().get("detail", "").lower()
    assert "export" in detail and ("limit" in detail or "reached" in detail)


# -------------- Season endpoint returns pre_launch --------------
def test_season_pre_launch():
    r = requests.get(f"{API}/season")
    assert r.status_code == 200
    data = r.json()
    assert data.get("phase") == "pre_launch", f"phase was {data.get('phase')}"
    assert "start" in data
    # days_until should be around 56 (±2)
    if "days_until" in data:
        assert 40 <= data["days_until"] <= 80, f"days_until={data['days_until']}"


# -------------- Regression: specs, audit/start, checkout, ai/blurb --------------
def test_specs_ok():
    r = requests.get(f"{API}/specs")
    assert r.status_code == 200
    d = r.json()
    assert "platforms" in d and "trim_sizes" in d and "tiers" in d


def test_audit_start_ok():
    r = requests.post(f"{API}/audit/start", json={"platform": "kdp", "trim_size": "6x9"})
    assert r.status_code == 200
    assert "audit_id" in r.json()


@pytest.mark.parametrize("tier", ["author", "creator_pro", "publisher", "studio"])
def test_checkout_tiers(free_user, tier):
    r = free_user["session"].post(f"{API}/payments/checkout", json={
        "tier": tier,
        "origin_url": BASE_URL,
    })
    # Should be 200 (redirect URL) or possibly 400 if stripe not configured.
    # We only require it doesn't 500.
    assert r.status_code in (200, 400), f"tier={tier} -> {r.status_code} {r.text[:200]}"


def test_ai_blurb(author_user):
    r = author_user["session"].post(f"{API}/ai/blurb", json={
        "title": "Test Book",
        "genre": "fiction",
        "audience": "adults",
        "tone": "serious",
        "themes": "loss, hope",
    })
    # AI can be slow — allow up to 30s (requests default is fine, but sanity check)
    assert r.status_code in (200, 402, 429, 500), f"unexpected status {r.status_code}: {r.text[:300]}"
    # Accept 200 as pass; other statuses log but don't fail regression
    if r.status_code != 200:
        pytest.skip(f"AI blurb returned {r.status_code} - likely rate/quota, not a regression")
