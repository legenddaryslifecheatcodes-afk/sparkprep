"""Interior Studio AI Writing Help: suggestions must point at real text, the tool is gated like the other AI
tools, and a daily allowance caps the model bill. The model provider is faked; nothing leaves the machine."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="sp_wh_"), USE_MEMORY_DB="1",
                  JWT_SECRET=os.environ.get("JWT_SECRET") or "wh-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import writing_help as wh  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FREE = "writing-free@example.com"
PAID = "writing-paid@example.com"
PARAS = [
    {"id": 0, "text": "Their was a storm comming."},
    {"id": 1, "text": "She said, “its fine.”"},
    {"id": 2, "text": "The end."},
]


# ---- pure helpers ----

def test_clean_paragraphs_drops_bad_rows_and_caps_size():
    rows = [{"id": 0, "text": "ok"}, {"id": 0, "text": "dup"}, {"id": "x", "text": "bad id"}, {"id": 1, "text": "   "},
            {"id": True, "text": "bool id"}, {"id": 2, "text": "a" * 9999}, {"id": 3, "text": "b" * 4000},
            {"id": 4, "text": "c" * 2000}]
    out = wh.clean_paragraphs(rows)
    assert [p["id"] for p in out] == [0, 2, 3]         # one long paragraph is cut to 4,000; the next would pass 9,000
    assert len(out[1]["text"]) == wh.MAX_PARAGRAPH_CHARS
    assert wh.chars(out) <= wh.MAX_CHARS


def test_suggestions_must_match_real_text():
    raw = {"suggestions": [
        {"p": 0, "find": "Their was", "replace": "There was", "why": "Spelling", "kind": "word_choice"},
        {"p": 0, "find": "comming", "replace": "coming", "why": "Spelling", "kind": "spelling"},
        {"p": 0, "find": "not in the text", "replace": "x", "kind": "spelling"},     # invented: dropped
        {"p": 9, "find": "The", "replace": "A", "kind": "spelling"},                   # unknown paragraph: dropped
        {"p": 2, "find": "The end.", "replace": "The end.", "kind": "spelling"},       # no change: dropped
        {"p": 0, "find": "storm comming", "replace": "storm coming", "kind": "spelling"},  # overlaps: dropped
        {"p": "2", "find": "[2] The end", "replace": "The End", "kind": "nonsense"},   # number cleaned, kind defaulted
    ]}
    out = wh.clean_suggestions(raw, PARAS)
    assert [(s["p"], s["find"], s["replace"]) for s in out] == [
        (0, "Their was", "There was"), (0, "comming", "coming"), (2, "The end", "The End")]
    assert out[1]["at"] == PARAS[0]["text"].index("comming")
    assert out[2]["kind"] == "word_choice"


def test_straight_quotes_from_the_model_still_find_curly_quotes():
    raw = {"suggestions": [{"p": 1, "find": "\"its fine", "replace": "“It's fine", "kind": "grammar"}]}
    out = wh.clean_suggestions(raw, PARAS)
    assert len(out) == 1
    assert out[0]["find"] == "“its fine"       # the paragraph's own characters come back


def test_garbage_answers_give_no_suggestions():
    for raw in (None, [], {"suggestions": "nope"}, {"suggestions": [1, "x", None]}):
        assert wh.clean_suggestions(raw, PARAS) == []


def test_prompts_name_the_job_and_number_paragraphs():
    sp = wh.build_prompt("spelling", PARAS, "Thriller", "Night Print")
    wd = wh.build_prompt("wording", PARAS)
    assert "[1] She said" in sp and "Night Print" in sp and "Thriller" in sp
    assert "proofreader" in sp and "Do NOT change the author's style" in sp
    assert "line editor" in wd and "Keep the author's voice" in wd


# ---- endpoint ----

@pytest.fixture(scope="module")
def client():
    for email, tier in ((FREE, "free"), (PAID, "studio")):
        if not asyncio.run(server.db.users.find_one({"email": email})):
            asyncio.run(server.db.users.insert_one({
                "email": email, "password_hash": server.hash_password("TestPass123!"), "name": "W", "tier": tier,
                "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        yield c


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "tiers")
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "test-key")


def login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "TestPass123!"})
    return {"Authorization": "Bearer " + r.json()["token"]}


def reset_allowance():
    asyncio.run(server.db.users.update_one({"email": PAID}, {"$set": {"ai_writing_day": None, "ai_writing_chars": 0}}))


def test_needs_login(client):
    assert client.post("/api/ai/writing-help", json={"mode": "spelling", "paragraphs": PARAS}).status_code == 401


def test_included_with_a_book_not_free(client):
    r = client.post("/api/ai/writing-help", json={"mode": "spelling", "paragraphs": PARAS}, headers=login(client, FREE))
    assert r.status_code == 402


def test_returns_checked_suggestions(client, monkeypatch):
    reset_allowance()
    seen = {}

    async def fake(prompt, max_tokens=1200, who=""):
        seen["prompt"] = prompt
        return {"suggestions": [{"p": 0, "find": "comming", "replace": "coming", "why": "Spelling", "kind": "spelling"},
                                {"p": 0, "find": "made up", "replace": "x", "kind": "spelling"}]}
    monkeypatch.setattr(server, "_claude_json", fake)
    r = client.post("/api/ai/writing-help", json={"mode": "spelling", "paragraphs": PARAS, "title": "Night Print"},
                    headers=login(client, PAID))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["checked"] == [0, 1, 2]
    assert [s["replace"] for s in d["suggestions"]] == ["coming"]
    assert d["allowance_left"] == wh.DAILY_CHARS - wh.chars(PARAS)
    assert "Night Print" in seen["prompt"]


def test_rejects_unknown_mode_and_empty_writing(client):
    h = login(client, PAID)
    assert client.post("/api/ai/writing-help", json={"mode": "rewrite", "paragraphs": PARAS}, headers=h).status_code == 400
    assert client.post("/api/ai/writing-help", json={"mode": "wording", "paragraphs": [{"id": 0, "text": "  "}]},
                       headers=h).status_code == 400


def test_daily_allowance_stops_runaway_use(client, monkeypatch):
    reset_allowance()

    async def fake(prompt, max_tokens=1200, who=""):
        return {"suggestions": []}
    monkeypatch.setattr(server, "_claude_json", fake)
    monkeypatch.setattr(wh, "DAILY_CHARS", 60)
    h = login(client, PAID)
    body = {"mode": "spelling", "paragraphs": PARAS}          # 55 characters
    assert client.post("/api/ai/writing-help", json=body, headers=h).status_code == 200
    r = client.post("/api/ai/writing-help", json=body, headers=h)
    assert r.status_code == 429
    assert "resets" in r.json()["detail"]
