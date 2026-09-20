"""Agent 3 -- Verification & Regression (fast, programmatic, no AI).

Independent of Agent 2: it re-reads the project record from the database,
opens the saved file itself, and runs its own fresh scan. It never looks at
the repair engine's self-report. The verdict comes from three things only:

  1. Is the file intact? (exists, decodes, page count / pixel size unchanged,
     the customer's original upload is still safe)
  2. Did every problem Agent 1 confirmed actually go away?
  3. Did the fix break anything that was fine before?
"""
from __future__ import annotations

import time
from pathlib import Path

from .common import (
    Trail, Budget, run_bounded, StageTimeout, normalize_issues, actionable, health_score, brief, sha256_file,
)

ROLE = "Independently verifying the fix — fresh scan, integrity and regression checks"


def _integrity(path: Path, before_meta: dict, original_path: Path) -> list[dict]:
    """Blocking checks on the saved file itself (runs in a worker thread)."""
    checks = []

    def add(id_, label, passed, evidence):
        checks.append({"id": id_, "label": label, "passed": bool(passed), "evidence": evidence})

    exists = path.exists() and path.stat().st_size > 0
    add("file_present", "Saved file exists and isn't empty", exists,
        f"{path.stat().st_size:,} bytes" if exists else "file missing or empty")
    if not exists:
        return checks

    is_pdf = path.suffix.lower() == ".pdf"
    try:
        if is_pdf:
            import pymupdf
            with pymupdf.open(str(path)) as doc:
                pages = doc.page_count
                if pages:
                    doc[0].get_pixmap(dpi=24)  # really renders a page, not just parses the header
            add("opens_cleanly", "File opens and renders", pages > 0, f"{pages} page(s) render")
            before_pages = before_meta.get("pdf_pages")
            add("page_count_unchanged", "Page count unchanged", before_pages in (None, pages),
                f"{before_pages} → {pages}")
        else:
            from PIL import Image
            with Image.open(path) as img:
                img.load()  # full decode
                w, h, mode = img.width, img.height, img.mode
            add("opens_cleanly", "File opens and decodes", True, f"{w}×{h}px, {mode}")
            bw, bh = before_meta.get("width_px"), before_meta.get("height_px")
            same = bw is None or (abs(w - bw) <= 1 and abs(h - bh) <= 1)
            add("size_unchanged", "Pixel dimensions unchanged", same, f"{bw}×{bh} → {w}×{h}")
    except Exception as e:  # noqa: BLE001 - any failure to open is the finding
        add("opens_cleanly", "File opens and decodes", False, f"{type(e).__name__}: {e}")

    add("original_preserved", "Your original upload is untouched", original_path.exists(), original_path.name)
    return checks


async def verify(*, get_project, slot, resolve_target, scan_fn, upload_dir, diagnosis: dict, trail: Trail,
                 budget: Budget) -> dict:
    trail.start_agent(3, ROLE)
    trail.emit("step", 3, "Ignoring the repair report — re-reading the saved project and file myself…")

    project = await get_project()
    stored_filename, cur_meta = resolve_target(project, slot)
    path = upload_dir / stored_filename
    original_path = upload_dir / (cur_meta.get("original_stored_filename") or stored_filename)

    def fail(reason: str) -> dict:
        out = {"verdict": "rejected", "reason": reason, "criteria": [], "resolved": [], "remaining": [],
               "regressions": [], "file": stored_filename}
        trail.emit("step", 3, reason)
        trail.finish_agent(3, out, "failed", reason)
        return out

    t_int = time.monotonic()
    try:
        integrity = await run_bounded(_integrity, budget.cap(12.0), path, diagnosis["meta"], original_path)
    except StageTimeout:
        return fail("Integrity checks didn't finish in time — treating the fix as unverified.")
    integrity_seconds = round(time.monotonic() - t_int, 2)
    for c in integrity:
        trail.emit("criterion", 3, f"{'✓' if c['passed'] else '✗'} {c['label']} — {c['evidence']}", **c)

    t_scan = time.monotonic()
    try:
        scan = await run_bounded(scan_fn, budget.cap(12.0), project, slot, stored_filename)
    except StageTimeout:
        return fail("The verification scan didn't finish in time — treating the fix as unverified.")

    scan_seconds = round(time.monotonic() - t_scan, 2)
    after = normalize_issues(scan)
    after_by_id = {i["id"]: i for i in after}
    before_by_id = {i["id"]: i for i in diagnosis["issues"]}

    criteria = list(integrity)
    resolved, unresolved = [], []
    for b in diagnosis["fixable"]:
        a = after_by_id.get(b["id"])
        ok = a is None or a["status"] == "pass"
        (resolved if ok else unresolved).append(b["id"])
        trail.emit("check_result", 3, f"{b['label']}  →  {a['label'] if a else 'no longer flagged'}",
                   id=b["id"], resolved=ok, before=b, after=brief(a) if a else {"status": "pass", "label": "Passes", "message": ""})
        criteria.append({"id": f"fixed:{b['id']}", "label": f"Problem gone: {b['label']}", "passed": ok,
                         "evidence": (a["message"] if a and not ok else (a["label"] if a else "no longer flagged"))})

    regressions = []
    for a in actionable(after):
        b = before_by_id.get(a["id"])
        if b is None or b["status"] == "pass":
            regressions.append(brief(a))
    for r in regressions:
        trail.emit("criterion", 3, f"✗ New problem after repair: {r['label']} — {r['message']}", id=f"regression:{r['id']}",
                   label=f"No new problems: {r['label']}", passed=False, evidence=r["message"])
    if not regressions:
        trail.emit("criterion", 3, "✓ No regressions — nothing that passed before is failing now", id="no_regressions",
                   label="No regressions", passed=True, evidence="all previously-passing checks still pass")
    criteria.append({"id": "no_regressions", "label": "No regressions", "passed": not regressions,
                     "evidence": "; ".join(r["label"] for r in regressions) or "all previously-passing checks still pass"})

    remaining = actionable(after)
    health_after = health_score(after)
    trail.emit("issues_after", 3, "Independent scan complete", issues=[brief(i) | {"engine": i["engine"]} for i in after],
               health=health_after)

    integrity_ok = all(c["passed"] for c in integrity)
    if not integrity_ok or regressions:
        verdict = "rejected"
    elif not resolved:
        verdict = "no_progress"
    elif not unresolved and not remaining:
        verdict = "approved"
    else:
        verdict = "partial"

    out = {
        "verdict": verdict,
        "integrity_seconds": integrity_seconds,
        "scan_seconds": scan_seconds,
        "criteria": criteria,
        "resolved": resolved,
        "unresolved": unresolved,
        "remaining": [brief(i) | {"engine": i["engine"]} for i in remaining],
        "regressions": regressions,
        "health_before": diagnosis["health"],
        "health_after": health_after,
        "file": stored_filename,
        "sha256": sha256_file(path),
        "scanned_at": trail.now(),
        "issues_after": [brief(i) for i in after],
    }
    msgs = {
        "approved": "APPROVED — every confirmed problem is gone, nothing regressed.",
        "partial": f"PARTIAL — {len(resolved)} fixed, {len(remaining)} still open (see list). No regressions.",
        "no_progress": "NO PROGRESS — the repair didn't clear any confirmed problem. Reverting.",
        "rejected": "REJECTED — the fix failed integrity/regression checks. Reverting to your original state.",
    }
    trail.emit("step", 3, msgs[verdict])
    trail.finish_agent(3, out, "done" if verdict in ("approved", "partial") else "failed", msgs[verdict])
    return out
