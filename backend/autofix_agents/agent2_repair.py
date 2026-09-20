"""Agent 2 -- Repair Specialist.

Runs the existing repair engine (server.py's autofix(), unchanged) against
the problems Agent 1 confirmed and reports what the engine says it did.

HARD RULE: this module must never verify its own work. It deliberately
receives NO scanner, no compliance checker and no file-inspection helpers --
only the repair callable and Agent 1's diagnosis -- and it strips the
engine's own self-check (`compliance`) out of what it hands on, so Agents 3
and 4 can't accidentally lean on the repair engine's grading of itself.
tests/test_autofix_agents.py enforces this structurally.
"""
from __future__ import annotations

import asyncio
import time

from .common import Trail, FIX_ACTION_LABELS, FIX_ACTION_PROGRESS

ROLE = "Applying the repair to the confirmed problems"

# Fields of the engine's response that describe what it *did*. Everything
# else (notably `compliance`, the engine's own re-check) is dropped.
_KEEP = ("ghostscript_fix", "interior_margin_fix")


async def repair(*, repair_fn, diagnosis: dict, trail: Trail, timeout_s: float) -> dict:
    trail.start_agent(2, ROLE)

    # Plain-language plan derived from what Agent 1 confirmed -- the customer
    # sees what is about to happen and why.
    plan, seen = [], set()
    for issue in diagnosis["fixable"]:
        label = FIX_ACTION_LABELS.get(issue["fix_action"], issue["fix_action"])
        if label not in seen:
            seen.add(label)
            plan.append({"label": label, "doing": FIX_ACTION_PROGRESS.get(issue["fix_action"], label + "…"),
                         "because": issue["label"]})
    trail.emit("repair_plan", 2, "Repair plan", plan=plan)
    for step in plan:
        trail.emit("step", 2, f"Will: {step['label']}  (fixes: {step['because']})")

    started = time.monotonic()
    task = asyncio.ensure_future(repair_fn())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=1.5)
            if done:
                break
            elapsed = time.monotonic() - started
            trail.emit("heartbeat", 2, "Repair in progress…", elapsed=round(elapsed, 1))
            if elapsed > timeout_s:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                out = {"status": "failed", "error": f"The repair engine didn't finish within {int(timeout_s)} seconds."}
                trail.finish_agent(2, out, "failed", out["error"])
                return out
        engine_response = task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise
    except Exception as e:  # HTTPException from the engine, or anything unexpected
        detail = getattr(e, "detail", None) or str(e)
        out = {"status": "failed", "error": str(detail)}
        trail.finish_agent(2, out, "failed", f"Repair engine reported an error: {detail}")
        return out

    out = {
        "status": "applied",
        "seconds": round(time.monotonic() - started, 1),
        **{k: engine_response.get(k) for k in _KEEP},
    }
    for label in ("interior_margin_fix", "ghostscript_fix"):
        r = out.get(label)
        if r and r.get("attempted") and not r.get("succeeded"):
            trail.emit("step", 2, f"Engine note: {r.get('reason') or label + ' did not fully succeed'}")
    trail.emit("step", 2, f"Patch applied in {out['seconds']}s. Handing over for independent verification.")
    trail.finish_agent(2, out, "done", "Repair applied — not self-verified; passing to Agent 3")
    return out
