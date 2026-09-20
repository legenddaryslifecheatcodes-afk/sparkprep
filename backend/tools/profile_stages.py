"""Per-stage timing profiler for the SparkPrep pipelines.

Runs the REAL app in-process (in-memory DB, temp upload folder) with timers wrapped around the real
functions -- production code is not modified and pays nothing. Reports, per workload and per run:

  upload handling | initial scan | issue detection | repair operations (incl. CMYK conversion) |
  verification (Agent 3) | Agent 4 audit/sign-off | final output (export)

Usage:   python backend/tools/profile_stages.py [reps=3]  ->  prints a table, writes tools/perf_last_run.json
Numbers depend heavily on how busy the machine is, so each run records CPU load; compare like with like.
"""
import collections
import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_prof_"), USE_MEMORY_DB="1", JWT_SECRET="profiler-" + "x" * 32)
os.environ.pop("SPARKPREP_TIME_LIMITS", None)
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import asyncio  # noqa: E402
import file_processor as fp  # noqa: E402
import pdfx_validator as pv  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

server.ANTHROPIC_API_KEY = ""
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 3

# ------------------------------------------------------------------ timers
TIMES = collections.defaultdict(list)
_lock = threading.Lock()


def _wrap(mod, name, label):
    orig = getattr(mod, name)

    def wrapper(*a, **k):
        t = time.perf_counter()
        try:
            return orig(*a, **k)
        finally:
            with _lock:
                TIMES[label].append(time.perf_counter() - t)
    setattr(mod, name, wrapper)


for mod, name, label in [
    (fp, "analyze_file", "analyze_file"), (server, "analyze_file", "analyze_file"),
    (fp, "check_total_ink_coverage", "ink coverage check"), (server, "check_total_ink_coverage", "ink coverage check"),
    (pv, "check_cover_safety_margins", "cover margin check (OCR)"), (server, "check_cover_safety_margins", "cover margin check (OCR)"),
    (pv, "check_interior_safety_margins", "interior margin check"), (server, "check_interior_safety_margins", "interior margin check"),
    (pv, "run_pdf_structure_audit", "PDF structure audit"), (server, "run_pdf_structure_audit", "PDF structure audit"),
    (fp, "run_compliance_checks", "compliance checks (all)"), (server, "run_compliance_checks", "compliance checks (all)"),
    (server, "convert_to_cmyk", "REPAIR: CMYK conversion + ink clamp"),
    (server, "autofix_cover_safe_margin", "REPAIR: cover safe-margin"),
    (server, "autofix_interior_safety_margins", "REPAIR: interior margins/page size"),
    (server, "convert_to_pdfx1a", "REPAIR: Ghostscript PDF/X-1a"),
    (server, "build_print_ready_pdf", "EXPORT: build cover PDF/X-1a"),
    (server, "build_interior_pdf_x1a", "EXPORT: build interior PDF/X-1a"),
]:
    _wrap(mod, name, label)


def take():
    with _lock:
        out = {k: round(sum(v), 2) for k, v in TIMES.items()}
        TIMES.clear()
    return out


def cpu_load():
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average"],
                           capture_output=True, text=True, timeout=20)
        return float(r.stdout.strip())
    except Exception:
        return None


# ------------------------------------------------------------------ test data
def cover_jpg():
    from PIL import ImageDraw, ImageFont
    W, H = 3810, 2775
    img = Image.new("RGB", (W, H), (6, 4, 40))
    d = ImageDraw.Draw(img)
    for i in range(0, W, 300):
        d.rectangle([i, 0, i + 150, H], fill=(0, 10, 60))
    f = ImageFont.load_default(size=130)
    d.text((55, 60), "LAST LIGHT", font=f, fill=(255, 255, 255))
    d.text((55, H - 260), "J. MERCER", font=f, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", dpi=(300, 300), quality=92)
    return buf.getvalue()


def interior_pdf(pages=300):
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch))
    for pg in range(pages):
        c.setFont("Times-Roman", 11)
        for i in range(34):
            c.drawString(0.06 * inch, (8.8 - i * 0.25) * inch, f"page {pg+1} line {i} text jammed against the trim edge of the page")
        c.showPage()
    c.save()
    return buf.getvalue()


# ------------------------------------------------------------------ harness
def timed(fn):
    t = time.perf_counter()
    r = fn()
    return r, round(time.perf_counter() - t, 2)


def new_project(cl, ptype, pages):
    return cl.post("/api/projects", json={"name": "Prof", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                          "binding": "paperback", "page_count": pages, "project_type": ptype}).json()["id"]


def run_cover(cl, data):
    row = {}
    pid = new_project(cl, "cover", 200)
    take()
    r, w = timed(lambda: cl.post(f"/api/projects/{pid}/slot-upload/full_wrap", files={"file": ("c.jpg", data, "image/jpeg")}))
    assert r.status_code == 200, r.text
    t = take()
    scan = t.get("compliance checks (all)", 0) + t.get("analyze_file", 0)
    row["1 upload handling (store/parse, loopback)"] = round(w - scan, 2)
    row["2 initial scan (on upload)"] = round(scan, 2)
    p = asyncio.run(server.db.projects.find_one({"_id": server.ObjectId(pid)}))
    fn = p["slots"]["full_wrap"]["stored_filename"]
    _, w = timed(lambda: server._scan_slot_sync(p, "full_wrap", fn))
    t = take()
    row["3 issue detection (Agent 1's scan)"] = w
    row["   of which cover margin check (OCR)"] = t.get("cover margin check (OCR)", 0)
    row["   of which ink coverage check"] = t.get("ink coverage check", 0)
    row["   of which analyze_file"] = t.get("analyze_file", 0)
    r, w = timed(lambda: cl.post(f"/api/projects/{pid}/autofix/verified", params={"slot": "full_wrap", "stream": "false"}))
    body = r.json()
    t = take()
    tm = body["pipeline"]["timings"]
    row["_status"] = body["pipeline"]["status"]
    row["4 whole verified pipeline"] = w
    row["   Agent 1 triage (issue detection)"] = tm["triage"]
    row["   Agent 2 repair (engine total)"] = tm["repair"]
    row["      CMYK conversion + ink clamp"] = t.get("REPAIR: CMYK conversion + ink clamp", 0)
    row["      safe-margin repair"] = t.get("REPAIR: cover safe-margin", 0)
    row["      engine's own re-check + margin OCR"] = round(max(tm["repair"] - t.get("REPAIR: CMYK conversion + ink clamp", 0) - t.get("REPAIR: cover safe-margin", 0), 0), 2)
    row["   Agent 3 verification"] = tm["verify"]
    row["   Agent 4 audit / sign-off"] = tm["audit"]
    _, w = timed(lambda: cl.post(f"/api/projects/{pid}/autofix/confirm", params={"slot": "full_wrap"}))
    row["5 confirm rescan (Enter)"] = w
    take()
    cl.patch(f"/api/projects/{pid}", json={"name": "Prof Book"})
    r, w = timed(lambda: cl.post(f"/api/projects/{pid}/export"))
    t = take()
    row["6 final output (export cover PDF/X-1a)"] = w
    row["_export_status"] = r.status_code
    return row


def run_interior(cl, data, pages):
    row = {}
    pid = new_project(cl, "interior", pages)
    take()
    r, w = timed(lambda: cl.post(f"/api/projects/{pid}/slot-upload/interior", files={"file": ("b.pdf", data, "application/pdf")}))
    assert r.status_code == 200, r.text
    t = take()
    scan = t.get("compliance checks (all)", 0) + t.get("analyze_file", 0)
    row["1 upload handling (store/parse, loopback)"] = round(w - scan, 2)
    row["2 initial scan (on upload, basic = page 1)"] = round(scan, 2)
    p = asyncio.run(server.db.projects.find_one({"_id": server.ObjectId(pid)}))
    fn = p["slots"]["interior"]["stored_filename"]
    _, w = timed(lambda: server._scan_slot_sync(p, "interior", fn))
    t = take()
    row["3 issue detection (Agent 1's scan)"] = w
    row["   of which interior margin check"] = t.get("interior margin check", 0)
    row["   of which PDF structure audit"] = t.get("PDF structure audit", 0)
    r, w = timed(lambda: cl.post(f"/api/projects/{pid}/autofix/verified", params={"slot": "interior", "stream": "false"}))
    body = r.json()
    t = take()
    tm = body["pipeline"]["timings"]
    row["_status"] = body["pipeline"]["status"]
    row["4 whole verified pipeline"] = w
    row["   Agent 1 triage (issue detection)"] = tm["triage"]
    row["   Agent 2 repair (engine total)"] = tm["repair"]
    row["      interior margin/page-size repair"] = t.get("REPAIR: interior margins/page size", 0)
    row["      Ghostscript PDF/X-1a"] = t.get("REPAIR: Ghostscript PDF/X-1a", 0)
    row["   Agent 3 verification"] = tm["verify"]
    row["   Agent 4 audit / sign-off"] = tm["audit"]
    asyncio.run(server.db.promo_codes.insert_one({"code": f"P{time.time_ns()}", "type": "advanced_interior_check", "used": False}))
    code = asyncio.run(server.db.promo_codes.find_one({"used": False}))["code"]
    cl.post(f"/api/projects/{pid}/redeem-promo", json={"code": code})
    take()
    r, w = timed(lambda: cl.post(f"/api/projects/{pid}/interior-check/run"))
    t = take()
    row[f"5a PAID Advanced Check, file already clean ({pages}pp)"] = w
    row["   (already repaired above, so scan-only) structure audit"] = t.get("PDF structure audit", 0)
    row["   (scan-only) interior margin check, all pages"] = t.get("interior margin check", 0)
    cl.patch(f"/api/projects/{pid}", json={"name": "Prof Book"})
    r, w = timed(lambda: cl.post(f"/api/projects/{pid}/export"))
    take()
    row["6 final output (export interior PDF/X-1a)"] = w
    row["_export_status"] = r.status_code

    # 5b: the paid check on a FRESH bad file, so it has to scan, repair and re-scan
    pid2 = new_project(cl, "interior", pages)
    cl.post(f"/api/projects/{pid2}/slot-upload/interior", files={"file": ("b.pdf", data, "application/pdf")})
    asyncio.run(server.db.promo_codes.insert_one({"code": f"Q{time.time_ns()}", "type": "advanced_interior_check", "used": False}))
    code = asyncio.run(server.db.promo_codes.find_one({"used": False}))["code"]
    cl.post(f"/api/projects/{pid2}/redeem-promo", json={"code": code})
    take()
    r, w = timed(lambda: cl.post(f"/api/projects/{pid2}/interior-check/run"))
    t = take()
    row[f"5b PAID Advanced Check, bad file: scan+repair+rescan"] = w
    row["   of which repair: margins/page size"] = t.get("REPAIR: interior margins/page size", 0)
    row["   of which repair: Ghostscript"] = t.get("REPAIR: Ghostscript PDF/X-1a", 0)
    row["   of which scans (structure audit + margin check)"] = round(t.get("PDF structure audit", 0) + t.get("interior margin check", 0), 2)
    return row


def summarize(name, rows):
    keys = [k for k in rows[0] if not k.startswith("_")]
    print(f"\n=== {name}  ({len(rows)} runs; median [min-max] seconds) ===")
    out = {}
    for k in keys:
        vals = [r[k] for r in rows]
        out[k] = {"median": round(statistics.median(vals), 2), "min": min(vals), "max": max(vals)}
        print(f"  {k:52s} {out[k]['median']:7.2f}  [{min(vals):.2f} - {max(vals):.2f}]")
    print("  statuses:", [r["_status"] for r in rows], "| export http:", [r["_export_status"] for r in rows])
    return out


def main():
    ghost = fp and getattr(server, "find_ghostscript", lambda: None)()
    meta = {"when": datetime.now(timezone.utc).isoformat(), "cpu_cores": os.cpu_count(), "ghostscript": bool(ghost),
            "cpu_load_before_%": cpu_load(), "reps": REPS}
    print("machine:", meta)
    asyncio.run(server.db.users.insert_one({"email": "p@example.com", "password_hash": server.hash_password("TestPass123!"), "name": "P", "tier": "studio",
                                            "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    cdata, idata = cover_jpg(), interior_pdf(300)
    results = {"meta": meta}
    with TestClient(server.app) as cl:
        tok = cl.post("/api/auth/login", json={"email": "p@example.com", "password": "TestPass123!"}).json()["token"]
        cl.headers["Authorization"] = "Bearer " + tok
        rows = []
        for i in range(REPS):
            print(f"cover run {i+1}/{REPS} (cpu {cpu_load()}%) ...", flush=True)
            rows.append(run_cover(cl, cdata))
        results["cover_3810x2775"] = summarize("COVER 3810x2775 RGB (10.6 MP) -- full loop", rows)
        rows = []
        for i in range(REPS):
            print(f"interior run {i+1}/{REPS} (cpu {cpu_load()}%) ...", flush=True)
            rows.append(run_interior(cl, idata, 300))
        results["interior_300pp"] = summarize("INTERIOR 300-page PDF -- full loop", rows)
    meta["cpu_load_after_%"] = cpu_load()
    (BACKEND / "tools" / "perf_last_run.json").write_text(json.dumps(results, indent=2))
    print("\nwrote tools/perf_last_run.json; cpu load after:", meta["cpu_load_after_%"])


if __name__ == "__main__":
    main()
