"""Cover Studio AI: the cover planner and the full-wrap art painter, plus the spine calculator's guide numbers.
Runs in-process against the in-memory database; the model providers are faked, nothing leaves the machine."""
import asyncio
import base64
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

os.environ.update(DATA_DIR=os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="sp_cs_"), USE_MEMORY_DB="1",
                  JWT_SECRET=os.environ.get("JWT_SECRET") or "cs-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FREE = "studio-free@example.com"
PAID = "studio-paid@example.com"
LAYOUTS = ["classic", "block", "glow"]
STYLES = ["noir", "gold"]


@pytest.fixture(scope="module")
def client():
    for email, tier in ((FREE, "free"), (PAID, "studio")):
        if not asyncio.run(server.db.users.find_one({"email": email})):
            asyncio.run(server.db.users.insert_one({
                "email": email, "password_hash": server.hash_password("TestPass123!"), "name": "S", "tier": tier,
                "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        yield c


@pytest.fixture(autouse=True)
def legacy_tiers(monkeypatch):
    # Plain tier gate (the book model maps "holds a book" onto the same tier field).
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "tiers")
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")


def login(client, email):
    r = client.post("/api/auth/login", json={"email": email, "password": "TestPass123!"})
    return {"Authorization": "Bearer " + r.json()["token"]}


def plan_body(**kw):
    return {"title": "The Long Print", "author": "A. Writer", "genre": "Thriller", "layouts": LAYOUTS, "styles": STYLES, **kw}


def test_plan_needs_login(client):
    assert client.post("/api/ai/cover-plan", json=plan_body()).status_code == 401


def _free_id():
    return str(asyncio.run(server.db.users.find_one({"email": FREE}))["_id"])


def _set_cover_credits_used(welcome_used, month_used):
    import ai_credits
    uid = _free_id()
    server.db.ai_credits._docs[:] = [d for d in server.db.ai_credits._docs if d.get("user_id") != uid]
    asyncio.run(server.db.ai_credits.insert_one({"user_id": uid, "pool": "cover", "period": ai_credits.period(),
                                                 "welcome_used": welcome_used, "month_used": month_used}))


def test_free_account_spends_cover_credits_then_is_asked_to_join(client, monkeypatch):
    """Owner's plan (2026-11-27): free accounts get 50 cover credits to start plus 10 a month; a plan costs 5."""
    async def fake(prompt, max_tokens=1200):
        return {"layout": "classic", "style": "gold", "subtitle": "", "tagline": "", "blurb": "", "art_prompt": ""}
    monkeypatch.setattr(server, "_claude_json", fake)
    _set_cover_credits_used(0, 0)
    hdr = login(client, FREE)
    assert client.get("/api/ai/credits", headers=hdr).json()["pools"]["cover"]["left"] == 60
    r = client.post("/api/ai/cover-plan", json=plan_body(), headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["credits_left"] == 55
    _set_cover_credits_used(50, 8)                       # 2 credits left, a plan needs 5
    r = client.post("/api/ai/cover-plan", json=plan_body(), headers=hdr)
    assert r.status_code == 402
    d = r.json()["detail"]
    assert d["code"] == "out_of_credits" and "$9.99/month membership" in d["msg"] and "500 credits" in d["msg"]


def test_credit_balance_signed_out_offers_free_credits(client):
    d = client.get("/api/ai/credits").json()
    assert d["signed_in"] is False and d["offer"]["free_welcome"] == 50 and d["offer"]["member_monthly"] == 500


def test_plan_returns_only_layouts_and_styles_that_exist(client, monkeypatch):
    seen = {}

    async def fake(prompt, max_tokens=1200):
        seen["prompt"] = prompt
        return {"layout": "not-a-layout", "style": "gold", "subtitle": "", "tagline": "Every page hides a lie.",
                "blurb": "A printer. A deadline. A secret.", "art_prompt": "A rain-soaked printing press at night"}
    monkeypatch.setattr(server, "_claude_json", fake)
    r = client.post("/api/ai/cover-plan", json=plan_body(), headers=login(client, PAID))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["layout"] == "classic"          # the model's unknown answer falls back to a real layout
    assert d["style"] == "gold"
    assert d["tagline"] == "Every page hides a lie."
    assert "The Long Print" in seen["prompt"] and "glow" in seen["prompt"]


def test_plan_rejects_junk_keys(client):
    r = client.post("/api/ai/cover-plan", json=plan_body(layouts=["<script>"], styles=["x y"]), headers=login(client, PAID))
    assert r.status_code == 400


def test_plan_without_key_says_not_configured(client, monkeypatch):
    monkeypatch.setattr(server, "ANTHROPIC_API_KEY", "")
    r = client.post("/api/ai/cover-plan", json=plan_body(), headers=login(client, PAID))
    assert r.status_code == 503


def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (20, 40, 60)).save(buf, "PNG")
    return buf.getvalue()


def test_art_is_wide_for_a_full_wrap_and_has_no_lettering(client, monkeypatch):
    calls = {}

    async def fake(prompt, api_key, aspect_ratio="3:4"):
        calls.update(prompt=prompt, aspect=aspect_ratio)
        return _png(1536, 1024)
    monkeypatch.setattr(server, "generate_cover_image", fake)
    r = client.post("/api/ai/cover-art", json={"prompt": "a lighthouse in a storm", "width_in": 12.7, "height_in": 9.25},
                    headers=login(client, PAID))
    assert r.status_code == 200, r.text
    d = r.json()
    assert calls["aspect"] == "3:2"
    assert "Do not render any words" in calls["prompt"] and "front cover" in calls["prompt"]
    assert (d["width_px"], d["height_px"]) == (1536, 1024)
    assert Image.open(io.BytesIO(base64.b64decode(d["image_b64"]))).size == (1536, 1024)


def test_art_needs_25_cover_credits(client):
    _set_cover_credits_used(50, 0)                       # 10 left this month, artwork needs 25
    r = client.post("/api/ai/cover-art", json={"prompt": "x"}, headers=login(client, FREE))
    assert r.status_code == 402 and r.json()["detail"]["needed"] == 25


def test_spine_calc_gives_the_studio_its_guide_numbers(client):
    r = client.post("/api/specs/spine", json={"page_count": 200, "platform": "ingramspark", "binding": "paperback"})
    d = r.json()
    assert d["barcode_zone"] == {"w": 1.75, "h": 1.0}
    assert d["safe_margin"] == server.resolve_binding_spec("paperback", "ingramspark")["safe_margin"]
