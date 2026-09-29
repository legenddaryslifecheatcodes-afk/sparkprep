"""Generates a downloadable PDF audit report from a findings list --
the actual export/download artifact the original spec calls for,
distinct from AuditReport.jsx (which is an in-app page, not a file the
user can save/attach to an email/keep as a record).
"""
from datetime import datetime, timezone
from typing import Optional
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

_SEVERITY_COLOR = {
    "fail": colors.HexColor("#d03b3b"),
    "warning": colors.HexColor("#c98500"),
    "pass": colors.HexColor("#199e70"),
}


def generate_audit_brief_pdf(
    findings: list,
    summary: dict,
    project_meta: dict,
    output_path: str,
    brand_name: str = "SparkPrep",
) -> str:
    """The paid audit's downloadable PDF: summary plus each finding's what / where / why / publisher
    requirement. Never repair steps, tools or fix times -- the audit detects; SparkPrep's book fixes."""
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("BriefTitle", parent=styles["Title"], fontSize=18, spaceAfter=4)
    body_style = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10, leading=14)
    small_style = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=9, textColor=colors.grey)
    finding_line_style = ParagraphStyle("FindingLine", parent=styles["BodyText"], fontSize=10, leading=15, spaceAfter=3)

    doc = SimpleDocTemplate(
        output_path, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )
    story = []

    story.append(Paragraph(project_meta.get("audit_label") or f"{brand_name} Audit", title_style))
    story.append(Paragraph(
        f"{project_meta.get('title', 'Untitled project')} — "
        f"{project_meta.get('platform', 'Unknown platform')} — "
        f"{project_meta.get('trim_size', 'Unknown trim size')}",
        body_style,
    ))
    story.append(Paragraph(
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        small_style,
    ))
    story.append(Spacer(1, 0.2 * inch))

    risk = summary.get("rejection_risk", "unknown")
    risk_color = {"high": _SEVERITY_COLOR["fail"], "medium": _SEVERITY_COLOR["warning"],
                  "low": _SEVERITY_COLOR["warning"], "minimal": _SEVERITY_COLOR["pass"]}.get(risk, colors.grey)
    summary_data = [
        ["Critical failures", "Warnings", "Rejection risk"],
        [str(summary.get("critical_failures", 0)), str(summary.get("warnings", 0)), risk.upper()],
    ]
    summary_table = Table(summary_data, colWidths=[2.1 * inch] * 3)
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1efe8")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TEXTCOLOR", (2, 1), (2, 1), risk_color),
        ("FONTNAME", (2, 1), (2, 1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.25 * inch))

    order = {"fail": 0, "warning": 1, "pass": 2}
    sorted_findings = sorted(findings, key=lambda f: order.get(f.get("severity"), 3))

    # Owner's rule: the audit says what failed, where, why, and what the publisher requires -- never a
    # step-by-step repair. Doing the work is SparkPrep's main service.
    if not sorted_findings:
        story.append(Paragraph("No issues found. This file is ready for print submission. Thank you for using SparkPrep!", body_style))
    else:
        detail_style = ParagraphStyle("Detail", parent=body_style, fontSize=9, leading=12, leftIndent=14, spaceAfter=2)
        story.append(Paragraph("Issues found:", body_style))
        story.append(Spacer(1, 0.1 * inch))
        for f in sorted_findings:
            severity = f.get("severity", "warning")
            color = _SEVERITY_COLOR.get(severity, colors.grey)
            badge = f'<font color="{color.hexval()}">[{severity.upper()}]</font>'
            story.append(Paragraph(f"{badge} {_esc(f.get('title', 'Untitled finding'))}", finding_line_style))
            if f.get("why_it_fails"):
                story.append(Paragraph(_esc(f["why_it_fails"]), detail_style))
            where = _pinpoint_text(f.get("pinpoint"))
            if where:
                story.append(Paragraph(f"<b>Where:</b> {_esc(where)}", detail_style))
            if f.get("publisher_rule"):
                story.append(Paragraph(f"<b>Publisher requirement:</b> {_esc(f['publisher_rule'])}", detail_style))
            story.append(Spacer(1, 0.08 * inch))
        story.append(Spacer(1, 0.15 * inch))
        story.append(Paragraph(
            f"<b>{brand_name} can fix these issues for you right now</b> — no need to go anywhere else. "
            "Your audit fee is credited toward your SparkPrep book.", body_style))

    doc.build(story)
    return output_path


def _esc(s) -> str:
    from xml.sax.saxutils import escape
    return escape(str(s))


def _pinpoint_text(p: Optional[dict]) -> str:
    """Where in the file, and actual vs required -- e.g. 'Full cover · actual 13.72x9.0" · required 14.256x10.5"'."""
    if not p:
        return ""
    parts = [p["region"]] if p.get("region") else []
    if p.get("actual_dpi") is not None:
        parts.append(f"actual {p['actual_dpi']} DPI · required {p.get('required_dpi') or 300} DPI")
    for label, key in (("actual", "actual_inches"), ("required", "expected_inches")):
        if p.get(key):
            parts.append(f'{label} {p[key][0]}x{p[key][1]}"')
    for label, key in (("actual", "actual_pixels"), ("required", "required_pixels")):
        if p.get(key):
            parts.append(f"{label} {p[key][0]}x{p[key][1]}px")
    if p.get("actual") and p.get("required"):
        parts.append(f"{p['actual']} → required {p['required']}")
    return " · ".join(str(x) for x in parts)


def generate_repair_report_pdf(
    repair_log: list,
    final_compliance: list,
    project_meta: dict,
    output_path: str,
    brand_name: str = "SparkPrep",
) -> str:
    """Writes the "found & fixed" companion report bundled with every export --
    the owner's own words: customers should see the depth of what the app
    actually did, not just a pass/fail badge.

    repair_log: the project's full repair_log field (see
        autofix_agents.pipeline._append_repair_log) -- one entry per Repair Bay
        run that found something, in the order they happened.
    final_compliance: the CURRENT compliance list for whatever's being
        exported right now (export already guarantees no "fail" is present).
    """
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("RepairTitle", parent=styles["Title"], fontSize=18, spaceAfter=4)
    h2_style = ParagraphStyle("H2", parent=styles["Heading2"], spaceBefore=14, spaceAfter=6)
    body_style = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10, leading=14)
    small_style = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=9, textColor=colors.grey)
    run_title_style = ParagraphStyle("RunTitle", parent=styles["Heading3"], fontSize=11, spaceAfter=2)
    item_style = ParagraphStyle("Item", parent=styles["BodyText"], fontSize=10, leading=14, leftIndent=14)

    doc = SimpleDocTemplate(
        output_path, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )
    story = []

    story.append(Paragraph(f"{brand_name} — What We Found &amp; Fixed", title_style))
    story.append(Paragraph(
        f"{project_meta.get('title', 'Untitled project')} — "
        f"{project_meta.get('platform', 'Unknown platform')} — "
        f"{project_meta.get('trim_size', 'Unknown trim size')}",
        body_style,
    ))
    story.append(Paragraph(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", small_style))
    story.append(Spacer(1, 0.2 * inch))

    if not repair_log:
        story.append(Paragraph(
            "Nothing needed fixing. Every check we run passed the first time your file was scanned.",
            body_style,
        ))
    else:
        story.append(Paragraph(
            "SparkPrep found and fixed real print-compliance issues on this file. Here's a plain-language "
            "record of each pass, in order, so you can see exactly what changed and why.",
            body_style,
        ))
        story.append(Spacer(1, 0.15 * inch))
        for i, entry in enumerate(repair_log, start=1):
            by_id = {f["id"]: f for f in entry.get("found", [])}
            when = (entry.get("at") or "")[:16].replace("T", " ")
            slot_label = {"full_wrap": "cover", "front_cover": "cover", "back_cover": "cover",
                          "spine": "cover spine", "interior": "interior"}.get(entry.get("slot"), entry.get("slot") or "file")
            story.append(Paragraph(f"Pass {i} — {slot_label} — {when} UTC", run_title_style))
            for f in entry.get("found", []):
                story.append(Paragraph(f"• Found: {f['label']} — {f['message']}", item_style))
            # Older log entries stored remaining issues as whole objects rather than ids; a
            # dict can't be a lookup key, which crashed every export of such a project.
            def _name(r):
                if isinstance(r, dict):
                    return r.get("label") or r.get("id") or "issue"
                return by_id.get(r, {}).get("label", r)
            resolved = entry.get("resolved") or []
            if resolved:
                names = ", ".join(_name(r) for r in resolved)
                story.append(Paragraph(f'<font color="{_SEVERITY_COLOR["pass"].hexval()}">✓ Fixed: {names}</font>', item_style))
            remaining = entry.get("remaining") or []
            if remaining:
                names = ", ".join(_name(r) for r in remaining)
                story.append(Paragraph(f'<font color="{_SEVERITY_COLOR["warning"].hexval()}">Still open at this pass: {names}</font>', item_style))
            story.append(Spacer(1, 0.15 * inch))

    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph("Final status at export", h2_style))
    real_checks = [c for c in (final_compliance or []) if c.get("id") not in ("bleed", "pdfx1a", "pdf_dpi")]
    if not real_checks:
        story.append(Paragraph("All checks passed.", body_style))
    else:
        for c in real_checks:
            ok = c.get("status") == "pass"
            mark = f'<font color="{_SEVERITY_COLOR["pass"].hexval()}">✓</font>' if ok else f'<font color="{_SEVERITY_COLOR["warning"].hexval()}">!</font>'
            story.append(Paragraph(f"{mark} {c.get('label', c.get('id'))}", item_style))

    doc.build(story)
    return output_path
