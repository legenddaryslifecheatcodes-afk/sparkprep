"""Shared plumbing for the four-agent verified auto-fix pipeline.

The repair engine itself (server.py's autofix()) is untouched -- everything
in this package is the *outside* layer around it: something checks the
problem is real before the engine runs (Agent 1), something else checks the
engine's work independently afterwards (Agent 3), and an auditor reviews the
whole trail before the customer is told anything is fixed (Agent 4).

Nothing here imports server.py; every server-side capability (database,
scanner, the repair engine itself) is handed in as a callable so each agent
only ever sees what it is allowed to see.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

# ---- Time limits: ONE switch, currently OFF ----
# SPARKPREP_TIME_LIMITS=on restores the original launch-week guard rails (45s
# request ceiling, tight per-agent caps). Anything else (the default) lifts
# them: a file is allowed to take as long as it takes -- correct beats fast.
# A very large safety ceiling remains only so a genuinely wedged job can't
# hold a worker forever.
def time_limits_on() -> bool:
    return os.environ.get("SPARKPREP_TIME_LIMITS", "off").strip().lower() in ("on", "1", "true", "yes")


NO_LIMIT_CEILING_S = 1800.0      # 30 min "practically unlimited" backstop when limits are lifted
PIPELINE_BUDGET_S = 40.0         # only used when limits are ON

AGENT_NAMES = {
    0: "Pipeline",
    1: "Diagnostic & Triage",
    2: "Repair Specialist",
    3: "Verification & Regression",
    4: "Supervisor",
}

# Checks that are informational by design: "bleed" and "pdfx1a" are always
# reported as warnings ("will be added automatically on export"), never
# something the repair engine resolves, so they must not count as
# "reproducible errors" or hold the health score down forever.
INFORMATIONAL_IDS = {
    "bleed", "pdfx1a", "pdf_dpi", "pdfx1a_not_declared", "pdfx1a_missing_output_intent",
}

# Structure-audit findings the repair engine (Ghostscript pass) can really
# fix -- mirrors the `fixable_ids` set in server.py's autofix().
STRUCTURE_FIXABLE_IDS = {"live_transparency_detected", "layers_detected"}

# fix_action values the existing autofix() engine actually performs. Anything
# else flagged auto_fix=True (e.g. "upscale_300dpi") belongs to a different
# tool (AI Upscale) -- the pipeline reports it honestly instead of promising
# a fix this engine never attempts.
ENGINE_FIX_ACTIONS = {"convert_cmyk", "scale_safe_margin", "fit_recenter_interior", "flatten", "flatten_pdfx"}

FIX_ACTION_LABELS = {
    "convert_cmyk": "Convert to CMYK and limit total ink coverage",
    "scale_safe_margin": "Pull cover art back inside the safe margin",
    "fit_recenter_interior": "Fit and re-center interior pages",
    "flatten": "Flatten transparency for print",
    "flatten_pdfx": "Flatten transparency/layers and declare PDF/X-1a",
}


# Present-tense wording for the live repair screen (what the engine is doing right now).
FIX_ACTION_PROGRESS = {
    "convert_cmyk": "Converting colors to CMYK and limiting ink coverage…",
    "scale_safe_margin": "Pulling text and art back inside the safe margin…",
    "fit_recenter_interior": "Fitting and re-centering every page…",
    "flatten": "Flattening transparency for print…",
    "flatten_pdfx": "Flattening layers and declaring PDF/X-1a…",
}


class StageTimeout(Exception):
    """A pipeline stage ran out of its share of the time budget."""


class Budget:
    def __init__(self, total: float = PIPELINE_BUDGET_S):
        self.total = total if time_limits_on() else max(total, NO_LIMIT_CEILING_S)
        self.start = time.monotonic()

    def cap(self, seconds: float) -> float:
        """Time allowed for one stage: a tight cap when limits are on, whatever
        is left of the (huge) ceiling when they're lifted."""
        return min(self.remaining(), seconds) if time_limits_on() else self.remaining()

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def remaining(self) -> float:
        return max(self.total - self.elapsed(), 0.0)


async def run_bounded(fn: Callable, seconds: float, *args, **kwargs):
    """Run a blocking function in a worker thread with a hard time limit
    (same reasoning as server.run_with_timeout: wait_for alone can't
    interrupt synchronous code)."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=max(seconds, 0.5))
    except asyncio.TimeoutError:
        raise StageTimeout(f"timed out after {seconds:.0f}s")


def sha256_file(path: Path, chunk: int = 1 << 20) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


class Trail:
    """Append-only audit trail. Every event a customer sees on screen is
    also recorded here, and Agent 4 audits this record -- not whatever an
    agent claims about itself."""

    def __init__(self, sink: Optional[Callable[[dict], Any]] = None):
        self.events: list[dict] = []
        self.records: dict[int, dict] = {}
        self._t0 = time.monotonic()
        self._seq = 0
        self._sink = sink

    def now(self) -> float:
        return round(time.monotonic() - self._t0, 2)

    def emit(self, type_: str, agent: int = 0, message: str = "", **data) -> dict:
        self._seq += 1
        ev = {"seq": self._seq, "t": self.now(), "type": type_, "agent": agent,
              "agent_name": AGENT_NAMES.get(agent, ""), "message": message, "data": data}
        if type_ != "heartbeat":
            self.events.append(ev)
        if self._sink:
            self._sink(ev)
        return ev

    def start_agent(self, agent: int, role: str):
        self.records[agent] = {"started": self.now(), "finished": None, "output": None}
        self.emit("agent_start", agent, role)

    def finish_agent(self, agent: int, output: dict, state: str = "done", message: str = ""):
        rec = self.records.setdefault(agent, {"started": self.now(), "finished": None, "output": None})
        rec["finished"] = self.now()
        rec["output"] = output
        rec["state"] = state
        self.emit("agent_done", agent, message, state=state)


def _norm_status(raw: Optional[str]) -> str:
    raw = (raw or "").lower()
    if raw == "pass":
        return "pass"
    if raw in ("fail", "critical", "error", "high"):
        return "fail"
    return "warning"


def normalize_issues(scan: dict) -> list[dict]:
    """Flattens one scan (compliance checks + the PDF structure findings the
    engine can repair) into a single uniform issue list.

    Each issue: id, label, status (pass|warning|fail), message, auto_fix,
    fix_action, informational, engine (True if the repair engine handles it).
    """
    issues: list[dict] = []
    for c in scan.get("compliance") or []:
        issues.append(_issue(c.get("id"), c.get("label"), c.get("status"), c.get("message"),
                             bool(c.get("auto_fix")), c.get("fix_action")))
    for f in scan.get("structure") or []:
        if f.get("id") in STRUCTURE_FIXABLE_IDS:
            issues.append(_issue(f["id"], f.get("title"), f.get("severity"), f.get("why_it_fails"),
                                 True, "flatten_pdfx"))
    return issues


def _issue(id_, label, status, message, auto_fix, fix_action) -> dict:
    informational = id_ in INFORMATIONAL_IDS
    return {
        "id": id_, "label": label or id_, "status": _norm_status(status), "message": message or "",
        "auto_fix": auto_fix, "fix_action": fix_action, "informational": informational,
        "engine": bool(auto_fix and fix_action in ENGINE_FIX_ACTIONS),
    }


def actionable(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i["status"] != "pass" and not i["informational"]]


def health_score(issues: list[dict]) -> int:
    """0-100: share of real (non-informational) checks currently passing."""
    real = [i for i in issues if not i["informational"]]
    if not real:
        return 100
    return round(100 * sum(1 for i in real if i["status"] == "pass") / len(real))


def brief(issue: Optional[dict]) -> Optional[dict]:
    if not issue:
        return None
    return {"id": issue["id"], "label": issue["label"], "status": issue["status"], "message": issue["message"]}
