"""Agent 1 -- Diagnostic & Triage (fast, programmatic, no AI).

Independently re-runs the scan on the file as it exists on disk right now
and compares it with what the project last reported. Its only job is to
answer: "is the reported problem real, and can the repair engine fix it?"
If not, the pipeline stops here and nothing gets touched.
"""
from __future__ import annotations

import time

from .common import (
    Trail, Budget, run_bounded, StageTimeout, normalize_issues, actionable, health_score, brief, sha256_file,
)

ROLE = "Re-running the scan to confirm the reported problem is real"


def _reported_ids(project: dict, slot):
    """Ids of the problems the customer was last shown for this file."""
    if slot:
        stored = ((project.get("slots") or {}).get(slot) or {}).get("compliance")
    else:
        stored = project.get("compliance")
    return [c.get("id") for c in (stored or []) if c.get("status") != "pass"]


async def diagnose(*, project: dict, slot, resolve_target, scan_fn, upload_dir, trail: Trail, budget: Budget) -> dict:
    trail.start_agent(1, ROLE)
    stored_filename, _current_meta = resolve_target(project, slot)
    trail.emit("step", 1, "Locating the file and re-running the print-readiness scan…")

    scan_t0 = time.monotonic()
    try:
        scan = await run_bounded(scan_fn, budget.cap(15.0), project, slot, stored_filename)
    except StageTimeout as e:
        out = {"outcome": "scan_failed", "reason": str(e)}
        trail.finish_agent(1, out, "failed", "Scan did not finish in time")
        return out

    issues = normalize_issues(scan)
    real = actionable(issues)
    fixable = [i for i in real if i["engine"]]
    other_tool = [i for i in real if i["auto_fix"] and not i["engine"]]
    manual = [i for i in real if not i["auto_fix"]]

    reported = _reported_ids(project, slot)
    reported_set = set(reported)
    confirmed = [i for i in real if i["id"] in reported_set]
    new = [i for i in real if i["id"] not in reported_set]
    fresh_ids = {i["id"] for i in real}
    cleared = [rid for rid in reported if rid not in fresh_ids and rid not in {"bleed", "pdfx1a", "pdf_dpi"}]

    meta = scan["metadata"]
    health = health_score(issues)
    out = {
        "outcome": "proceed",
        "scan_seconds": round(time.monotonic() - scan_t0, 2),
        "file": stored_filename,
        "sha256": sha256_file(upload_dir / stored_filename),
        "meta": {k: meta.get(k) for k in ("width_px", "height_px", "pdf_pages", "color_mode", "dpi_x", "is_pdf", "format")},
        "issues": [brief(i) | {"engine": i["engine"], "auto_fix": i["auto_fix"], "fix_action": i["fix_action"],
                                "informational": i["informational"]} for i in issues],
        "health": health,
        "reported": reported,
        "confirmed": [brief(i) for i in confirmed],
        "new": [brief(i) for i in new],
        "cleared_since_report": cleared,
        "fixable": [brief(i) | {"fix_action": i["fix_action"]} for i in fixable],
        "other_tool": [brief(i) for i in other_tool],
        "manual": [brief(i) for i in manual],
    }

    # Show the problems on screen *before* anything is touched -- this is the
    # "here is the broken thing" evidence the fix is later measured against.
    trail.emit("issues_before", 1, "Scan complete", issues=out["issues"], health=health)
    for i in real:
        trail.emit("finding", 1, f"{i['label']} — {i['message']}", id=i["id"], status=i["status"], engine=i["engine"])

    if fixable:
        n = len(fixable)
        trail.emit("step", 1, f"Reproduced: {n} problem{'s' if n != 1 else ''} confirmed that the repair engine can fix.")
        trail.finish_agent(1, out, "done", "Problem reproduced — handing to Agent 2")
        return out

    if not real:
        out["outcome"] = "not_reproduced" if reported else "nothing_to_fix"
        msg = ("Couldn't reproduce the reported problem — the file already passes every check. Nothing was changed."
               if reported else "This file already passes every check. Nothing needed fixing.")
    elif other_tool and not manual:
        out["outcome"] = "other_tool_only"
        msg = "Nothing the auto-fix engine can repair here — the remaining issue needs AI Upscale (resolution)."
    else:
        out["outcome"] = "manual_only"
        msg = "The remaining issues can't be fixed automatically — they need a manual fix in your design tool."
    trail.emit("step", 1, msg)
    trail.finish_agent(1, out, "exited", msg)
    return out
