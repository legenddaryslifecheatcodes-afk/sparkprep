"""Stage timings at the file sizes customers actually upload: ~10-15 MB and ~24-32 MB, cover and interior.

Reuses profile_stages.py's harness (real app in-process, timers around the real functions, production code
untouched) with realistic, photo-like files instead of flat test art. Also times Final Review and the
same-book fingerprint, which run on every rescan / export.

Usage:  python backend/tools/profile_sizes.py [reps=1]
"""
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_stages as ps  # noqa: E402  (sets up env, in-memory app, timers)
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 1


def photo_like(w, h, grain, seed=1):
    """Smooth colour fields + film grain: compresses like a real photographic cover, not like flat art."""
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 255, (h // 60 + 2, w // 60 + 2, 3), dtype=np.uint8)
    base = np.asarray(Image.fromarray(small).resize((w, h), Image.BICUBIC), dtype=np.int16)
    noise = rng.normal(0, grain, (h, w, 1)).astype(np.int16)
    return Image.fromarray(np.clip(base + noise, 0, 255).astype(np.uint8))


def cover_png(w, h, grain, target_mb):
    img = photo_like(w, h, grain)
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("arialbd.ttf", int(h * 0.06))
    except OSError:
        f = ImageFont.load_default()
    d.text((int(w * 0.55), int(h * 0.12)), "THE LANTERN KEEPER", font=f, fill=(255, 255, 255))
    d.text((int(w * 0.55), int(h * 0.82)), "MARIA COLE", font=f, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=98, subsampling=0, dpi=(300, 300))
    data = buf.getvalue()
    print(f"  cover {w}x{h} ({w*h/1e6:.1f} MP): {len(data)/1e6:.1f} MB (target ~{target_mb} MB)")
    return data


def interior_pdf_with_images(pages, jpeg_quality, target_mb, art_px=(900, 600), grain=18):
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch))
    for pg in range(pages):                                      # a distinct illustration on every page
        b = io.BytesIO()
        photo_like(art_px[0], art_px[1], grain, seed=pg + 10).save(b, "JPEG", quality=jpeg_quality)
        c.drawImage(ImageReader(io.BytesIO(b.getvalue())), 0.75 * inch, 5.2 * inch, width=4.5 * inch, height=3.0 * inch)
        c.setFont("Times-Roman", 11)
        for i in range(16):
            c.drawString(0.9 * inch, (4.8 - i * 0.26) * inch, f"Page {pg+1}. She carried the lantern down to the harbor and waited, line {i}.")
        c.showPage()
    c.save()
    data = buf.getvalue()
    print(f"  interior {pages} pp: {len(data)/1e6:.1f} MB (target ~{target_mb} MB)")
    return data


def run_cover_full(cl, data):
    row = ps.run_cover(cl, data)                                  # upload, scan, Repair Bay, confirm, export
    pid = [p for p in ps.asyncio.run(server_projects())][-1]
    t = time.perf_counter()
    cl.post(f"/api/projects/{pid}/final-review")
    row["7 final review (rescan button)"] = round(time.perf_counter() - t, 2)
    p = ps.asyncio.run(ps.server.db.projects.find_one({"_id": ps.server.ObjectId(pid)}))
    t = time.perf_counter()
    ps.server.book_pass.fingerprint.project_fingerprint(p, ps.server.UPLOAD_DIR)
    row["8 same-book fingerprint (per export)"] = round(time.perf_counter() - t, 2)
    return row


async def server_projects():
    out = []
    async for p in ps.server.db.projects.find({}):
        out.append(str(p["_id"]))
    return out


def main():
    print("building test files ...", flush=True)
    covers = {
        "COVER small (~10-15 MB)": cover_png(3780, 2775, 9, "10-15"),
        "COVER big (~24-32 MB)": cover_png(5400, 3960, 11, "24-32"),
    }
    interiors = {
        "INTERIOR small (~10-15 MB, 300 pp)": interior_pdf_with_images(300, 80, "10-15"),
        "INTERIOR big (~24-32 MB, 300 pp)": interior_pdf_with_images(300, 95, "24-32"),
    }
    ps.asyncio.run(ps.server.db.users.insert_one({"email": "p@example.com", "password_hash": ps.server.hash_password("TestPass123!"),
                                                  "name": "P", "tier": "studio", "created_at": "2026-09-23T00:00:00+00:00",
                                                  "exports_this_month": 0, "books_this_month": 0}))
    with ps.TestClient(ps.server.app) as cl:
        cl.headers["Authorization"] = "Bearer " + cl.post("/api/auth/login", json={"email": "p@example.com", "password": "TestPass123!"}).json()["token"]
        for name, data in covers.items():
            rows = []
            for i in range(REPS):
                print(f"{name} run {i+1}/{REPS} (cpu {ps.cpu_load()}%) ...", flush=True)
                rows.append(run_cover_full(cl, data))
            ps.summarize(name, rows)
        for name, data in interiors.items():
            rows = []
            for i in range(REPS):
                print(f"{name} run {i+1}/{REPS} (cpu {ps.cpu_load()}%) ...", flush=True)
                rows.append(ps.run_interior(cl, data, 300))
            ps.summarize(name, rows)


if __name__ == "__main__":
    main()
