"""A paperback cover uploaded as separate back / spine / front pieces is assembled into one wrap at export.
Pieces are solid colors so the exported file can be checked by where each color actually landed."""
import asyncio
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_cp_"), USE_MEMORY_DB="1", JWT_SECRET="cp-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

EMAIL = "pieces-author@example.com"
BLEED, TRIM_W, TRIM_H = 0.125, 6.0, 9.0
RED, GREEN, BLUE = (220, 20, 20), (20, 200, 20), (20, 20, 220)


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "P", "tier": "free",
        "beta_active": True, "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        c.headers["Authorization"] = "Bearer " + c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"}).json()["token"]
        yield c


@pytest.fixture(autouse=True)
def legacy_pricing(monkeypatch):
    monkeypatch.delenv("SPARKPREP_PRICING_MODEL", raising=False)


def jpeg(w_in, h_in, color):
    buf = io.BytesIO()
    Image.new("RGB", (max(1, round(w_in * 300)), round(h_in * 300)), color).save(buf, "JPEG", quality=95, dpi=(300, 300))
    return buf.getvalue()


def project(client, pages, binding="paperback"):
    return client.post("/api/projects", json={"name": "Pieces Book", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                              "binding": binding, "page_count": pages, "project_type": "cover"}).json()["id"]


def upload(client, pid, slot, data):
    r = client.post(f"/api/projects/{pid}/slot-upload/{slot}", files={"file": (f"{slot}.jpg", data, "image/jpeg")})
    assert r.status_code == 200, r.text
    return r.json()


def upload_pieces(client, pid, spine_w, skip=()):
    panel = (TRIM_W + 2 * BLEED, TRIM_H + 2 * BLEED)
    for slot, size, color in (("back_cover", panel, RED), ("spine", (spine_w, panel[1]), GREEN), ("front_cover", panel, BLUE)):
        if slot not in skip:
            size_check = next(c for c in upload(client, pid, slot, jpeg(*size, color))["compliance"] if c["id"] == "cover_size")
            assert size_check["status"] == "pass", size_check


def exported_cover(client, pid):
    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 200, r.text
    import pymupdf
    doc = pymupdf.open(server.EXPORT_DIR / r.json()["export_name"])
    page = doc[0]
    pix = page.get_pixmap(dpi=50)
    return r.json(), page.rect.width / 72, pix


def color_at(pix, x_in):
    x = min(pix.width - 1, int(x_in * 50))
    r, g, b = pix.pixel(x, pix.height // 2)[:3]
    return max((("red", r), ("green", g), ("blue", b)), key=lambda t: t[1])[0]


def test_three_pieces_export_as_one_correctly_assembled_wrap(client):
    spine_w = 100 / 444
    pid = project(client, 100)
    upload_pieces(client, pid, spine_w)
    body, width_in, pix = exported_cover(client, pid)
    assert width_in == pytest.approx(2 * TRIM_W + spine_w + 2 * BLEED, abs=0.01)
    spine_mid = BLEED + TRIM_W + spine_w / 2
    assert color_at(pix, 2.0) == "red"
    assert color_at(pix, spine_mid) == "green"
    assert color_at(pix, width_in - 2.0) == "blue"


def test_thin_spine_never_lets_one_panel_cover_the_other(client):
    spine_w = 24 / 444                                    # ~0.054", narrower than the 0.125" bleed
    pid = project(client, 24)
    upload_pieces(client, pid, spine_w)
    _, width_in, pix = exported_cover(client, pid)
    assert color_at(pix, BLEED + TRIM_W - 0.06) == "red"                      # back art right up to the spine
    assert color_at(pix, BLEED + TRIM_W + spine_w + 0.06) == "blue"            # front art right after it


def test_a_missing_piece_is_named(client):
    pid = project(client, 100)
    upload_pieces(client, pid, 100 / 444, skip=("spine",))
    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 404 and "spine" in r.json()["detail"]


def test_hardcover_pieces_ask_for_one_combined_file(client):
    pid = project(client, 100, binding="hardcover_case")
    upload(client, pid, "front_cover", jpeg(7, 10, BLUE))
    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 400 and "one combined file" in r.json()["detail"]


def test_a_wrong_size_piece_is_flagged_with_its_needed_size(client):
    pid = project(client, 100)
    check = next(c for c in upload(client, pid, "front_cover", jpeg(8.5, 11, BLUE))["compliance"] if c["id"] == "cover_size")
    assert check["status"] == "fail" and "front cover piece" in check["message"]
