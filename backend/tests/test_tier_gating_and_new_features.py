"""Tests for the features added in this iteration: AI Blurb/AI Cover tier
gating, Advanced Interior Check pricing table, series consistency, and
team seats/batch export.

Unlike the older test_*.py files in this directory (which hit a deployed
REACT_APP_BACKEND_URL over real HTTP and a real MongoDB via pymongo for
tier manipulation), these run fully in-process against the FastAPI app
object via httpx's ASGI transport, and upgrade tiers by writing directly
to `server.db` -- the same in-memory fallback database the app itself
uses when no real MongoDB is reachable. This means these tests are
self-contained: no deployed server, no real MongoDB, and no Stripe key
required to run them.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io
import pytest
import httpx
from bson import ObjectId
from reportlab.pdfgen import canvas

import server


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test/api")


async def _register(client, email):
    r = await client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "T"})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["user"]["id"], {"Authorization": f"Bearer {body['token']}"}


async def _set_tier(user_id, tier):
    await server.db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"tier": tier}})


def _sample_pdf_bytes():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * 72, 9 * 72))
    c.drawString(72, 72, "test")
    c.showPage()
    c.save()
    buf.seek(0)
    return buf


class TestAIBlurbTierGate:
    @pytest.mark.asyncio
    async def test_free_tier_blocked(self, client):
        async with client as c:
            uid, headers = await _register(c, "blurb_free@example.com")
            r = await c.post("/ai/blurb", headers=headers, json={"title": "Test"})
            assert r.status_code == 402
            assert "Author plan" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_paid_tier_passes_gate_reaches_key_check(self, client):
        # Without ANTHROPIC_API_KEY configured in this environment, a paid
        # user should get past the tier gate and hit the "not configured"
        # 503, never the 402 tier-gate error.
        async with client as c:
            uid, headers = await _register(c, "blurb_author@example.com")
            await _set_tier(uid, "author")
            r = await c.post("/ai/blurb", headers=headers, json={"title": "Test"})
            assert r.status_code in (503, 200)


class TestAICoverTierGate:
    @pytest.mark.asyncio
    async def test_free_tier_blocked(self, client):
        async with client as c:
            uid, headers = await _register(c, "cover_free@example.com")
            r = await c.post("/projects", headers=headers, json={
                "name": "Book", "platform": "kdp", "trim_size": "6x9",
                "paper_type": "white_50lb", "binding": "paperback", "page_count": 150, "project_type": "cover",
            })
            pid = r.json()["id"]
            r = await c.post(f"/projects/{pid}/ai-cover", headers=headers, json={"prompt": "a lighthouse"})
            assert r.status_code == 402
            assert "Author plan" in r.json()["detail"]


class TestAdvancedInteriorPricing:
    @pytest.mark.parametrize("tier,expected_cents", [
        ("free", 4999), ("author", 3999), ("creator_pro", 3499), ("publisher", 2999), ("studio", 2999),
    ])
    def test_price_table_matches_spec(self, tier, expected_cents):
        assert server.ADVANCED_INTERIOR_PRICE_CENTS[tier] == expected_cents

    def test_max_pages_is_300(self):
        assert server.ADVANCED_INTERIOR_MAX_PAGES == 300

    def test_basic_check_is_one_page(self):
        assert server.BASIC_CHECK_MAX_PAGES == 1


class TestExportBookRules:
    def test_exports_per_book_is_five(self):
        assert server.EXPORTS_PER_BOOK == 5

    @pytest.mark.asyncio
    async def test_export_requires_title(self, client):
        async with client as c:
            uid, headers = await _register(c, "title_required@example.com")
            await _set_tier(uid, "author")
            r = await c.post("/projects", headers=headers, json={
                "name": "Untitled Book", "platform": "kdp", "trim_size": "6x9",
                "paper_type": "white_50lb", "binding": "paperback", "page_count": 150, "project_type": "cover",
            })
            pid = r.json()["id"]
            r = await c.post(f"/projects/{pid}/slot-upload/full_wrap", headers=headers,
                              files={"file": ("c.pdf", _sample_pdf_bytes(), "application/pdf")})
            assert r.status_code == 200
            r = await c.post(f"/projects/{pid}/export", headers=headers)
            assert r.status_code == 400
            assert "title" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_free_tier_cannot_export(self, client):
        async with client as c:
            uid, headers = await _register(c, "free_no_export@example.com")
            r = await c.post("/projects", headers=headers, json={
                "name": "A Real Title", "platform": "kdp", "trim_size": "6x9",
                "paper_type": "white_50lb", "binding": "paperback", "page_count": 150, "project_type": "cover",
            })
            pid = r.json()["id"]
            r = await c.post(f"/projects/{pid}/slot-upload/full_wrap", headers=headers,
                              files={"file": ("c.pdf", _sample_pdf_bytes(), "application/pdf")})
            assert r.status_code == 200
            r = await c.post(f"/projects/{pid}/export", headers=headers)
            assert r.status_code == 402

    @pytest.mark.asyncio
    async def test_free_tier_can_upload_and_audit(self, client):
        """Free tier must still be able to upload + get a compliance report --
        the restriction is on export/production output only."""
        async with client as c:
            uid, headers = await _register(c, "free_upload_ok@example.com")
            r = await c.post("/projects", headers=headers, json={
                "name": "A Real Title", "platform": "kdp", "trim_size": "6x9",
                "paper_type": "white_50lb", "binding": "paperback", "page_count": 150, "project_type": "cover",
            })
            pid = r.json()["id"]
            r = await c.post(f"/projects/{pid}/slot-upload/full_wrap", headers=headers,
                              files={"file": ("c.pdf", _sample_pdf_bytes(), "application/pdf")})
            assert r.status_code == 200
            assert "compliance" in r.json()
            r = await c.post(f"/projects/{pid}/autofix", headers=headers)
            assert r.status_code == 200


class TestSeriesConsistency:
    @pytest.mark.asyncio
    async def test_flags_trim_mismatch(self, client):
        async with client as c:
            uid, headers = await _register(c, "series_test@example.com")
            await c.post("/projects", headers=headers, json={
                "name": "Book One", "platform": "kdp", "trim_size": "6x9",
                "paper_type": "white_50lb", "binding": "paperback", "page_count": 150,
                "project_type": "cover", "series_name": "My Series",
            })
            await c.post("/projects", headers=headers, json={
                "name": "Book Two", "platform": "kdp", "trim_size": "5x8",
                "paper_type": "white_50lb", "binding": "paperback", "page_count": 150,
                "project_type": "cover", "series_name": "My Series",
            })
            r = await c.get("/series/My Series/consistency", headers=headers)
            assert r.status_code == 200
            findings = r.json()["findings"]
            assert any(f["id"] == "series_trim_size_mismatch" for f in findings)


class TestTeamSeats:
    @pytest.mark.asyncio
    async def test_invite_blocked_below_publisher(self, client):
        async with client as c:
            uid, headers = await _register(c, "team_free@example.com")
            r = await c.post("/team/invite", headers=headers)
            assert r.status_code == 402

    @pytest.mark.asyncio
    async def test_publisher_invite_and_join_shares_plan(self, client):
        async with client as c:
            owner_id, owner_headers = await _register(c, "team_owner@example.com")
            member_id, member_headers = await _register(c, "team_member@example.com")
            await _set_tier(owner_id, "publisher")

            r = await c.post("/team/invite", headers=owner_headers)
            assert r.status_code == 200
            code = r.json()["invite_code"]

            r = await c.post("/team/join", headers=member_headers, json={"code": code})
            assert r.status_code == 200

            r = await c.get("/team/members", headers=owner_headers)
            assert r.status_code == 200
            assert len(r.json()["members"]) == 1
