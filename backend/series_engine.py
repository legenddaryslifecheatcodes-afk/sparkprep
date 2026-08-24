"""Series consistency checker.

A "series" is just projects that share the same free-text `series_name`
field on a project. Distributors don't enforce series consistency
themselves, but a series with a trim size that changes halfway through,
or a book 3 printed on a different distributor than books 1-2, is a real
problem for an author/publisher shipping multiple titles that are meant
to sit next to each other on a shelf -- this catches that before export,
across every book in the series at once, using only fields the project
model already has (trim_size, binding, paper_type, platform, isbn).
"""
from typing import List, Optional


def _majority(values: List[str]) -> Optional[str]:
    """Returns the most common non-empty value, or None if there isn't one."""
    counts = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def check_series_consistency(projects: List[dict], labels: dict) -> List[dict]:
    """projects: list of project dicts (must have id/name/trim_size/binding/
    paper_type/platform/isbn). labels: {"trim_sizes": TRIM_SIZES, "platforms":
    PLATFORMS, "paper_types": PAPER_TYPES} for human-readable messages.

    Returns a list of findings, each naming which book(s) are the odd one
    out relative to the series majority -- not just "these differ".
    """
    findings = []
    if len(projects) < 2:
        return findings

    trim_sizes = labels.get("trim_sizes", {})
    platforms = labels.get("platforms", {})
    paper_types = labels.get("paper_types", {})

    def field_label(field, key):
        if field == "trim_size":
            return trim_sizes.get(key, {}).get("label", key)
        if field == "platform":
            return platforms.get(key, {}).get("name", key)
        if field == "paper_type":
            return paper_types.get(key, {}).get("label", key)
        return key

    checks = [
        ("trim_size", "fail", "Trim size differs across the series",
         "Different trim sizes in the same series mean the books will be physically different "
         "sizes on a shelf, and a reader buying the set will notice immediately. This is almost "
         "always unintentional."),
        ("binding", "warning", "Binding differs across the series",
         "Mixing paperback/hardcover-case/hardcover-jacket within a series is sometimes "
         "deliberate (e.g. a special edition), but worth confirming it's not a mistake."),
        ("paper_type", "warning", "Paper stock differs across the series",
         "Different paper stock changes the spine-width-per-page ratio and the page color/feel "
         "book to book -- readers comparing spines on a shelf will notice a mismatch."),
        ("platform", "warning", "Distributor differs across the series",
         "Publishing different books in the same series through different distributors is "
         "sometimes intentional (e.g. exclusivity deals), but it means trim/bleed/barcode specs "
         "aren't guaranteed to match, so double-check this was a deliberate choice."),
    ]

    for field, severity, title, why in checks:
        values = [p.get(field) for p in projects]
        majority = _majority(values)
        if majority is None:
            continue
        outliers = [p for p in projects if p.get(field) and p.get(field) != majority]
        if not outliers:
            continue
        findings.append({
            "id": f"series_{field}_mismatch",
            "severity": severity,
            "title": title,
            "why_it_fails": why,
            "expected": field_label(field, majority),
            "outliers": [
                {"project_id": p["id"], "name": p.get("name"), "value": field_label(field, p.get(field))}
                for p in outliers
            ],
            "fix_steps": [f"Set every book in this series to {field_label(field, majority)} (the series majority), or confirm the difference is intentional."],
        })

    # ISBN publisher-prefix consistency -- ISBN-13 EAN.UCC-13 structure is
    # 978/979-<registration group>-<registrant/publisher prefix>-<title>-<check digit>.
    # Different books from the same publisher/imprint normally share the
    # registrant prefix; a mismatch usually means an ISBN was entered wrong
    # or bought from a different pool than the rest of the series.
    isbns = [(p, (p.get("isbn") or "").replace("-", "").replace(" ", "")) for p in projects]
    isbns = [(p, i) for p, i in isbns if len(i) == 13 and i.isdigit()]
    if len(isbns) >= 2:
        prefixes = [i[:7] for _, i in isbns]  # first 7 digits ~ EAN prefix + registration group + start of registrant
        majority_prefix = _majority(prefixes)
        outliers = [p for (p, i) in isbns if i[:7] != majority_prefix]
        if outliers and majority_prefix:
            findings.append({
                "id": "series_isbn_prefix_mismatch",
                "severity": "warning",
                "title": "ISBN publisher prefix differs across the series",
                "why_it_fails": (
                    "These ISBNs don't share the same registrant prefix as the rest of the series. "
                    "That's expected if you deliberately bought ISBNs from different pools, but it's "
                    "also a common sign one was mistyped or reused from an unrelated project."
                ),
                "outliers": [{"project_id": p["id"], "name": p.get("name")} for p in outliers],
                "fix_steps": ["Double-check each flagged book's ISBN was entered correctly and belongs to this series' publisher account."],
            })

    return findings
