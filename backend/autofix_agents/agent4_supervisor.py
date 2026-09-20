"""Agent 4 -- Supervisor & Overseer.

Sits outside the repair loop as an external auditor. It doesn't repair and
it doesn't re-run Agent 3's scan; it audits the *record* -- that the other
three agents each did their own job, in order, without leaning on each
other -- and that the system as a whole is still sound before anyone is told
the file is fixed.

The deterministic checks are the gate. An optional AI reviewer (Claude, if
an API key is configured) reads the same audit trail and adds a plain-
English assessment plus any concerns; it is advisory and can never approve
something the checks rejected, nor roll back a fix the checks confirmed.
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

from .common import Trail, Budget, sha256_file, time_limits_on

ROLE = "Auditing the full trail from Agents 1–3 and the system as a whole"


async def supervise(*, get_project, slot, resolve_target, upload_dir, snapshot: dict, diagnosis: dict, repair: dict,
                    verification: dict, trail: Trail, budget: Budget,
                    ai_review: Optional[Callable[[dict], Awaitable[Optional[dict]]]] = None) -> dict:
    trail.start_agent(4, ROLE)
    criteria: list[dict] = []

    def add(id_, label, passed, evidence):
        c = {"id": id_, "label": label, "passed": bool(passed), "evidence": evidence}
        criteria.append(c)
        trail.emit("criterion", 4, f"{'✓' if passed else '✗'} {label} — {evidence}", **c)

    recs = trail.records
    r1, r2, r3 = recs.get(1), recs.get(2), recs.get(3)

    # 1. The three agents each ran, in order, and finished.
    ordered = bool(
        r1 and r2 and r3 and None not in (r1["finished"], r2["finished"], r3["finished"])
        and r1["finished"] <= r2["started"] <= r2["finished"] <= r3["started"]
    )
    add("trail_complete", "Agents 1 → 2 → 3 all ran, in order", ordered, "audit trail is complete" if ordered else "missing or out-of-order stage")

    # 2. Separation of duties: the repairer supplied no verification data,
    # and verification happened strictly after the repair finished.
    self_graded = any(k in repair for k in ("compliance", "issues", "verdict", "issues_after"))
    add("no_self_grading", "Repair agent did not grade its own work", not self_graded,
        "repair report contains no verification data" if not self_graded else "repair report carried its own verification")
    after_repair = bool(r2 and r2["finished"] is not None and verification.get("scanned_at") is not None
                        and verification["scanned_at"] >= r2["finished"])
    add("independent_verification", "Verification ran independently, after the repair", after_repair,
        f"verification scan at t={verification.get('scanned_at')}s, repair finished t={r2 and r2['finished']}s")

    # 3. What Agent 3 approved is what is actually saved.
    project = await get_project()
    stored_filename, cur_meta = resolve_target(project, slot)
    same_file = stored_filename == verification.get("file")
    current_hash = sha256_file(upload_dir / stored_filename)
    unchanged = current_hash is not None and current_hash == verification.get("sha256")
    add("verified_file_is_saved_file", "The file Agent 3 verified is the file that's saved",
        same_file and unchanged, f"{stored_filename} (fingerprint {'matches' if unchanged else 'DIFFERS'})")

    # 4. System-wide integrity.
    original = upload_dir / (cur_meta.get("original_stored_filename") or stored_filename)
    add("original_safe", "Customer's original upload is intact", original.exists(), original.name)
    other_ok, other_note = True, "no other files touched"
    for other_slot, fname in snapshot["other_slots"].items():
        now_slots = project.get("slots") or {}
        now_name = (now_slots.get(other_slot) or {}).get("stored_filename")
        if now_name != fname or not (upload_dir / fname).exists():
            other_ok, other_note = False, f"slot '{other_slot}' changed unexpectedly"
    add("other_files_untouched", "The project's other files are untouched", other_ok, other_note)
    add("same_owner", "Project ownership unchanged", project.get("user_id") == snapshot["user_id"], "owner matches")

    # 5. Time budget.
    if time_limits_on():
        add("within_time_budget", f"Finished inside the {int(budget.total)}s limit", budget.elapsed() <= budget.total,
            f"{budget.elapsed():.1f}s used")
    else:
        add("within_time_budget", "No time limit in effect (correct beats fast)", True, f"took {budget.elapsed():.1f}s")

    # 6. Acceptance criteria.
    v = verification.get("verdict")
    add("acceptance", "Agent 3 approved with every problem cleared", v == "approved",
        {"approved": "all confirmed problems resolved, no regressions", "partial": "some problems remain open"}.get(v, f"verdict was {v}"))

    integrity_ok = all(c["passed"] for c in criteria if c["id"] != "acceptance")
    if not integrity_ok:
        decision = "rejected"
    elif v == "approved":
        decision = "confirmed"
    else:
        decision = "partial"

    # Optional advisory AI review of the same trail.
    review = None
    if ai_review and (budget.remaining() > 12 or not time_limits_on()):
        trail.emit("step", 4, "Asking the AI reviewer to read the audit trail…")
        try:
            review = await asyncio.wait_for(ai_review({
                "diagnosis": {k: diagnosis.get(k) for k in ("outcome", "confirmed", "new", "health")},
                "repair": repair,
                "verification": {k: verification.get(k) for k in ("verdict", "criteria", "resolved", "remaining", "regressions",
                                                                     "health_before", "health_after")},
                "supervisor_criteria": criteria,
                "decision": decision,
                "seconds": round(budget.elapsed(), 1),
            }), timeout=9.0)
        except Exception:  # noqa: BLE001 - advisory only, never fatal
            review = None
        if review:
            trail.emit("ai_review", 4, review.get("summary", ""), concerns=review.get("concerns", []))

    msgs = {
        "confirmed": "CONFIRMED — audit passed. Fix is real and the system is intact.",
        "partial": "PARTIAL — no integrity problems, but not every issue is cleared.",
        "rejected": "REJECTED — audit found a problem. Reverting to the pre-fix state.",
    }
    out = {"decision": decision, "criteria": criteria, "ai_review": review}
    trail.emit("step", 4, msgs[decision])
    trail.finish_agent(4, out, "done" if decision != "rejected" else "failed", msgs[decision])
    return out


async def anthropic_review(summary: dict, *, api_key: str, model: str, timeout_s: float = 8.0) -> Optional[dict]:
    """Advisory AI read of the audit trail. Returns {"summary","concerns"} or
    None on any failure (missing key, timeout, bad output) -- callers treat
    None as "no AI opinion", never as a problem."""
    import httpx

    prompt = (
        "You are the independent supervisor of an automated print-file repair pipeline. Below is the audit trail: "
        "what was diagnosed (Agent 1), what the repair engine reported (Agent 2), what an independent verifier found "
        "(Agent 3), and the supervisor's own deterministic checks. In at most two plain-English sentences for a "
        "non-technical author, say what was fixed and whether anything is still open. Then list any concrete "
        "concerns about the audit trail itself (contradictions, suspicious gaps) or an empty list. Reply with ONLY "
        'JSON: {"summary": "...", "concerns": ["..."]}\n\n' + json.dumps(summary, default=str)[:12000]
    )
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]},
        )
    if r.status_code != 200:
        return None
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    parsed = json.loads(text[start:end + 1])
    return {"summary": str(parsed.get("summary", ""))[:600], "concerns": [str(c)[:300] for c in (parsed.get("concerns") or [])][:5]}
