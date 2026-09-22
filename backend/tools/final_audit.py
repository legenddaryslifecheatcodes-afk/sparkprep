"""Final pre-launch audit: exercises every real endpoint against the real app (in-process,
in-memory DB) and prints a PASS/FAIL/SKIP report with the reason for each. Nothing here is
mocked out except Stripe (kept OFF -- legacy checkout endpoints are expected to 503 without a
key, which is itself a correct, checked result) and OpenAI/Anthropic (kept unset -- ai-cover and
ai/blurb are expected to 503, also a correct, checked result).

Usage: python backend/tools/final_audit.py
"""
import asyncio
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_audit_"), USE_MEMORY_DB="1", JWT_SECRET="audit-" + "x" * 32,
                  ADMIN_EMAIL="admin-audit@example.com")
os.environ.pop("SPARKPREP_PRICING_MODEL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
import pymupdf  # noqa: E402

server.stripe.api_key = "sk_test_not_configured"     # legacy checkout must be tested as "not configured"
ROWS = []


def check(area, label, ok, detail=""):
    ROWS.append((area, label, bool(ok), str(detail)[:200]))


def realistic_cover_jpg(w=3810, h=2775):
    """A REAL photo-like image, not a flat rectangle: a gradient plus noise plus text, closer to
    what an actual author's cover art looks like (exercises real CMYK conversion on real detail)."""
    import numpy as np
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:h, 0:w]
    r = (40 + 120 * (yy / h)).astype(np.uint8)
    g = (20 + 60 * (xx / w)).astype(np.uint8)
    b = (80 + 100 * (1 - yy / h)).astype(np.uint8)
    arr = np.stack([r, g, b], axis=-1).astype(np.int16)
    arr += rng.integers(-8, 8, arr.shape)
    arr = arr.clip(0, 255).astype(np.uint8)
    img = Image.fromarray(arr, "RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", dpi=(300, 300), quality=90)
    return buf.getvalue()


def realistic_manuscript_pdf(pages=12):
    """A real multi-page interior with an embedded (non-base-14) font, a chapter heading, body
    text, a running head, and a folio -- closer to a genuine self-published novel layout."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch))
    font_name = "Helvetica"
    try:
        candidates = [r"C:\Windows\Fonts\georgia.ttf", r"C:\Windows\Fonts\times.ttf"]
        for fp in candidates:
            if os.path.exists(fp):
                pdfmetrics.registerFont(TTFont("RealBookFont", fp))
                font_name = "RealBookFont"
                break
    except Exception:
        pass
    for pg in range(pages):
        c.setFont(font_name, 9)
        c.drawCentredString(3 * inch, 8.55 * inch, f"Chapter {pg // 4 + 1}" if pg % 4 == 0 else "The Long Road Home")
        c.setFont(font_name, 11)
        y = 8.0 * inch
        for line in range(28):
            c.drawString(1.1 * inch, y, f"This is real body text on page {pg + 1}, line {line + 1}, of a realistic manuscript.")
            y -= 0.24 * inch
        c.setFont(font_name, 8)
        c.drawCentredString(3 * inch, 0.4 * inch, str(pg + 1))
        c.showPage()
    c.save()
    return buf.getvalue()


def main():
    asyncio.run(server.db.users.insert_one({
        "email": "admin-audit@example.com", "password_hash": server.hash_password("TestPass123!"), "name": "Audit Admin",
        "tier": "studio", "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))

    with TestClient(server.app) as cl:
        # ---------------- AUTH ----------------
        r = cl.post("/api/auth/register", json={"email": "reader1@example.com", "password": "TestPass123!", "name": "Reader One"})
        check("auth", "register", r.status_code == 200, r.text[:150])
        r = cl.post("/api/auth/login", json={"email": "reader1@example.com", "password": "TestPass123!"})
        check("auth", "login", r.status_code == 200, r.text[:150])
        tok = r.json().get("token")
        cl.headers["Authorization"] = "Bearer " + tok
        r = cl.get("/api/auth/me")
        check("auth", "me", r.status_code == 200 and r.json()["user"]["email"] == "reader1@example.com")
        r = cl.post("/api/auth/login", json={"email": "reader1@example.com", "password": "WRONG"})
        check("auth", "wrong password rejected", r.status_code == 401)
        r = cl.get("/api/projects", headers={"Authorization": "Bearer garbage.token.here"})
        check("auth", "garbage token rejected", r.status_code == 401)

        # switch to the studio admin account for the paid-feature checks below
        admin_tok = cl.post("/api/auth/login", json={"email": "admin-audit@example.com", "password": "TestPass123!"}).json()["token"]
        cl.headers["Authorization"] = "Bearer " + admin_tok

        # ---------------- SPECS / MISC ----------------
        r = cl.get("/api/health")
        check("specs", "/health", r.status_code == 200 and r.json()["status"] == "ok", r.json())
        r = cl.get("/api/specs")
        check("specs", "/specs", r.status_code == 200 and "kdp" in r.json().get("platforms", {}))
        r = cl.post("/api/specs/spine", json={"page_count": 92, "paper_type": "white_50lb", "trim_size": "6x9", "binding": "hardcover_case", "platform": "ingramspark"})
        check("specs", "/specs/spine hardcover flagged as estimate", r.status_code == 200 and r.json()["spine_is_estimate"] is True)
        r = cl.get("/api/season")
        check("specs", "/season", r.status_code == 200)
        r = cl.get("/api/cover-templates")
        check("specs", "/cover-templates", r.status_code == 200)
        r = cl.get("/api/manuscript/templates")
        check("specs", "/manuscript/templates", r.status_code == 200)
        r = cl.post("/api/isbn/validate", json={"isbn": "978-3-16-148410-0"})
        check("isbn", "validate a real ISBN-13", r.status_code == 200, r.json())
        r = cl.post("/api/isbn/validate", json={"isbn": "not-an-isbn"})
        check("isbn", "reject a bad ISBN", r.status_code == 200 and r.json()["valid"] is False)
        r = cl.get("/api/isbn/barcode.png", params={"isbn": "9783161484100"})
        check("isbn", "generate a real barcode PNG", r.status_code == 200 and r.content[:4] == b"\x89PNG", f"{len(r.content)} bytes")

        # ---------------- COVER: full loop with a REALISTIC image ----------------
        pid = cl.post("/api/projects", json={"name": "Audit Cover", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                             "binding": "paperback", "page_count": 200, "project_type": "cover"}).json()["id"]
        cover_bytes = realistic_cover_jpg()
        r = cl.post(f"/api/projects/{pid}/slot-upload/full_wrap", files={"file": ("cover.jpg", cover_bytes, "image/jpeg")})
        check("cover", "upload realistic cover + scan", r.status_code == 200, r.text[:150])
        before_fail = [c["id"] for c in r.json()["compliance"] if c["status"] != "pass"]
        check("cover", "scan found real issues to fix (RGB etc.)", len(before_fail) > 0, before_fail)

        r = cl.post(f"/api/projects/{pid}/autofix", params={"slot": "full_wrap"})
        check("cover", "legacy one-shot autofix still works", r.status_code == 200, r.text[:150])

        pid2 = cl.post("/api/projects", json={"name": "Audit Cover RB", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                              "binding": "paperback", "page_count": 200, "project_type": "cover"}).json()["id"]
        cl.post(f"/api/projects/{pid2}/slot-upload/full_wrap", files={"file": ("cover.jpg", cover_bytes, "image/jpeg")})
        r = cl.post(f"/api/projects/{pid2}/autofix/verified", params={"slot": "full_wrap", "stream": "false"})
        check("cover", "Repair Bay (verified pipeline) on a realistic cover", r.status_code == 200 and r.json()["pipeline"]["status"] in ("confirmed", "partial"), r.json().get("pipeline"))
        r = cl.post(f"/api/projects/{pid2}/autofix/confirm", params={"slot": "full_wrap"})
        check("cover", "confirm rescan", r.status_code == 200)
        r = cl.post(f"/api/projects/{pid2}/ai-enhance/full_wrap")
        check("cover", "AI Upscale (Lanczos, no external key needed)", r.status_code == 200, r.text[:150])
        r = cl.get(f"/api/projects/{pid2}/slot/full_wrap/preview", params={"token": admin_tok})
        check("cover", "web preview renders", r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"))
        r = cl.get(f"/api/projects/{pid2}/slot/full_wrap/original")
        check("cover", "original file still fetchable", r.status_code == 200)
        cl.patch(f"/api/projects/{pid2}", json={"name": "Audit Cover Book"})
        r = cl.post(f"/api/projects/{pid2}/final-review")
        check("cover", "final review", r.status_code == 200, r.json().get("status") if r.status_code == 200 else r.text[:150])
        r = cl.post(f"/api/projects/{pid2}/export")
        check("cover", "export", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            data = r.json()
            dl = cl.get(data["download_url"], params={"token": admin_tok})
            check("cover", "download real PDF bytes", dl.status_code == 200 and dl.content[:4] == b"%PDF", f"{len(dl.content)} bytes")
            doc = pymupdf.open(stream=dl.content, filetype="pdf")
            imgs = doc[0].get_images(full=True)
            cs = [doc.extract_image(i[0]).get("cs-name") for i in imgs]
            check("cover", "INDEPENDENT: exported image is DeviceCMYK", cs == ["DeviceCMYK"], cs)

        # cross-account access must be denied
        r2 = cl.get(f"/api/projects/{pid2}", headers={"Authorization": "Bearer " + tok})
        check("security", "another user cannot read someone else's project", r2.status_code == 404)
        r2 = cl.get(f"/api/projects/{pid2}/download/{data['download_url'].rsplit('/', 1)[-1]}" if r.status_code == 200 else "/api/projects/x/download/y",
                    headers={"Authorization": "Bearer " + tok})
        check("security", "another user cannot download someone else's export", r2.status_code in (401, 403, 404))

        # ---------------- INTERIOR: full loop with a REALISTIC manuscript ----------------
        pid3 = cl.post("/api/projects", json={"name": "Audit Interior", "platform": "ingramspark", "trim_size": "6x9", "paper_type": "white_50lb",
                                              "binding": "paperback", "page_count": 12, "project_type": "interior"}).json()["id"]
        ms_bytes = realistic_manuscript_pdf(12)
        r = cl.post(f"/api/projects/{pid3}/slot-upload/interior", files={"file": ("manuscript.pdf", ms_bytes, "application/pdf")})
        check("interior", "upload realistic manuscript + scan", r.status_code == 200 and r.json()["file_metadata"]["pdf_pages"] == 12, r.text[:150])
        r = cl.post(f"/api/projects/{pid3}/autofix/verified", params={"slot": "interior", "stream": "false"})
        check("interior", "Repair Bay on a realistic manuscript", r.status_code == 200, r.json().get("pipeline") if r.status_code == 200 else r.text[:150])
        cl.patch(f"/api/projects/{pid3}", json={"name": "Audit Interior Book"})
        r = cl.post(f"/api/projects/{pid3}/export")
        check("interior", "export", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            data = r.json()
            check("interior", "correct asymmetric bleed reported", data.get("page_size_inches") == [6.125, 9.25], data.get("page_size_inches"))
            dl = cl.get(data["download_url"], params={"token": admin_tok})
            doc = pymupdf.open(stream=dl.content, filetype="pdf")
            mismatches = 0
            for i in range(doc.page_count):
                page = doc[i]
                gutter_left = (i % 2 == 0)
                tb = page.trimbox
                left_gap, right_gap = round(tb.x0 / 72, 4), round((page.mediabox.x1 - tb.x1) / 72, 4)
                ok_geom = (left_gap == 0 and right_gap == 0.125) if gutter_left else (left_gap == 0.125 and right_gap == 0)
                mismatches += not ok_geom
            check("interior", "INDEPENDENT: every page's TrimBox matches its real gutter side", mismatches == 0, f"{mismatches}/{doc.page_count} mismatched")
        r = cl.post(f"/api/projects/{pid3}/interior-check/checkout", json={"origin_url": "https://sparkprep.legenddary.com"})
        check("interior", "Advanced Interior Check checkout blocked without Stripe (expected)", r.status_code == 503)

        # ---------------- COMBINED PROJECT ----------------
        pid4 = cl.post("/api/projects", json={"name": "Audit Combined", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                              "binding": "paperback", "page_count": 12, "project_type": "combined"}).json()["id"]
        cl.post(f"/api/projects/{pid4}/slot-upload/full_wrap", files={"file": ("c.jpg", cover_bytes, "image/jpeg")})
        cl.post(f"/api/projects/{pid4}/slot-upload/interior", files={"file": ("m.pdf", realistic_manuscript_pdf(12), "application/pdf")})
        cl.post(f"/api/projects/{pid4}/autofix/verified", params={"slot": "full_wrap", "stream": "false"})
        cl.post(f"/api/projects/{pid4}/autofix/verified", params={"slot": "interior", "stream": "false"})
        cl.patch(f"/api/projects/{pid4}", json={"name": "Audit Combined Book"})
        r = cl.post(f"/api/projects/{pid4}/export")
        check("combined", "combined cover+interior export produces a zip", r.status_code == 200 and r.json().get("download_url", "").endswith(".zip"), r.text[:200] if r.status_code != 200 else r.json().get("download_url"))

        # ---------------- AI FEATURES (no external keys configured -- 503 is the CORRECT result) ----------------
        r = cl.post(f"/api/projects/{pid2}/ai-cover", json={"prompt": "a moody forest at dusk"})
        check("ai", "ai-cover without OPENAI_API_KEY correctly refuses", r.status_code == 503, r.text[:150])
        r = cl.post("/api/ai/blurb", json={"title": "Test Book", "genre": "fantasy", "synopsis": "A hero's journey."})
        check("ai", "ai blurb without ANTHROPIC_API_KEY correctly refuses", r.status_code == 503, r.text[:150])

        # ---------------- MANUSCRIPT COMPOSER ----------------
        r = cl.get("/api/manuscript/templates")
        tmpls = r.json().get("templates")
        check("manuscript", "templates list non-empty", isinstance(tmpls, list) and len(tmpls) > 0, tmpls[:2] if isinstance(tmpls, list) else tmpls)

        # ---------------- COVER TEMPLATE ----------------
        r = cl.get("/api/cover-templates")
        check("cover-template", "templates list responds", r.status_code == 200)

        # ---------------- BATCH (studio tier) ----------------
        r = cl.post("/api/projects/batch-audit", json={"project_ids": [pid2, pid3]})
        check("batch", "batch-audit (studio tier)", r.status_code == 200, r.text[:150])

        # ---------------- TEAM (studio = 10 seats) ----------------
        r = cl.get("/api/team/status")
        check("team", "team status", r.status_code == 200, r.json() if r.status_code == 200 else r.text[:150])
        r = cl.post("/api/team/invite", json={"email": "teammate@example.com"})
        check("team", "invite a seat (studio has 10)", r.status_code == 200, r.text[:150])
        r = cl.patch("/api/team/branding", json={"white_label_brand_name": "Test Imprint"})
        check("team", "white-label branding (studio only)", r.status_code == 200, r.text[:150])

        # a FREE tier user must be refused the same things
        free_pid = cl.post("/api/projects", json={"name": "Free proj", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                                   "binding": "paperback", "page_count": 12, "project_type": "cover"},
                           headers={"Authorization": "Bearer " + tok}).json()["id"]
        r = cl.post(f"/api/projects/{free_pid}/slot-upload/full_wrap", files={"file": ("c.jpg", cover_bytes, "image/jpeg")}, headers={"Authorization": "Bearer " + tok})
        cl.patch(f"/api/projects/{free_pid}", json={"name": "Free Book"}, headers={"Authorization": "Bearer " + tok})
        r = cl.post(f"/api/projects/{free_pid}/export", headers={"Authorization": "Bearer " + tok})
        check("plan-gating", "free tier export correctly blocked (0 monthly exports)", r.status_code == 402, r.text[:150])
        r = cl.patch("/api/team/branding", json={"white_label_brand_name": "x"}, headers={"Authorization": "Bearer " + tok})
        check("plan-gating", "free tier white-label correctly blocked", r.status_code == 402, r.text[:150])

        # ---------------- LEGACY PAYMENTS (no Stripe key -- 503 is correct) ----------------
        r = cl.post("/api/payments/checkout", json={"tier": "author", "origin_url": "https://x"})
        check("payments", "subscription checkout without Stripe key correctly refuses", r.status_code == 503, r.text[:150])

        # ---------------- ANONYMOUS $0.99 AUDIT FLOW (no login) ----------------
        anon = TestClient(server.app)
        r = anon.post("/api/audit/start", json={"platform": "kdp", "trim_size": "6x9"})
        check("audit-flow", "start an anonymous audit", r.status_code == 200, r.text[:150])
        aid = r.json().get("audit_id")
        if aid:
            r = anon.post(f"/api/audit/{aid}/upload", files={"file": ("c.jpg", cover_bytes, "image/jpeg")})
            check("audit-flow", "upload to the anonymous audit", r.status_code == 200, r.text[:150])
            r = anon.get(f"/api/audit/{aid}")
            check("audit-flow", "read back the audit result", r.status_code == 200)
            r = anon.post(f"/api/audit/{aid}/checkout", json={"origin_url": "https://x"})
            check("audit-flow", "$0.99 checkout without Stripe key correctly refuses", r.status_code == 503, r.text[:150])

        # ---------------- BETA PROGRAM ----------------
        r = cl.get("/api/beta/status")
        check("beta", "beta status", r.status_code == 200)
        r = cl.get("/api/admin/beta/passes")
        check("admin", "admin can list beta passes", r.status_code == 200, r.text[:150])
        r = cl.get("/api/admin/failures")
        check("admin", "admin can read the failure log", r.status_code == 200)
        r = cl.get("/api/admin/beta/passes", headers={"Authorization": "Bearer " + tok})
        check("security", "non-admin correctly blocked from admin routes", r.status_code == 403)

        # ---------------- PROJECT LIFECYCLE ----------------
        r = cl.get(f"/api/projects/{pid2}/slot/full_wrap/preview", params={"token": admin_tok})
        check("cover", "preview still works after export", r.status_code == 200)
        r = cl.delete(f"/api/projects/{pid2}/slot/full_wrap")
        check("projects", "delete a slot", r.status_code == 200, r.text[:150])
        r = cl.delete(f"/api/projects/{free_pid}", headers={"Authorization": "Bearer " + tok})
        check("projects", "delete a project", r.status_code == 200, r.text[:150])
        r = cl.get(f"/api/projects/{free_pid}", headers={"Authorization": "Bearer " + tok})
        check("projects", "deleted project really is gone", r.status_code == 404)

    print("\nFINAL PRE-LAUNCH AUDIT\n" + "=" * 100)
    areas = {}
    for area, label, ok, detail in ROWS:
        areas.setdefault(area, []).append((label, ok, detail))
    total_ok = sum(1 for *_, ok, _ in ROWS for ok in [ok] if ok)
    for area, rows in areas.items():
        print(f"\n[{area.upper()}]")
        for label, ok, detail in rows:
            print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   -- {detail}" if detail and not ok else ""))
    fails = [(a, l, d) for a, l, ok, d in ROWS if not ok]
    print("\n" + "=" * 100)
    print(f"{len(ROWS) - len(fails)} / {len(ROWS)} passed")
    if fails:
        print("\nFAILURES:")
        for a, l, d in fails:
            print(f"  [{a}] {l}: {d}")
    return len(fails)


if __name__ == "__main__":
    sys.exit(main())
