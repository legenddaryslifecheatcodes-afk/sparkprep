"""Coordinator for the four-agent verified auto-fix.

Order of a verified fix (trey's order, 2026-11-24):
  1. Scanner 1 (the main scanner) scans the file            -- Agent 1
  2. Scanner 2 (independent_checker/, its own process, no database, no keys,
     nothing from Scanner 1) scans the ORIGINAL file on its own; only then are
     the two sets of findings compared. If they disagree, the file is flagged
     and NOT auto-fixed.
  3. Repair fixes (Agent 2); verification (Agent 3, plus Scanner 2's own
     check of the fixed file) and the Supervisor (Agent 4) check the work.
  4. The fixed file goes back through Scanner 1 for a final rescan. Only a
     clean final pass counts as fixed; otherwise the fix is undone.

The coordinator owns the things no single agent may own: the safety net
(a snapshot of the project + a backup of the file taken before the repair,
and the rollback if verification or the audit says no), the time budget, and
the one-run-at-a-time lock. It contains no repair logic and no verification
logic of its own -- it only sequences Agent 1 → 2 → 3 → 4.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .common import Budget, Trail, time_limits_on, run_bounded, StageTimeout, normalize_issues, actionable, brief
from .disagreements import settle
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
    # The independent checker (independent_checker/, its own code and process):
    # async (project, stored_filename) -> its JSON answer. Run on the file
    # before and after the repair; if it disagrees, the fix is rolled back.
    independent_fn: Optional[Callable[[dict, str], Awaitable[dict]]] = None
    ai_review: Optional[Callable[[dict], Awaitable[Optional[dict]]]] = None
    log: Optional[Callable[[str, Exception, dict], Awaitable[None]]] = None
    # Called with the issues a run could NOT fix -- the server keeps these as "unsolved cases" to study.
    on_unsolved: Optional[Callable[[list], Awaitable[None]]] = None
    # Whole-run ceiling. A live-streamed run keeps bytes flowing to the browser
    # the whole time (events + repair heartbeats), so Cloudflare's ~100s idle
    # cut-off doesn't apply and it can use most of that window; a single
    # buffered JSON response is bound by the API-wide 45s request limit.
    budget_s: float = 40.0
    # When the two scanners disagree (see disagreements.py):
    # tiebreak_fn(disputes) -> {check: {"verdict", "why"}}; record_disagreement(row) logs it for the admin view.
    tiebreak_fn: Optional[Callable[[list], Awaitable[dict]]] = None
    record_disagreement: Optional[Callable[[dict], Awaitable[None]]] = None
    picture_url: Optional[str] = None
    # Printer rules (independent checker ids) whose fix must not run this time,
    # because the scanners disagreed and it wasn't proven to be a problem.
    skip_checks: set = field(default_factory=set)


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


async def _independent_second_opinion(deps: Deps, diagnosis: dict, before: Optional[dict], verification: dict,
                                      trail: Trail) -> None:
    """Checker 3's second opinion from the independent checker (separate code,
    separate PDF reader, separate process). It must agree with the main
    scanner that every targeted problem is gone and that nothing it passed
    before now fails -- otherwise the verdict becomes "rejected" and run()
    rolls the repair back."""
    from independent_check import compare

    trail.emit("step", 3, "Asking the independent checker (separate code and process) for a second opinion…")
    project = await deps.get_project()
    stored_filename, _ = deps.resolve_target(project, deps.slot)
    after = await deps.independent_fn(project, stored_filename)
    result = compare(diagnosis.get("fixable") or [], before or {}, after)
    for t in result["targeted"]:
        trail.emit("criterion", 3, f"{'✓' if t['passed'] else '✗'} Independent check — {t['label']}: {t['evidence']}",
                   id=f"independent:{t['check']}", label=f"Independent check: {t['label']}", passed=t["passed"],
                   evidence=t["evidence"])
    for c in result["regressions"]:
        trail.emit("criterion", 3, f"✗ Independent check found a new problem — {c['label']}: {c['evidence']}",
                   id=f"independent_regression:{c['id']}", label=f"Independent check: {c['label']}", passed=False,
                   evidence=c["evidence"])
    if result["not_judged"]:
        trail.emit("step", 3, "Not something the independent checker measures (checked by the main scanner only): "
                   + ", ".join(result["not_judged"]))
    criterion = {"id": "independent_agrees", "label": "Independent checker agrees", "passed": result["agree"],
                 "evidence": result["reason"] or "every targeted problem is gone by its own check, nothing new failing"}
    trail.emit("criterion", 3, f"{'✓' if result['agree'] else '✗'} {criterion['label']} — {criterion['evidence']}", **criterion)
    verification["criteria"].append(criterion)
    verification["independent"] = {"before": before, "after": after, **result}
    if not result["agree"]:
        verification["verdict"] = "rejected"
        verification["reason"] = "The independent checker disagreed: " + (result["reason"] or "")
        trail.emit("step", 3, "REJECTED — the independent checker disagreed with the main scan. Reverting to your original state.")


FLAGGED_MESSAGE = ("Our second scanner couldn't confirm the scan of this file, so we didn't change anything. "
                   "Your file is exactly as you uploaded it. Please try again in a minute.")
NEEDS_CHECK_MESSAGE = ("Our two scanners disagreed about this file, so we didn't change anything. "
                       "Please check the items listed below. Your file is exactly as you uploaded it.")


async def _scanner2_first_look(deps: Deps, project: dict, diagnosis: dict, trail: Trail) -> tuple[dict, dict]:
    """Step 2: Scanner 2 scans the original file on its own, then its findings
    are compared with Scanner 1's. Returns (scanner 2's answer, comparison)."""
    from independent_check import cross_check

    trail.emit("step", 1, "Scanner 2 (separate program, no database, no keys) is scanning your original file on its own…")
    before = await deps.independent_fn(project, diagnosis["file"])
    result = cross_check(diagnosis.get("issues") or [], before)
    for c in result["compared"]:
        if c["agree"]:
            text = f"✓ Both scanners agree — {c['label']}: {'passes' if c['scanner1_passed'] else 'needs fixing'}"
        elif c["scanner1_passed"]:
            text = f"✗ Scanners disagree — {c['label']}: Scanner 2 found a problem Scanner 1 passed ({c['evidence']})"
        else:
            text = f"✗ Scanners disagree — {c['label']}: Scanner 1 found a problem Scanner 2 says is fine ({c['evidence']})"
        trail.emit("criterion", 1, text, id=f"scanners_agree:{c['check']}", label=f"Both scanners agree: {c['label']}",
                   passed=c["agree"], evidence=c["evidence"])
    if result["not_judged"]:
        trail.emit("step", 1, "Checked by Scanner 1 only (Scanner 2 doesn't measure these): "
                   + ", ".join(sorted(set(result["not_judged"]))))
    criterion = {"id": "scanners_agree", "label": "Scanner 2 confirms Scanner 1's findings", "passed": result["agree"],
                 "evidence": result["reason"] or "both scanners found the same problems"}
    trail.emit("criterion", 1, f"{'✓' if result['agree'] else '✗'} {criterion['label']} — {criterion['evidence']}", **criterion)
    if not result["agree"]:
        n = len(result["disagreements"])
        trail.emit("step", 1, f"The two scanners disagree on {n} item{'s' if n != 1 else ''}." if n else
                   "Scanner 2 could not confirm the scan.")
    return before, result


async def _scanners_step(deps: Deps, project: dict, diagnosis: dict, trail: Trail, budget: Budget) -> dict:
    """Step 2 plus the disagreement plan. Returns
    {"stop", "status", "message", "before", "checks", "record"}; when stop is
    False the repair goes ahead on the problems both scanners agree on."""
    from independent_check import cross_check, CATEGORY_FOR_ISSUE

    plan = {"stop": False, "status": None, "message": None, "before": None, "checks": [], "record": None}
    if not deps.independent_fn:
        return plan
    before, result = await _scanner2_first_look(deps, project, diagnosis, trail)
    plan["before"] = before
    if result["agree"]:
        return plan

    first = result
    # 1. Scan again once with both scanners -- a scan can glitch.
    trail.emit("step", 1, "The scanners disagree. Scanning again with both, in case one of them glitched…")
    issues = diagnosis.get("issues") or []
    try:
        rescan = await run_bounded(deps.scan_fn, budget.cap(15.0), project, deps.slot, diagnosis["file"])
        issues = [brief(i) | {"informational": i["informational"]} for i in normalize_issues(rescan)]
    except StageTimeout:
        trail.emit("step", 1, "Scanner 1's second scan didn't finish; using its first scan.")
    before = await deps.independent_fn(project, diagnosis["file"])
    plan["before"] = before
    result = cross_check(issues, before)
    record = {"slot": deps.slot, "file": diagnosis.get("file"), "first_scan": first, "second_scan": result,
              "resolved_by": None, "outcomes": []}
    plan["record"] = record

    if result["agree"]:
        trail.emit("criterion", 1, "✓ On the second scan both scanners agree — the first disagreement was a glitch",
                   id="scanners_agree_rescan", label="Scanners agree on the second scan", passed=True,
                   evidence=first["reason"] or "")
        record["resolved_by"] = "rescan"
        await _record(deps, record)
        return plan

    if not result["compared"]:
        # Scanner 2 couldn't run or open the file: there's nothing to vote on,
        # and "could not confirm" never counts as a pass.
        trail.emit("criterion", 1, f"✗ {result['reason']}", id="scanners_agree_rescan",
                   label="Scanner 2 confirms the scan", passed=False, evidence=result["reason"])
        record["resolved_by"] = "not_confirmed"
        await _record(deps, record)
        plan.update(stop=True, status="flagged", message=FLAGGED_MESSAGE)
        return plan

    # 3. Tiebreaker on each disputed item; the majority (2 of 3) wins.
    tb = {}
    if deps.tiebreak_fn:
        trail.emit("step", 1, "Still disagreeing. Asking a tiebreaker (Claude looks at the file and both scanners' evidence)…")
        try:
            tb = await asyncio.wait_for(deps.tiebreak_fn(result["disagreements"]), timeout=max(min(budget.remaining() - 25.0, 30.0), 5.0))
        except Exception as e:  # noqa: BLE001 - no tiebreaker answer = unsure, never a pass
            await _log(deps, "verified_autofix_tiebreak", e, {"slot": deps.slot})
            tb = {}
    else:
        trail.emit("step", 1, "No tiebreaker is available right now, so disputed items go to you to check.")
    settled = settle(result["disagreements"], tb or {})
    for o in settled["outcomes"]:
        who = {"scanner1": "Scanner 1 was right", "scanner2": "Scanner 2 was right", None: "couldn't decide"}[o["winner"]]
        trail.emit("criterion", 1, f"{'✓' if o['winner'] else '?'} Tiebreaker on {o['label']}: {who}"
                   + (f" — {o['tiebreak_why']}" if o.get("tiebreak_why") else ""),
                   id=f"tiebreak:{o['check']}", label=f"Tiebreaker: {o['label']}", passed=o["winner"] is not None,
                   evidence=o.get("tiebreak_why") or o["tiebreak"])
    # 4. Anything still unclear goes to the customer, in plain words, with a picture.
    for c in settled["checks"]:
        trail.emit("check_this", 1, c["message"], picture=deps.picture_url, check=c["check"], label=c["label"])
    plan["checks"] = settled["checks"]

    # 2. Fix only what's agreed (or proven by the majority).
    deps.skip_checks = set(settled["skip_checks"])
    skip_ids = {i["id"] for i in diagnosis.get("issues") or [] if CATEGORY_FOR_ISSUE.get(i["id"]) in deps.skip_checks}
    diagnosis["fixable"] = [f for f in diagnosis.get("fixable") or [] if f["id"] not in skip_ids]
    diagnosis["skipped_disputed"] = sorted(skip_ids)
    if skip_ids:
        trail.emit("step", 1, "Not fixing (not proven to be a problem): " + ", ".join(sorted(skip_ids)))

    record["resolved_by"] = "tiebreak"
    record["outcomes"] = settled["outcomes"]
    record["customer_checks"] = settled["checks"]
    await _record(deps, record)

    if not diagnosis["fixable"]:
        plan.update(stop=True, status="needs_check", message=NEEDS_CHECK_MESSAGE)
    else:
        n = len(diagnosis["fixable"])
        trail.emit("step", 1, f"Going ahead with the {n} problem{'s' if n != 1 else ''} both scanners agree on.")
    return plan


async def _record(deps: Deps, row: dict) -> None:
    """5. Every disagreement is kept for the admin view."""
    if deps.record_disagreement:
        try:
            await deps.record_disagreement(row)
        except Exception as e:  # noqa: BLE001 - logging must never break a repair
            await _log(deps, "scanner_disagreement_record", e, {"slot": deps.slot})


async def _scanner1_final_pass(deps: Deps, diagnosis: dict, verification: dict, trail: Trail, budget: Budget,
                               require_clean: bool) -> dict:
    """Step 4: the fixed file goes back through Scanner 1 one more time.
    Passes when nothing that was fine before now fails, and (for a full fix)
    every problem the repair targeted is gone."""
    trail.emit("step", 4, "Final check: sending the fixed file back through Scanner 1…")
    project = await deps.get_project()
    stored_filename, _ = deps.resolve_target(project, deps.slot)
    try:
        scan = await run_bounded(deps.scan_fn, budget.cap(15.0), project, deps.slot, stored_filename)
    except StageTimeout as e:
        out = {"passed": False, "reason": f"Scanner 1's final rescan didn't finish ({e})", "remaining": [], "new": []}
        trail.emit("criterion", 4, f"✗ Scanner 1 final rescan — {out['reason']}", id="scanner1_final_pass",
                   label="Scanner 1 final rescan passes", passed=False, evidence=out["reason"])
        return out
    now_failing = {i["id"]: i for i in actionable(normalize_issues(scan))}
    before_failing = {i["id"] for i in diagnosis.get("issues") or [] if i.get("status") != "pass" and not i.get("informational")}
    targeted = {i["id"] for i in diagnosis.get("fixable") or []}
    expected_open = {r["id"] if isinstance(r, dict) else r for r in verification.get("remaining") or []}
    new = sorted(i for i in now_failing if i not in before_failing)
    still = sorted(i for i in now_failing if i in targeted and (require_clean or i not in expected_open))
    passed = not new and not still
    if passed:
        evidence = "no new problems" + (", every targeted problem is gone" if require_clean else "")
    else:
        bits = []
        if still:
            bits.append("still failing: " + ", ".join(now_failing[i]["label"] or i for i in still))
        if new:
            bits.append("new problem: " + ", ".join(now_failing[i]["label"] or i for i in new))
        evidence = "; ".join(bits)
    trail.emit("criterion", 4, f"{'✓' if passed else '✗'} Scanner 1 final rescan — {evidence}", id="scanner1_final_pass",
               label="Scanner 1 final rescan passes", passed=passed, evidence=evidence)
    return {"passed": passed, "reason": None if passed else evidence, "remaining": still, "new": new}


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


REPAIR_LOG_MAX_ENTRIES = 200  # generous ceiling; a real project runs Repair Bay a handful of times, not hundreds


async def _append_repair_log(deps: Deps, diagnosis: dict, pipeline: dict) -> None:
    """Keeps a running, plain-language history of what Repair Bay actually found
    and fixed on this project over its whole lifetime -- the raw material for the
    "found & fixed" report bundled with every export (see report_export.py). Without
    this, that detail only ever existed for the length of one HTTP response: the
    project record kept the current pass/fail state (_sync_stale_compliance above)
    but never a record of what was WRONG and what was DONE about it.

    Only logs a run that actually found something to report -- a scan that came
    back clean isn't part of the story of what got fixed."""
    found = [i for i in (diagnosis.get("issues") or []) if i.get("status") != "pass" and not i.get("informational")]
    if not found:
        return
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "slot": deps.slot,
        "status": pipeline["status"],
        "found": [{"id": i["id"], "label": i["label"], "message": i["message"]} for i in found],
        "resolved": pipeline["resolved"],
        # Agent 3 reports remaining issues as objects, resolved ones as ids -- store ids for both.
        "remaining": [r["id"] if isinstance(r, dict) else r for r in pipeline["remaining"]],
        "health_before": pipeline["health_before"],
        "health_after": pipeline["health_after"],
    }
    project = await deps.get_project()
    log = list(project.get("repair_log") or [])
    log.append(entry)
    if len(log) > REPAIR_LOG_MAX_ENTRIES:
        log = log[-REPAIR_LOG_MAX_ENTRIES:]
    await deps.save_fields({"repair_log": log}, [])
    if entry["remaining"] and deps.on_unsolved:
        by_id = {f["id"]: f for f in entry["found"]}
        try:
            await deps.on_unsolved([by_id.get(r, {"id": r, "label": r, "message": ""}) for r in entry["remaining"]])
        except Exception as e:                      # recording a case must never break a repair run
            if deps.log:
                await deps.log("unsolved_case_record", e, {"slot": deps.slot})


async def _run(deps: Deps, sink) -> dict:
    budget, trail = Budget(deps.budget_s), Trail(sink)
    trail.emit("pipeline_start", 0, "Verified auto-fix started", slot=deps.slot)
    snap = None
    repair_out = verification = supervision = None
    status, message = "error", "Something unexpected went wrong. Your file was not changed."
    project = await deps.get_project()
    diagnosis = {}
    plan: dict = {}

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
        elif (plan := await _scanners_step(deps, project, diagnosis, trail, budget))["stop"]:
            # Step 2: the scanners disagree and nothing is left that both agree
            # on (or Scanner 2 couldn't confirm) -- nothing is touched.
            status, message = plan["status"], plan["message"]
        else:
            snap = await _take_snapshot(deps, project, diagnosis["file"])
            indep_before = plan["before"]
            repair_out = await repair(repair_fn=deps.repair_fn, diagnosis=diagnosis, trail=trail,
                                      timeout_s=(max(budget.remaining() - 20.0, 5.0) if time_limits_on() else budget.remaining()))  # limits on: leave room for Agents 3 + 4
            if repair_out["status"] != "applied":
                await _rollback(deps, snap, trail, "the repair engine didn't complete")
                status, message = "repair_failed", f"The repair couldn't be completed ({repair_out['error']}). Your file is unchanged."
            else:
                verification = await verify(get_project=deps.get_project, slot=deps.slot, resolve_target=deps.resolve_target,
                                            scan_fn=deps.scan_fn, upload_dir=deps.upload_dir, diagnosis=diagnosis,
                                            trail=trail, budget=budget)
                if deps.independent_fn and verification["verdict"] not in ("rejected", "no_progress"):
                    await _independent_second_opinion(deps, diagnosis, indep_before, verification, trail)
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
                    else:
                        final = await _scanner1_final_pass(deps, diagnosis, verification, trail, budget,
                                                           require_clean=supervision["decision"] == "confirmed")
                        verification["scanner1_final"] = final
                        if not final["passed"]:
                            await _rollback(deps, snap, trail, "Scanner 1's final rescan did not pass")
                            status, message = "rolled_back", ("Scanner 1's final rescan didn't pass, so the repair was undone. "
                                                              "Your file is exactly as it was before.")
                            await _log(deps, "verified_autofix_final_rescan_failed", RuntimeError(final["reason"] or "final rescan"),
                                       {"slot": deps.slot})
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
    checks = plan.get("checks") or []
    if checks and status in ("confirmed", "partial"):
        message += (f" Please also check the {len(checks)} item{'s' if len(checks) != 1 else ''} "
                    "our two scanners couldn't agree on (listed below).")

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
        "scanners_disagree": (plan.get("record") or {}).get("outcomes") or None,
        "customer_checks": checks,
        "picture_url": deps.picture_url if checks else None,
        "seconds": round(budget.elapsed(), 1),
        "timings": timings,
    }
    await _append_repair_log(deps, diagnosis, pipeline)
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
