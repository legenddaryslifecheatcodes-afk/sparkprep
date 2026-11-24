"""What happens when Scanner 1 and Scanner 2 disagree about the original file
(trey's plan, 2026-11-24):

  1. Scan again once with both scanners, in case one of them glitched.
  2. Fix the problems both scanners agree on, so the customer isn't stuck.
  3. Bring in a tiebreaker (a Claude review of a picture of the file plus both
     scanners' evidence) on each disputed item; the majority wins (2 of 3).
  4. If it's still unclear, tell the customer in plain words, with a picture,
     and ask them to check that spot. Nothing uncertain gets "fixed".
  5. Every disagreement is recorded for the admin view, including which
     scanner the tiebreaker sided with, so the scanner that keeps getting it
     wrong can be fixed.
"""
from __future__ import annotations

import base64
import json
from typing import Optional

PLAIN_RULE = {
    "resolution": "picture sharpness (resolution for print)",
    "color_cmyk": "print colours (CMYK)",
    "ink_limit": "how much ink is used",
    "page_size": "page size",
    "margins": "space between your text and the page edge",
    "fonts": "fonts built into the file",
    "color_profile": "the print colour profile",
    "pdfx": "the print-ready (PDF/X) label",
    "transparency": "see-through effects",
    "layers": "hidden layers",
}


def settle(disagreements: list, tiebreak: dict) -> dict:
    """Majority vote per disputed rule. `tiebreak` maps check id ->
    {"verdict": "problem"|"fine"|"unsure", "why": str} (missing = unsure).

    Returns:
      skip_checks   rules whose fix must NOT run (not proven to be a problem)
      checks        items the customer is asked to look at, in plain words
      outcomes      one row per dispute for the log
    """
    skip, checks, outcomes = set(), [], []
    for d in disagreements:
        cat = d["check"]
        s1_problem = not d["scanner1_passed"]
        tb = tiebreak.get(cat) or {}
        verdict = tb.get("verdict") if tb.get("verdict") in ("problem", "fine") else "unsure"
        rule = PLAIN_RULE.get(cat, d["label"])
        if verdict == "unsure":
            winner, real_problem = None, None
        else:
            real_problem = verdict == "problem"
            winner = "scanner1" if real_problem == s1_problem else "scanner2"
        if s1_problem and real_problem is not True:
            skip.add(cat)                       # never "fix" something not proven broken
        if real_problem is None or (real_problem and not s1_problem):
            # Unsettled, or a real problem Scanner 1 missed (so the fixer
            # wasn't aimed at it): the customer is asked to look.
            checks.append({
                "check": cat, "label": d["label"],
                "message": (f"Please check the {rule} on this file. Our two scanners didn't agree"
                            + (" and the tiebreaker couldn't decide" if real_problem is None else
                               ", and the tiebreaker thinks there is a problem")
                            + ", so we didn't change it. " + (tb.get("why") or d.get("evidence") or "")).strip(),
            })
        outcomes.append({"check": cat, "label": d["label"], "scanner1_passed": d["scanner1_passed"],
                         "scanner2_passed": d["scanner2_passed"], "scanner2_evidence": d.get("evidence"),
                         "tiebreak": verdict, "tiebreak_why": tb.get("why"), "winner": winner,
                         "fixed": s1_problem and real_problem is True})
    return {"skip_checks": skip, "checks": checks, "outcomes": outcomes}


async def claude_tiebreak(disputes: list, picture_png: Optional[bytes], *, api_key: str, model: str,
                          timeout_s: float = 20.0) -> dict:
    """Third opinion on each disputed rule. Returns {check: {"verdict", "why"}}.
    Any failure (no key, timeout, bad answer) returns {} -- every item then
    counts as "unsure" and goes to the customer, never as a pass."""
    import httpx

    if not api_key or not disputes:
        return {}
    items = [{"check": d["check"], "rule": PLAIN_RULE.get(d["check"], d["label"]),
              "scanner1_says": "pass" if d["scanner1_passed"] else "problem",
              "scanner2_says": "pass" if d["scanner2_passed"] else "problem",
              "scanner2_measured": d.get("evidence")} for d in disputes]
    prompt = (
        "You are the tiebreaker for a print-file checker for self-published books. Two scanners disagree about "
        "the items below. Using the picture of the file (if given) and each scanner's evidence, decide for each "
        'item whether there really is a print problem. Answer "unsure" whenever the picture and evidence don\'t '
        "let you tell -- a wrong answer is worse than unsure. For each item give one short plain-English sentence "
        "a non-technical author would understand. Reply with ONLY JSON: "
        '{"items": [{"check": "...", "verdict": "problem|fine|unsure", "why": "..."}]}\n\n' + json.dumps(items)
    )
    content = [{"type": "text", "text": prompt}]
    if picture_png:
        content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                       "data": base64.b64encode(picture_png).decode()}})
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model, "max_tokens": 600, "messages": [{"role": "user", "content": content}]},
            )
        if r.status_code != 200:
            return {}
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        start, end = text.find("{"), text.rfind("}")
        parsed = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except Exception:  # noqa: BLE001 - no opinion is the safe answer
        return {}
    out = {}
    wanted = {d["check"] for d in disputes}
    for it in parsed.get("items") or []:
        if it.get("check") in wanted:
            v = it.get("verdict")
            out[it["check"]] = {"verdict": v if v in ("problem", "fine", "unsure") else "unsure",
                                "why": str(it.get("why") or "")[:300]}
    return out
