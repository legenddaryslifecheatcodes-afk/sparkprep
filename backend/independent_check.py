"""Runs the independent checker (independent_checker/) in its own process.

The checker is started as a separate Python process with:
  * a clean environment (no database address, no API keys, no secrets),
  * a memory cap (CHECKER_MEMORY_MB) and a time limit (CHECKER_TIMEOUT_S),
  * nothing shared with the app but the file path it is asked to read.
It prints one JSON answer; anything else (a crash, a timeout, garbage) is
reported as "the independent checker could not confirm", never as a pass.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

BACKEND_DIR = Path(__file__).resolve().parent
CHECKER_TIMEOUT_S = 90
CHECKER_MEMORY_MB = 900

# Main-scanner finding id -> the independent checker's check that judges the
# same printer rule (its own way). Findings with no entry (e.g. cover text
# margins, read by OCR) aren't judged by the independent checker, and the
# verification trail says so instead of pretending.
CATEGORY_FOR_ISSUE = {
    "dpi": "resolution",
    "colorspace": "color_cmyk",
    "total_ink_coverage": "ink_limit",
    "cover_size": "page_size",
    "interior_page_size_mismatch": "page_size",
    "interior_safety_margin": "margins",
    "fonts_not_embedded": "fonts",
    "icc_profile_missing": "color_profile",
    "pdfx1a_not_declared": "pdfx",
    "pdfx1a_missing_output_intent": "pdfx",
    "live_transparency_detected": "transparency",
    "layers_detected": "layers",
}


def _limit_resources():  # runs in the child just before it starts
    try:
        import resource
        cap = CHECKER_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        resource.setrlimit(resource.RLIMIT_CPU, (CHECKER_TIMEOUT_S, CHECKER_TIMEOUT_S + 5))
    except Exception:  # noqa: BLE001 - not available on this OS; the timeout still applies
        pass


def run_independent_check(path: str, kind: str, expect: dict, timeout_s: Optional[float] = None) -> dict:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    try:
        proc = subprocess.run(
            [sys.executable, "-E", "-s", "-m", "independent_checker"],
            input=json.dumps({"path": str(path), "kind": kind, "expect": expect}),
            capture_output=True, text=True, cwd=str(BACKEND_DIR), env=env,
            timeout=timeout_s or CHECKER_TIMEOUT_S,
            preexec_fn=_limit_resources if os.name == "posix" else None,
        )
        result = json.loads(proc.stdout)
        if not isinstance(result, dict) or "checks" not in result:
            raise ValueError("unexpected answer")
        return result
    except subprocess.TimeoutExpired:
        return {"checks": [], "ok": False, "error": "the independent checker ran out of time"}
    except Exception as e:  # noqa: BLE001
        return {"checks": [], "ok": False, "error": f"the independent checker failed: {type(e).__name__}"}


def compare(diagnosis_fixable: list, before: dict, after: dict) -> dict:
    """The independent checker's verdict on one repair.

    Blocks ("agree": False) when:
      * it couldn't run or open the repaired file,
      * a problem the repair was meant to fix still fails by its own check,
      * something it passed before the repair fails after it (a regression).
    """
    if after.get("error") or not after.get("checks"):
        return {"agree": False, "reason": after.get("error") or "no answer from the independent checker",
                "targeted": [], "regressions": [], "not_judged": []}
    a = {c["id"]: c for c in after["checks"]}
    b = {c["id"]: c for c in (before or {}).get("checks") or []}
    if a.get("opens", {}).get("passed") is False:
        return {"agree": False, "reason": "the independent checker couldn't open the repaired file",
                "targeted": [], "regressions": [], "not_judged": []}
    targeted, not_judged = [], []
    for issue in diagnosis_fixable:
        cat = CATEGORY_FOR_ISSUE.get(issue["id"])
        check = a.get(cat) if cat else None
        if not check or check["passed"] is None:
            not_judged.append(issue["id"])
            continue
        targeted.append({"issue": issue["id"], "check": cat, "passed": bool(check["passed"]),
                         "label": check["label"], "evidence": check["evidence"]})
    regressions = [c for cid, c in a.items()
                   if c["passed"] is False and b.get(cid, {}).get("passed") is True]
    failed_targets = [t for t in targeted if not t["passed"]]
    agree = not failed_targets and not regressions
    reason = None
    if failed_targets:
        reason = "still failing by the independent check: " + "; ".join(f"{t['label']} ({t['evidence']})" for t in failed_targets)
    elif regressions:
        reason = "the repair broke something: " + "; ".join(f"{c['label']} ({c['evidence']})" for c in regressions)
    return {"agree": agree, "reason": reason, "targeted": targeted, "regressions": regressions, "not_judged": not_judged}


def cross_check(scanner1_issues: list, independent: dict) -> dict:
    """Step 2 of a verified fix: does Scanner 2's own scan of the ORIGINAL file
    agree with Scanner 1's findings?

    Scanner 2 has already scanned the file on its own (separate process, no
    database, no keys, nothing from Scanner 1). Only afterwards are the two
    sets of findings laid side by side here. They disagree when:
      * Scanner 2 couldn't run or couldn't open the file ("could not confirm"),
      * Scanner 1 calls something a problem that Scanner 2 says is fine,
      * Scanner 2 finds a problem with something Scanner 1 says passes.
    Rules only one of the scanners measures are listed as not_judged.
    """
    if independent.get("error") or not independent.get("checks"):
        return {"agree": False, "reason": "Scanner 2 could not confirm the scan: "
                + (independent.get("error") or "no answer from the independent checker"),
                "compared": [], "disagreements": [], "not_judged": []}
    s2 = {c["id"]: c for c in independent["checks"]}
    if s2.get("opens", {}).get("passed") is False:
        return {"agree": False, "reason": "Scanner 2 couldn't open the file: " + s2["opens"].get("evidence", ""),
                "compared": [], "disagreements": [], "not_judged": []}

    # Scanner 1's verdict per printer rule: a rule fails if any finding for it fails.
    s1: dict = {}
    for issue in scanner1_issues:
        if issue.get("informational"):
            continue
        cat = CATEGORY_FOR_ISSUE.get(issue.get("id"))
        if not cat:
            continue
        row = s1.setdefault(cat, {"failed": False, "issues": []})
        row["issues"].append(issue)
        if issue.get("status") != "pass":
            row["failed"] = True

    compared, disagreements, not_judged = [], [], []
    for cat, row in s1.items():
        check = s2.get(cat)
        if not check or check.get("passed") is None:
            not_judged.extend(i["id"] for i in row["issues"])
            continue
        s1_ok, s2_ok = not row["failed"], bool(check["passed"])
        entry = {"check": cat, "label": check["label"], "scanner1_passed": s1_ok, "scanner2_passed": s2_ok,
                 "evidence": check["evidence"], "issues": [i["id"] for i in row["issues"]], "agree": s1_ok == s2_ok}
        compared.append(entry)
        if not entry["agree"]:
            disagreements.append(entry)

    reason = None
    if disagreements:
        parts = []
        for d in disagreements:
            if d["scanner1_passed"]:
                parts.append(f"{d['label']}: Scanner 2 found a problem Scanner 1 passed ({d['evidence']})")
            else:
                parts.append(f"{d['label']}: Scanner 1 found a problem Scanner 2 says is fine ({d['evidence']})")
        reason = "; ".join(parts)
    return {"agree": not disagreements, "reason": reason, "compared": compared,
            "disagreements": disagreements, "not_judged": not_judged}
