"""Interior Studio: the author's writing typeset into a print interior that passes SparkPrep's own checks.

Margins follow each distributor's own published numbers for the page count, chapters open on right-hand pages,
skipped left pages stay blank, text is black ink only, fonts are embedded, and the typeset PDF becomes the book's
interior with its print file kept for the book (designing is free)."""
import asyncio
import io
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pytest

os.environ.update(DATA_DIR=os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="sp_is_"), USE_MEMORY_DB="1",
                  JWT_SECRET=os.environ.get("JWT_SECRET") or "is-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import interior_studio as S  # noqa: E402
from pdfx_validator import check_interior_safety_margins  # noqa: E402

TEXT = ("The rain had not stopped for three days, and Mara had begun to suspect it never would. She stood at the "
        "window of the old cottage and watched the grey sea throw itself against the rocks below. ") * 3


def book(chapters=4, paras=12, titles=None):
    content = []
    for c in range(chapters):
        content.append({"type": "heading", "attrs": {"level": 1},
                        "content": [{"type": "text", "text": (titles or [])[c] if titles else f"Part of the Story {c}"}]})
        for i in range(paras):
            content.append({"type": "paragraph", "content": [{"type": "text", "text": TEXT}]})
            if i == 5:
                content.append({"type": "horizontalRule"})
    return {"type": "doc", "content": content}


def compose(style="classic_novel", platform="kdp", doc=None, details=None, trim=(6, 9)):
    buf = io.BytesIO()
    meta = S.compose(buf, {"style": style, "setup": {"platform": platform, "trim_w": trim[0], "trim_h": trim[1]},
                           "details": details or {"title": "The Keeper", "author": "Jane Writer",
                                                  "dedication": "For Gran"},
                           "content": doc or book()}, 0.5)
    return meta, buf.getvalue()


def test_margins_are_each_distributors_own_numbers():
    kdp = [S.distributor_min_margins("kdp", n, 0)["inside"] for n in (24, 150, 151, 300, 301, 500, 501, 700, 701)]
    assert kdp == [0.375, 0.375, 0.5, 0.5, 0.625, 0.625, 0.75, 0.75, 0.875]
    assert S.distributor_min_margins("kdp", 100, 0)["outside"] == 0.25
    lulu = [S.distributor_min_margins("lulu", n, 0)["inside"] for n in (60, 61, 150, 151, 400, 401, 600, 601)]
    assert lulu == [0.5, 0.625, 0.625, 1.0, 1.0, 1.125, 1.125, 1.25]
    assert S.distributor_min_margins("barnes_noble", 300, 0) == {"top": 0.5, "bottom": 0.5, "outside": 0.5,
                                                                 "inside": 0.75}
    assert set(S.distributor_min_margins("ingramspark", 300, 0).values()) == {0.5}
    # never below the safe margin SparkPrep's own check uses
    assert min(S.distributor_min_margins("kdp", 100, 0.5).values()) == 0.5


@pytest.mark.parametrize("platform", ["kdp", "ingramspark", "lulu", "barnes_noble"])
@pytest.mark.parametrize("style", sorted(S.BOOK_STYLES))
def test_every_style_passes_the_margin_check(tmp_path, style, platform):
    meta, pdf = compose(style, platform)
    f = tmp_path / "b.pdf"
    f.write_bytes(pdf)
    assert check_interior_safety_margins(str(f), platform, 6, 9) == []
    assert meta["margins"]["inside"] >= meta["distributor_minimums"]["inside"]


def test_long_book_gets_the_wider_gutter_for_its_page_count():
    meta, _ = compose("classic_novel", "lulu", book(chapters=14, paras=40))
    assert meta["page_count"] > 150
    assert meta["margins"]["inside"] >= S.distributor_min_margins("lulu", meta["page_count"], 0.5)["inside"]


def test_pages_are_trim_size_with_embedded_fonts_and_black_ink_only():
    _, pdf = compose(trim=(5.5, 8.5))
    d = pymupdf.open(stream=pdf, filetype="pdf")
    assert all(round(p.rect.width / 72, 3) == 5.5 and round(p.rect.height / 72, 3) == 8.5 for p in d)
    fonts = {f[3] for i in range(d.page_count) for f in d.get_page_fonts(i)}
    assert fonts and all(not f.startswith(("Helvetica", "Times")) for f in fonts)
    assert all(d.extract_font(f[0])[3] for f in d.get_page_fonts(6))      # embedded (font file present)
    for page in d:
        stream = page.read_contents()
        assert b" rg" not in stream and b" RG" not in stream                # no screen (RGB) colour at all


def test_chapters_open_on_right_hand_pages_and_skipped_pages_stay_blank():
    titles = ["Arrival", "Chapter 7: The Storm", "Epilogue", "Departure"]
    meta, pdf = compose(doc=book(titles=titles, paras=9))
    d = pymupdf.open(stream=pdf, filetype="pdf")
    pages = {t: i + 1 for i in range(d.page_count) for t in titles if t.split(": ")[-1] in d[i].get_text()
             and "Contents" not in d[i].get_text()}
    assert all(n % 2 == 1 for n in pages.values()), pages
    flat = lambda n: d[n - 1].get_text().replace(" ", "").upper()      # chapter labels are letter-spaced
    assert "CHAPTERONE" in flat(pages["Arrival"])
    assert "CHAPTER7" in flat(pages["Chapter 7: The Storm"])
    blanks = [i + 1 for i in range(d.page_count) if not d[i].get_text().strip()]
    assert blanks and all(n % 2 == 0 for n in blanks)
    toc = next(d[i].get_text() for i in range(d.page_count) if "Contents" in d[i].get_text())
    assert f"{pages['Departure']}" in toc


def test_word_formatting_is_kept():
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "One"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Bold ", "marks": [{"type": "bold"}]},
                                          {"type": "text", "text": "and <italic> & more", "marks": [{"type": "italic"}]}]},
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "A quote."}]}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "First point"}]}]}]},
        {"type": "pageBreak"},
        {"type": "paragraph", "content": [{"type": "text", "text": "After the break."}]},
    ]}
    meta, pdf = compose("nonfiction", doc=doc)
    d = pymupdf.open(stream=pdf, filetype="pdf")
    text = "".join(p.get_text() for p in d)
    assert "and <italic> & more" in text and "First point" in text
    fonts = {f[3] for i in range(d.page_count) for f in d.get_page_fonts(i)}
    assert any("Bold" in f for f in fonts) and any("Italic" in f for f in fonts)
    last = [i for i in range(d.page_count) if "After the break." in d[i].get_text()][0]
    assert "First point" not in d[last].get_text()


def test_characters_the_font_lacks_are_reported():
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hello 你好"}]}]}
    meta, _ = compose(doc=doc)
    assert meta["warnings"][0]["id"] == "missing_glyphs" and "你" in meta["warnings"][0]["message"]


# ---------- API ----------
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EMAIL = "interior-studio@example.com"


@pytest.fixture(autouse=True)
def book_mode(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")


@pytest.fixture(scope="module")
def client():
    if not asyncio.run(server.db.users.find_one({"email": EMAIL})):
        asyncio.run(server.db.users.insert_one({
            "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "I", "tier": "free",
            "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        yield c


def auth(c):
    return {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"}).json()["token"]}


def test_preview_is_free_and_serves_page_pictures(client):
    r = client.post("/api/interior/preview", json={"style": "romance", "platform": "ingramspark", "trim_size": "6x9",
                                                    "details": {"title": "T", "author": "A"}, "content": book(2, 8)})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["page_count"] > 5 and meta["platform_name"] == "IngramSpark"
    assert any(n["id"] == "too_few_pages" for n in meta["notes"])     # IngramSpark: 18 pages minimum
    img = client.get(f"/api/interior/preview/{meta['key']}/page/1")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/interior/preview/{meta['key']}/page/999").status_code == 404
    assert client.post("/api/interior/preview", json={"style": "nope"}).status_code == 400


def test_send_to_book_makes_the_interior_and_keeps_the_print_file_for_the_book(client):
    h = auth(client)
    pid = client.post("/api/projects", headers=h, json={"name": "Studio Book", "platform": "kdp", "trim_size": "6x9",
                                                         "paper_type": "white_50lb", "binding": "paperback",
                                                         "page_count": 200, "project_type": "interior"}).json()["id"]
    doc = book(3, 10)
    r = client.post(f"/api/projects/{pid}/interior-studio", headers=h,
                    json={"style": "classic_novel", "details": {"title": "My Book", "author": "Me"}, "content": doc})
    assert r.status_code == 200, r.text
    body = r.json()
    pages = body["interior_studio"]["page_count"]
    assert body["file_metadata"]["made_in_interior_studio"] is True
    assert body["file_metadata"]["pdf_pages"] == pages
    assert not [c for c in body["compliance"] if c.get("id") == "interior_safety_margin" and c.get("status") == "fail"]
    p = asyncio.run(server.db.projects.find_one({"_id": server.ObjectId(pid)}))
    assert p["page_count"] == pages and p["interior_studio_doc"]["content"] == doc
    assert client.get(f"/api/projects/{pid}/interior-studio", headers=h).json()["doc"]["details"]["title"] == "My Book"
    r = client.get(f"/api/projects/{pid}/slot/interior/original", headers=h)
    assert r.status_code == 402 and r.json()["detail"]["code"] == "book_required"
    asyncio.run(server.db.projects.update_one({"_id": server.ObjectId(pid)}, {"$set": {"promo_access": "full_access"}}))
    r = client.get(f"/api/projects/{pid}/slot/interior/original", headers=h)
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


# ---------- pictures ----------
def _png(w, h, color=(200, 30, 30)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def _with_picture(pid, width=100, caption=""):
    return {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "Pictures"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": TEXT}]},
        {"type": "image", "attrs": {"id": pid, "width": width, "caption": caption}},
        {"type": "paragraph", "content": [{"type": "text", "text": "After the picture."}]},
    ]}


def test_a_picture_prints_in_black_ink_only_inside_the_margins(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "IMAGE_DIR", tmp_path)
    info = S.save_image(_png(1800, 1200), tmp_path)
    assert (tmp_path / f"{info['id']}.jpg").exists() and (tmp_path / f"{info['id']}.view.jpg").exists()
    assert S.save_image(_png(1800, 1200), tmp_path)["id"] == info["id"]          # same picture stored once
    assert S.image_ids(_with_picture(info["id"])) == [info["id"]]
    meta, pdf = compose(doc=_with_picture(info["id"], 100, "The old lighthouse"))
    assert meta["pictures"] == 1
    assert not [w for w in meta["warnings"] if w["id"] == "low_res_pictures"]   # 1800 px over ~4.5" > 300 DPI
    d = pymupdf.open(stream=pdf, filetype="pdf")
    pages = [p for p in d if p.get_images()]
    assert len(pages) == 1 and "The old lighthouse" in pages[0].get_text()
    xref = pages[0].get_images()[0][0]
    pix = pymupdf.Pixmap(d, xref)
    assert pix.n == 4                                                             # CMYK
    c, m, y, k = pix.pixel(pix.width // 2, pix.height // 2)
    assert c == m == y == 0 and k > 0                                             # black ink only
    f = tmp_path / "b.pdf"
    f.write_bytes(pdf)
    assert check_interior_safety_margins(str(f), "kdp", 6, 9) == []


def test_a_small_picture_gets_a_blurry_warning_with_its_page(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "IMAGE_DIR", tmp_path)
    pid = S.save_image(_png(400, 300), tmp_path)["id"]
    meta, _ = compose(doc=_with_picture(pid))
    w = [w for w in meta["warnings"] if w["id"] == "low_res_pictures"]
    assert w and "picture 1 (page" in w[0]["message"] and "DPI" in w[0]["message"]
    smaller, _ = compose(doc=_with_picture(pid, 25))                             # a quarter of the column
    assert not [w for w in smaller["warnings"] if w["id"] == "low_res_pictures"]


def test_a_missing_or_bad_picture_is_skipped_and_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "IMAGE_DIR", tmp_path)
    meta, _ = compose(doc=_with_picture("0" * 32))
    assert meta["pictures"] == 0 and any(w["id"] == "missing_pictures" for w in meta["warnings"])
    with pytest.raises(ValueError):
        S.save_image(b"not a picture", tmp_path)


def test_picture_upload_api_and_book_keeps_its_pictures(client):
    r = client.post("/api/interior/images", files={"file": ("p.png", _png(1500, 1000), "image/png")})
    assert r.status_code == 200, r.text
    pic = r.json()
    assert pic["width"] == 1500 and pic["url"] == f"/api/interior/images/{pic['id']}"
    got = client.get(pic["url"])
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    assert client.get("/api/interior/images/nothere").status_code == 404
    assert client.post("/api/interior/images", files={"file": ("x.png", b"nope", "image/png")}).status_code == 400
    prev = client.post("/api/interior/preview", json={"style": "classic_novel", "details": {"title": "T"},
                                                       "content": _with_picture(pic["id"])})
    assert prev.status_code == 200 and prev.json()["pictures"] == 1
    h = auth(client)
    pid = client.post("/api/projects", headers=h, json={"name": "Pic Book", "platform": "kdp", "trim_size": "6x9",
                                                         "paper_type": "white_50lb", "binding": "paperback",
                                                         "page_count": 100, "project_type": "interior"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/interior-studio", headers=h,
                    json={"style": "classic_novel", "details": {"title": "Pic Book"}, "content": _with_picture(pic["id"])})
    assert r.status_code == 200, r.text
    p = asyncio.run(server.db.projects.find_one({"_id": server.ObjectId(pid)}))
    assert p["interior_images"] == [pic["id"]]
    # an old picture a book uses is kept; an old unused one is removed
    spare = client.post("/api/interior/images", files={"file": ("s.png", _png(300, 300, (1, 2, 3)), "image/png")}).json()
    old = time.time() - server.INTERIOR_IMAGE_KEEP_S - 60
    for i in (pic["id"], spare["id"]):
        os.utime(server.INTERIOR_IMAGE_DIR / f"{i}.jpg", (old, old))
    asyncio.run(server._sweep_interior_images())
    assert (server.INTERIOR_IMAGE_DIR / f"{pic['id']}.jpg").exists()
    assert not (server.INTERIOR_IMAGE_DIR / f"{spare['id']}.jpg").exists()
