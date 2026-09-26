"""Coordinator for the four-agent verified auto-fix.

The coordinator owns the things no single agent may own: the safety net
(a snapshot of the project + a backup of the file taken before the repair,
and the rollback if verification or the audit says no), the time budget, and
the one-run-at-a-time lock. It contains no repair logic and no verification
logic of its own -- it only sequences Agent 1 → 2 → 3 → 4.
"""
from __future__ import annotations

import copy
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .common import Budget, Trail, time_limits_on
from .agent1_triage import diagnose
from .agent2_repair import repair
from .agent3_verify import verify
from .agent4_supervisor import supervise

PROMPT_TEXT = "Fixed — press Enter for rescan and system confirmation"
_STATE_KEYS = ("uploaded_file", "file_metadata", "compliance", "slots", "updated_at")
_RUNNING: set[str] = set()
_LOG = logging.getLogger("sparkprep.autofix")


@dataclass
class Deps:
    project_id: str
    slot: Optional[str]
    user_id: str
    upload_dir: Path
    get_project: Callable[[], Awaitable[dict]]
    save_fields: Callable[[dict, list], Awaitable[None]]      # (fields to set, keys to unset)
    resolve_target: Callable[[dict, Optional[str]], tuple]    # -> (stored_filename, metadata)
    scan_fn: Callable                                         # sync; used only by Agents 1 and 3
    repair_fn: Callable[[], Awaitable[dict]]                  # the existing engine; used only by Agent 2
    ai_review: Optional[Callable[[dict], Awaitable[Optional[dict]]]] = None
    log: Optional[Callable[[str, Exception, dict], Awaitable[None]]] = None
    # Whole-run ceiling. A live-streamed run keeps bytes flowing to the browser
    # the whole time (events + repair heartbeats), so Cloudflare's ~100s idle
    # cut-off doesn't apply and it can use most of that window; a single
    # buffered JSON response is bound by the API-wide 45s request limit.
    budget_s: float = 40.0


def try_claim(project_id: str, slot: Optional[str]) -> bool:
    """One verified run per file at a time (a double-click must not start two
    repairs racing on the same file). run() releases the claim."""
    key = f"{project_id}:{slot or ''}"
    if key in _RUNNING:
        return False
    _RUNNING.add(key)
    return True


async def run(deps: Deps, sink: Callable[[dict], Any]) -> dict:
    """Caller must have won try_claim() first."""
    try:
        return await _run(deps, sink)
    finally:
        _RUNNING.discard(f"{deps.project_id}:{deps.slot or ''}")


async def _take_snapshot(deps: Deps, project: dict, stored_filename: str) -> dict:
    src = deps.upload_dir / stored_filename
    backup = deps.upload_dir / f".bak_{uuid.uuid4().hex[:8]}_{stored_filename}"
    try:
        os.link(src, backup)          # instant, no extra disk
    except OSError:
        shutil.copy2(src, backup)
    same_target = {deps.slot or "full_wrap"}
    return {
        "fields": {k: copy.deepcopy(project[k]) for k in _STATE_KEYS if k in project},
        "absent": [k for k in _STATE_KEYS if k not in project],
        "stored_filename": stored_filename,
        "backup": backup,
        "before_files": {n for n in os.listdir(deps.upload_dir) if n.startswith(deps.project_id)},
        "other_slots": {n: d["stored_filename"] for n, d in (project.get("slots") or {}).items()
                        if n not in same_target and d.get("stored_filename")},
        "user_id": project.get("user_id"),
    }


async def _rollback(deps: Deps, snap: dict, trail: Trail, reason: str):
    trail.emit("rollback", 0, f"Reverting to the state before the repair — {reason}")
    path = deps.upload_dir / snap["stored_filename"]
    if not path.exists() and snap["backup"].exists():
        shutil.copy2(snap["backup"], path)
    await deps.save_fields(snap["fields"], snap["absent"])
    for name in os.listdir(deps.upload_dir):
        if name.startswith(deps.project_id) and name not in snap["before_files"]:
            try:
                os.remove(deps.upload_dir / name)
            except OSError:
                pass
    trail.emit("step", 0, "Original state restored. Nothing of yours was lost.")


def _discard_backup(snap: Optional[dict]):
    if snap:
        try:
            os.remove(snap["backup"])
        except OSError:
            pass


async def _current_state(deps: Deps) -> tuple[dict, list]:
    project = await deps.get_project()
    if deps.slot:
        data = (project.get("slots") or {}).get(deps.slot) or {}
        return {k: v for k, v in data.items() if k != "compliance"}, data.get("compliance") or []
    return project.get("file_metadata") or {}, project.get("compliance") or []


async def _sync_stale_compliance(deps: Deps, project: dict, diagnosis: dict) -> None:
    """Writes Agent 1's fresh compliance findings back to the project so the
    UI reflects what Repair Bay just proved is actually true right now,
    instead of leaving whatever was stored before this run in place
    indefinitely (see the call site's comment for why that matters)."""
    fresh = diagnosis.get("issues") or []
    if deps.slot:
        slots = copy.deepcopy(project.get("slots") or {})
        slot_data = dict(slots.get(deps.slot) or {})
        slot_data["compliance"] = fresh
        slots[deps.slot] = slot_data
        fields = {"slots": slots}
        if deps.slot == "full_wrap":
            # Mirrors _replace_slot()'s own legacy mirroring in server.py --
            # some older UI paths still read project-level compliance instead
            # of slots.full_wrap for a full_wrap cover.
            fields["compliance"] = fresh
    else:
        fields = {"compliance": fresh}
    await deps.save_fields(fields, [])


async def _run(deps: Deps, sink) -> dict:
    budget, trail = Budget(deps.budget_s), Trail(sink)
    trail.emit("pipeline_start", 0, "Verified auto-fix started", slot=deps.slot)
    snap = None
    repair_out = verification = supervision = None
    status, message = "error", "Something unexpected went wrong. Your file was not changed."
    project = await deps.get_project()
    diagnosis = {}

    try:
        diagnosis = await diagnose(project=project, slot=deps.slot, resolve_target=deps.resolve_target,
                                   scan_fn=deps.scan_fn, upload_dir=deps.upload_dir, trail=trail, budget=budget)
        if diagnosis["outcome"] != "proceed":
            status = "no_action"
            message = {
                "not_reproduced": "Couldn't reproduce the reported problem — the file already passes. Nothing was changed.",
                "nothing_to_fix": "This file already passes every check. Nothing needed fixing.",
                "other_tool_only": "Nothing here for the auto-fix engine — the remaining issue needs AI Upscale.",
                "manual_only": "The remaining issues can't be fixed automatically — see the fix steps on each check.",
                "scan_failed": "The scan didn't finish in time, so nothing was changed. Please try again.",
            }.get(diagnosis["outcome"], "Nothing to repair.")
            # Agent 1's scan just ran fresh against the file as it is right now --
            # more current than whatever compliance is sitting in the project
            # record (which could be stale from before an earlier repair, a
            # scanner fix that changed a verdict, or simply time passing). If
            # nothing gets written here, a customer can see Repair Bay say
            # "nothing to fix" while the project page still shows the old
            # failure forever, since nothing else ever re-triggers a save for
            # this outcome -- exactly the "app says one thing, Auto-Fix says
            # another" disconnect this pipeline exists to prevent.
            if diagnosis["outcome"] != "scan_failed":
                await _sync_stale_compliance(deps, project, diagnosis)
        else:
            snap = await _take_snapshot(deps, project, diagnosis["file"])
            repair_out = await repair(repair_fn=deps.repair_fn, diagnosis=diagnosis, trail=trail,
                                      timeout_s=(max(budget.remaining() - 20.0, 5.0) if time_limits_on() else budget.remaining()))  # limits on: leave room for Agents 3 + 4
            if repair_out["status"] != "applied":
                await _rollback(deps, snap, trail, "the repair engine didn't complete")
                status, message = "repair_failed", f"The repair couldn't be completed ({repair_out['error']}). Your file is unchanged."
            else:
                verification = await verify(get_project=deps.get_project, slot=deps.slot, resolve_target=deps.resolve_target,
                                            scan_fn=deps.scan_fn, upload_dir=deps.upload_dir, diagnosis=diagnosis,
                                            trail=trail, budget=budget)
                if verification["verdict"] in ("rejected", "no_progress"):
                    await _rollback(deps, snap, trail, "independent verification did not pass")
                    if verification["verdict"] == "no_progress":
                        status, message = "no_progress", "The repair didn't clear the problem, so your file was left exactly as it was."
                    else:
                        status, message = "rolled_back", "The repair failed verification, so it was undone. Your file is exactly as it was before."
                    await _log(deps, "verified_autofix_" + verification["verdict"], RuntimeError(verification.get("reason") or verification["verdict"]),
                               {"criteria": [c for c in verification.get("criteria", []) if not c["passed"]]})
                else:
                    supervision = await supervise(
                        get_project=deps.get_project, slot=deps.slot, resolve_target=deps.resolve_target,
                        upload_dir=deps.upload_dir, snapshot=snap, diagnosis=diagnosis, repair=repair_out,
                        verification=verification, trail=trail, budget=budget, ai_review=deps.ai_review)
                    if supervision["decision"] == "rejected":
                        await _rollback(deps, snap, trail, "the supervisor's audit failed")
                        status, message = "rolled_back", "The supervisor's audit found a problem, so the repair was undone. Your file is unchanged."
                        await _log(deps, "verified_autofix_audit_rejected", RuntimeError("audit rejected"),
                                   {"criteria": [c for c in supervision["criteria"] if not c["passed"]]})
                    elif supervision["decision"] == "confirmed":
                        status, message = "confirmed", PROMPT_TEXT
                    else:
                        n = len(verification["remaining"])
                        status = "partial"
                        message = (f"Improved — {len(verification['resolved'])} fixed, {n} still open. "
                                   "Details are listed below; nothing regressed.")
    except Exception as e:  # noqa: BLE001 - last line of defence: never leave a half-applied repair behind
        await _log(deps, "verified_autofix_crash", e, {"slot": deps.slot})
        if snap:
            try:
                await _rollback(deps, snap, trail, "an unexpected error occurred")
            except Exception as rb:  # noqa: BLE001
                await _log(deps, "verified_autofix_rollback_failed", rb, {"slot": deps.slot})
        status, message = "error", "Something unexpected went wrong; the repair was undone. Please try again."
    finally:
        _discard_backup(snap)

    if status == "confirmed":
        trail.emit("prompt", 4, PROMPT_TEXT)

    metadata, compliance = await _current_state(deps)
    timings = _stage_timings(trail, diagnosis, verification, budget)
    _LOG.info("verified_autofix status=%s slot=%s timings=%s", status, deps.slot, timings)
    pipeline = {
        "status": status,
        "message": message,
        "prompt": PROMPT_TEXT if status == "confirmed" else None,
        "health_before": diagnosis.get("health"),
        "health_after": (verification or {}).get("health_after") if status in ("confirmed", "partial") else diagnosis.get("health"),
        "resolved": (verification or {}).get("resolved", []) if status in ("confirmed", "partial") else [],
        "remaining": (verification or {}).get("remaining", []) if status in ("confirmed", "partial") else [],
        "seconds": round(budget.elapsed(), 1),
        "timings": timings,
    }
    payload = {
        "slot": deps.slot, "file_metadata": metadata, "compliance": compliance,
        "ghostscript_fix": (repair_out or {}).get("ghostscript_fix") if status in ("confirmed", "partial") else None,
        "interior_margin_fix": (repair_out or {}).get("interior_margin_fix") if status in ("confirmed", "partial") else None,
        "check_type": "basic", "pipeline": pipeline,
    }
    trail.emit("result", 0, message, **payload)
    return {**payload, "audit_trail": trail.events}


def _stage_timings(trail: Trail, diagnosis: dict, verification, budget: Budget) -> dict:
    """Seconds spent per stage, from timestamps the run already records (costs nothing).
    triage = Agent 1 (of which scan = issue detection), repair = Agent 2 (the unchanged engine,
    incl. its CMYK conversion etc.), verify = Agent 3, audit = Agent 4."""
    def dur(agent):
        r = trail.records.get(agent)
        return round(r["finished"] - r["started"], 2) if r and r.get("finished") is not None else None
    v = verification or {}
    return {
        "triage": dur(1), "triage_scan": (diagnosis or {}).get("scan_seconds"),
        "repair": dur(2),
        "verify": dur(3), "verify_integrity": v.get("integrity_seconds"), "verify_scan": v.get("scan_seconds"),
        "audit": dur(4),
        "total": round(budget.elapsed(), 2),
    }


async def _log(deps: Deps, stage: str, exc: Exception, context: dict):
    if deps.log:
        try:
            await deps.log(stage, exc, context)
        except Exception:  # noqa: BLE001
            pass
