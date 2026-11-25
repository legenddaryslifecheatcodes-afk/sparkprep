"""Legenddary SparkPrep Author Studio: members-only gate, book folders, version history, goals, checklist,
readiness, launch planner, ledger, the members-only Library, and the cancelled-membership re-check."""
import asyncio
import io
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_studio_"), USE_MEMORY_DB="1", JWT_SECRET="st-test-" + "x" * 32,
                  ADMIN_EMAIL="root-admin@example.com")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from author_studio import library as L  # noqa: E402
from author_studio import routes as R  # noqa: E402
from book_pass import entitlements as ent  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EMAIL = "studio-author@example.com"


class Obj(dict):
    __getattr__ = dict.get


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "A", "tier": "free",
        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        r = c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"})
        c.headers["Authorization"] = "Bearer " + r.json()["token"]
        yield c


def uid():
    return str(asyncio.run(server.db.users.find_one({"email": EMAIL}))["_id"])


@pytest.fixture
def member(monkeypatch):
    server.db.book_subscriptions._docs.clear()
    asyncio.run(ent.register_subscription(server.db, user_id=uid(), subscription_id="sub_member1", plan="member"))
    monkeypatch.setattr(server.stripe, "api_key", "sk_test_not_configured")   # no Stripe calls
    yield
    server.db.book_subscriptions._docs.clear()


def test_non_members_are_kept_out_but_see_their_status(client):
    server.db.book_subscriptions._docs.clear()
    s = client.get("/api/studio/status").json()
    assert s["member"] is False
    r = client.post("/api/studio/books", json={"title": "Nope"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "members_only"
    assert client.get("/api/studio/library").status_code == 403


def test_book_folder_goals_checklist_and_readiness(client, member):
    b = client.post("/api/studio/books", json={"title": "The Road to Success", "target_words": 1000}).json()
    assert b["writing_progress"] == 0 and b["publishing_progress"] == 0
    b = client.post(f"/api/studio/books/{b['id']}/words", json={"total": 700}).json()
    assert b["writing_progress"] == 70 and b["words"]["today"] == 700
    b = client.post(f"/api/studio/books/{b['id']}/words", json={"added": 300}).json()
    assert b["writing_progress"] == 100 and b["words"]["today"] == 1000 and b["words"]["streak_days"] == 1
    steps = {s["key"]: s["state"] for s in b["readiness"]}
    assert steps["manuscript"] == "done" and steps["editing"] == "not_started"
    b = client.put(f"/api/studio/books/{b['id']}/checklist", json={"key": "self_edit", "done": True}).json()
    assert {s["key"]: s["state"] for s in b["readiness"]}["editing"] == "in_progress"
    assert b["next_step"]["key"] == "editing"
    assert 0 < b["publishing_progress"] < 100
    assert client.put(f"/api/studio/books/{b['id']}/checklist", json={"key": "nope", "done": True}).status_code == 400


def test_linked_sparkprep_book_fills_in_the_final_check(client, member):
    pid = asyncio.run(server.db.projects.insert_one({"user_id": uid(), "name": "Linked", "slots": {"interior": {"x": 1}},
                                                     "last_final_review": {"status": "green"}})).inserted_id
    b = client.post("/api/studio/books", json={"title": "Linked book", "project_id": str(pid)}).json()
    steps = {s["key"]: s for s in b["readiness"]}
    assert steps["inspection"]["state"] == "done" and steps["interior"]["state"] == "in_progress"
    assert client.post("/api/studio/books", json={"title": "x", "project_id": "0" * 24}).status_code == 404


def test_files_keep_every_version_and_restore_never_deletes(client, member):
    b = client.post("/api/studio/books", json={"title": "Versions"}).json()
    up = lambda data, **form: client.post(f"/api/studio/books/{b['id']}/files", data={"folder": "manuscript", **form},
                                          files={"file": ("draft.docx", io.BytesIO(data))})
    v1 = up(b"draft one").json()
    v2 = up(b"draft two!").json()
    assert v1["version"] == 1 and v2["version"] == 2 and v1["group_id"] == v2["group_id"]
    assert client.get(f"/api/studio/files/{v1['id']}/download").content == b"draft one"
    v3 = client.post(f"/api/studio/files/{v1['id']}/restore").json()
    assert v3["version"] == 3 and v3["restored_from"] == 1
    assert client.get(f"/api/studio/files/{v3['id']}/download").content == b"draft one"
    # deleting the original version keeps the restored copy readable
    client.delete(f"/api/studio/files/{v1['id']}")
    assert client.get(f"/api/studio/files/{v3['id']}/download").content == b"draft one"
    files = client.get(f"/api/studio/books/{b['id']}").json()["files"]
    assert len(files) == 1 and [v["version"] for v in files[0]["versions"]] == [3, 2]
    assert up(b"x", folder="nope").status_code == 400
    bad = client.post(f"/api/studio/books/{b['id']}/files", data={"folder": "research"},
                      files={"file": ("run.exe", io.BytesIO(b"x"))})
    assert bad.status_code == 400
    used = client.get("/api/studio/status").json()["storage"]["used_bytes"]
    assert used >= len(b"draft one") + len(b"draft two!")


def test_former_members_can_still_download_but_not_change(client, member):
    b = client.post("/api/studio/books", json={"title": "Kept"}).json()
    f = client.post(f"/api/studio/books/{b['id']}/files", data={"folder": "final"},
                    files={"file": ("final.pdf", io.BytesIO(b"%PDF final"))}).json()
    server.db.book_subscriptions._docs.clear()
    assert client.get(f"/api/studio/files/{f['id']}/download").content == b"%PDF final"
    assert client.get(f"/api/studio/books/{b['id']}").status_code == 200
    assert client.patch(f"/api/studio/books/{b['id']}", json={"title": "Changed"}).status_code == 403


def test_launch_plan_notes_and_ledger(client, member):
    b = client.post("/api/studio/books", json={"title": "Launch"}).json()
    b = client.post(f"/api/studio/books/{b['id']}/launch/plan", json={"launch_date": "2027-03-01"}).json()
    assert b["launch_date"] == "2027-03-01" and len(b["launch_tasks"]) > 10
    assert any(t["due"] == "2027-03-01" for t in b["launch_tasks"])
    again = client.post(f"/api/studio/books/{b['id']}/launch/plan", json={"launch_date": "2027-03-01"}).json()
    assert len(again["launch_tasks"]) == len(b["launch_tasks"])          # no duplicates
    t = b["launch_tasks"][0]
    b = client.patch(f"/api/studio/books/{b['id']}/launch/tasks/{t['id']}", json={"done": True}).json()
    assert next(x for x in b["launch_tasks"] if x["id"] == t["id"])["done"] is True
    n = client.post(f"/api/studio/books/{b['id']}/notes", json={"title": "Idea", "body": "A twist"}).json()
    client.patch(f"/api/studio/books/{b['id']}/notes/{n['id']}", json={"body": "A bigger twist"})
    assert client.get(f"/api/studio/books/{b['id']}").json()["notes"][0]["body"] == "A bigger twist"
    client.post(f"/api/studio/books/{b['id']}/ledger", json={"kind": "expense", "label": "Editor", "amount_cents": 30000})
    b = client.post(f"/api/studio/books/{b['id']}/ledger", json={"kind": "sale", "label": "Nov", "amount_cents": 4500, "copies": 9}).json()
    assert b["totals"] == {"expense": 30000, "marketing": 0, "sale": 4500} and b["copies_sold"] == 9


def test_other_peoples_books_are_off_limits(client, member):
    other = asyncio.run(server.db.studio_books.insert_one({"user_id": "someone-else", "title": "Theirs"})).inserted_id
    assert client.get(f"/api/studio/books/{other}").status_code == 404
    assert client.delete(f"/api/studio/books/{other}").status_code == 404


def test_library_hides_future_articles_and_is_members_only(client, member, tmp_path):
    for slug, day in (("old", "2026-01-01"), ("future", "2099-01-01")):
        (tmp_path / f"{slug}.json").write_text(json.dumps({"slug": slug, "title": slug, "publish_on": day,
                                                           "category": "marketing", "body": [{"p": "x"}]}))
    (tmp_path / "broken.json").write_text("{not json")
    slugs = [a["slug"] for a in L.published(date(2026, 11, 25), tmp_path)]
    assert slugs == ["old"]
    lib = client.get("/api/studio/library").json()
    assert lib["articles"], "the seeded library has published articles"
    first = lib["articles"][0]["slug"]
    art = client.get(f"/api/studio/library/{first}").json()
    assert art["body"]
    assert client.get("/api/studio/library/does-not-exist").status_code == 404


def test_every_seeded_article_is_well_formed():
    arts = L.load_all()
    files = list(L.LIBRARY_DIR.glob("*.json"))
    assert len(arts) == len(files) >= 6
    assert len({a["slug"] for a in arts}) == len(arts)
    for a in arts:
        assert a["category"] in L.CATEGORIES and a.get("summary") and len(a["body"]) >= 4
        text = json.dumps(a)
        assert "Spark Prep" not in text


def test_cancelled_membership_is_noticed_without_a_webhook(client, monkeypatch):
    server.db.book_subscriptions._docs.clear()
    asyncio.run(ent.register_subscription(server.db, user_id=uid(), subscription_id="sub_gone", plan="member"))
    monkeypatch.setattr(server.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(server.stripe.Subscription, "retrieve", lambda sid: Obj(id=sid, status="canceled"))
    assert client.get("/api/studio/status").json()["member"] is False
    assert asyncio.run(server.db.book_subscriptions.find_one({"subscription_id": "sub_gone"}))["status"] == "canceled"


def test_stripe_outage_never_locks_out_a_paying_member(client, monkeypatch):
    server.db.book_subscriptions._docs.clear()
    asyncio.run(ent.register_subscription(server.db, user_id=uid(), subscription_id="sub_ok", plan="member"))
    monkeypatch.setattr(server.stripe, "api_key", "sk_test_dummy")

    def boom(sid):
        raise RuntimeError("stripe down")
    monkeypatch.setattr(server.stripe.Subscription, "retrieve", boom)
    assert client.get("/api/studio/status").json()["member"] is True


def test_progress_maths():
    assert R.writing_progress({"chapters_total": 10, "chapters_done": 3}) == 30
    assert R.writing_progress({"manuscript_done": True}) == 0 + 100
    today = date(2026, 11, 25)
    stats = R.word_stats({"word_log": [{"day": "2026-11-24", "words": 500}, {"day": "2026-11-23", "words": 200}]}, today)
    assert stats["streak_days"] == 2 and stats["today"] == 0 and stats["this_week"] == 700
