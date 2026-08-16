"""Iteration 7 tests: new /api/season, new TIERS, slot uploads, adjustments, regressions."""
import os, time, io, pytest, requests
from PIL import Image

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://sparkprep-print.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"
TS = int(time.time())
EMAIL = f"qa+it7_{TS}@example.com"
PW = "test1234"


@pytest.fixture(scope="session")
def auth():
    r = requests.post(f"{API}/auth/register", json={"email": EMAIL, "password": PW, "name": "QA7"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    return {"token": tok, "headers": {"Authorization": f"Bearer {tok}"}}


@pytest.fixture(scope="session")
def project(auth):
    r = requests.post(f"{API}/projects", json={
        "name": "TEST_it7_project", "platform": "kdp", "trim_size": "6x9",
        "paper_type": "white_50lb", "binding": "paperback", "page_count": 200,
        "project_type": "cover",
    }, headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="session")
def small_png(tmp_path_factory):
    p = tmp_path_factory.mktemp("png") / "small.png"
    img = Image.new("RGB", (1800, 2700), "white")
    img.save(str(p), "PNG", dpi=(300, 300))
    return str(p)


# ---------- Season endpoint ----------
class TestSeason:
    def test_season_shape(self):
        r = requests.get(f"{API}/season")
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("phase", "start", "end", "days_until", "days_remaining"):
            assert k in d, f"missing {k}"
        assert d["phase"] in ("pre_launch", "active", "closed")
        # 99-day range
        assert isinstance(d["days_until"], int)
        assert isinstance(d["days_remaining"], int)


# ---------- TIERS via /api/specs ----------
class TestSpecs:
    def test_tiers_present(self):
        r = requests.get(f"{API}/specs")
        assert r.status_code == 200
        tiers = r.json()["tiers"]
        expected_order = ["free", "author", "creator_pro", "publisher", "studio"]
        keys = list(tiers.keys())
        assert keys == expected_order, f"tier order wrong: {keys}"
        expected_prices = {"free": 0, "author": 1999, "creator_pro": 3999, "publisher": 9900, "studio": 24900}
        for k, expected_price in expected_prices.items():
            t = tiers[k]
            for field in ("name", "books_per_month", "monthly_exports", "max_file_mb", "price_cents", "features"):
                assert field in t, f"tier {k} missing {field}"
            assert t["price_cents"] == expected_price, f"{k} price {t['price_cents']} != {expected_price}"


# ---------- Checkout with new tier keys ----------
class TestCheckout:
    @pytest.mark.parametrize("tier,amount", [
        ("author", 1999), ("creator_pro", 3999), ("publisher", 9900), ("studio", 24900),
    ])
    def test_new_tier_checkout(self, auth, tier, amount):
        r = requests.post(f"{API}/payments/checkout", json={"tier": tier, "origin_url": BASE_URL},
                          headers=auth["headers"])
        assert r.status_code == 200, f"{tier} -> {r.status_code} {r.text}"
        d = r.json()
        assert d["checkout_url"].startswith("https://checkout.stripe.com"), d["checkout_url"]
        assert "session_id" in d

    def test_old_pro_tier_rejected(self, auth):
        r = requests.post(f"{API}/payments/checkout", json={"tier": "pro", "origin_url": BASE_URL},
                          headers=auth["headers"])
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"


# ---------- Slot upload ----------
class TestSlotUpload:
    @pytest.mark.parametrize("slot", ["full_wrap", "front_cover", "back_cover", "spine", "interior"])
    def test_slot_upload(self, auth, project, small_png, slot):
        with open(small_png, "rb") as f:
            r = requests.post(
                f"{API}/projects/{project['id']}/slot-upload/{slot}",
                files={"file": (f"{slot}.png", f, "image/png")},
                headers=auth["headers"],
            )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["slot"] == slot
        assert "file_metadata" in d and "compliance" in d
        # verify persistence
        g = requests.get(f"{API}/projects/{project['id']}", headers=auth["headers"])
        assert g.status_code == 200
        slots = g.json().get("slots") or {}
        assert slot in slots, f"slot {slot} not stored"

    def test_slot_unknown(self, auth, project, small_png):
        with open(small_png, "rb") as f:
            r = requests.post(
                f"{API}/projects/{project['id']}/slot-upload/bogus",
                files={"file": ("x.png", f, "image/png")},
                headers=auth["headers"],
            )
        assert r.status_code == 400

    def test_slot_preview_with_token(self, auth, project):
        # full_wrap uploaded above
        url = f"{API}/projects/{project['id']}/slot/full_wrap/preview?token={auth['token']}"
        r = requests.get(url)
        assert r.status_code == 200, r.text
        assert len(r.content) > 100

    def test_slot_delete(self, auth, project, small_png):
        # upload spine again to ensure exists
        with open(small_png, "rb") as f:
            requests.post(f"{API}/projects/{project['id']}/slot-upload/spine",
                          files={"file": ("s.png", f, "image/png")}, headers=auth["headers"])
        r = requests.delete(f"{API}/projects/{project['id']}/slot/spine", headers=auth["headers"])
        assert r.status_code == 200, r.text
        g = requests.get(f"{API}/projects/{project['id']}", headers=auth["headers"])
        slots = g.json().get("slots") or {}
        assert "spine" not in slots


# ---------- Adjustments ----------
class TestAdjustments:
    def test_patch_adjustments(self, auth, project):
        r = requests.patch(f"{API}/projects/{project['id']}/adjustments",
                           json={"spine_offset": 0.05, "target_dpi": 400},
                           headers=auth["headers"])
        assert r.status_code == 200, r.text
        adj = r.json()["adjustments"]
        assert adj.get("spine_offset") == 0.05
        assert adj.get("target_dpi") == 400


# ---------- Regressions ----------
class TestRegressions:
    def test_projects_list(self, auth):
        r = requests.get(f"{API}/projects", headers=auth["headers"])
        assert r.status_code == 200

    def test_legacy_upload(self, auth, project, small_png):
        with open(small_png, "rb") as f:
            r = requests.post(f"{API}/projects/{project['id']}/upload",
                              files={"file": ("cover.png", f, "image/png")},
                              headers=auth["headers"])
        assert r.status_code == 200, r.text

    def test_autofix(self, auth, project):
        r = requests.post(f"{API}/projects/{project['id']}/autofix", headers=auth["headers"])
        assert r.status_code == 200, r.text

    def test_export(self, auth, project):
        r = requests.post(f"{API}/projects/{project['id']}/export", headers=auth["headers"])
        assert r.status_code == 200, r.text

    def test_blurb(self, auth):
        r = requests.post(f"{API}/ai/blurb", json={"title": "Test Regression"},
                         headers=auth["headers"], timeout=90)
        assert r.status_code == 200, r.text

    def test_audit_start_and_upload(self, small_png):
        r = requests.post(f"{API}/audit/start", json={"platform": "kdp", "trim_size": "6x9"})
        assert r.status_code == 200, r.text
        aid = r.json()["audit_id"]
        with open(small_png, "rb") as f:
            u = requests.post(f"{API}/audit/{aid}/upload",
                              files={"file": ("a.png", f, "image/png")})
        assert u.status_code == 200, u.text
