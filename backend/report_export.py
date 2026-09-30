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


_GOLD = colors.HexColor("#B8912F")
_INK = colors.HexColor("#16161a")


def _seal_drawing(size: float = 1.9 * inch):
    """The "SparkPrep Certified" seal: a gold double ring with the words in the middle."""
    from reportlab.graphics.shapes import Drawing, Circle, String
    d = Drawing(size, size)
    c = size / 2
    d.add(Circle(c, c, c - 2, strokeColor=_GOLD, strokeWidth=3, fillColor=colors.HexColor("#FBF6E8")))
    d.add(Circle(c, c, c - 9, strokeColor=_GOLD, strokeWidth=1, fillColor=None))
    d.add(String(c, c + 20, "SPARKPREP", textAnchor="middle", fontName="Helvetica-Bold", fontSize=13, fillColor=_INK))
    d.add(String(c, c - 4, "CERTIFIED", textAnchor="middle", fontName="Helvetica-Bold", fontSize=19, fillColor=_GOLD))
    d.add(String(c, c - 24, "PUBLISHER PREFLIGHT", textAnchor="middle", fontName="Helvetica", fontSize=7.5, fillColor=_INK))
    return d


def generate_preflight_report_pdf(cert: dict, repair_log: list, project_meta: dict, output_path: str,
                                  brand_name: str = "SparkPrep") -> str:
    """The report every finished book gets (owner's rule). `cert` is server._certify_final_files()'s result,
    measured on the FINAL files. Certified -> "The SparkPrep Certified Complete Publisher Preflight Report"
    with the seal; otherwise the plainer "SparkPrep Preflight Report" listing what's still open. Every claim
    in it is a check that actually ran -- the seal is only worth something if it's never overclaimed."""
    styles = getSampleStyleSheet()
    certified = bool(cert.get("certified"))
    title_style = ParagraphStyle("PFTitle", parent=styles["Title"], fontSize=17 if certified else 18,
                                 leading=21, spaceAfter=4, textColor=_INK)
    h2_style = ParagraphStyle("PFH2", parent=styles["Heading2"], fontSize=12.5, spaceBefore=12, spaceAfter=5, textColor=_INK)
    body_style = ParagraphStyle("PFBody", parent=styles["BodyText"], fontSize=10, leading=14)
    small_style = ParagraphStyle("PFSmall", parent=styles["BodyText"], fontSize=8.5, leading=11.5, textColor=colors.grey)
    center_small = ParagraphStyle("PFCenterSmall", parent=small_style, alignment=1)
    item_style = ParagraphStyle("PFItem", parent=styles["BodyText"], fontSize=10, leading=14, leftIndent=14)
    run_title_style = ParagraphStyle("PFRun", parent=styles["Heading3"], fontSize=10.5, spaceAfter=2)
    green = _SEVERITY_COLOR["pass"].hexval()
    amber = _SEVERITY_COLOR["warning"].hexval()

    doc = SimpleDocTemplate(output_path, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.7 * inch,
                            leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            title=cert.get("report_name") or "SparkPrep Preflight Report", author=brand_name)
    story = []
    checked = (cert.get("checked_at") or datetime.now(timezone.utc).isoformat())[:16].replace("T", " ")

    if certified:
        seal = Table([[_seal_drawing()]], colWidths=[7 * inch])
        seal.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        story.append(seal)
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph("The SparkPrep Certified Complete Publisher Preflight Report",
                               ParagraphStyle("PFTitleC", parent=title_style, alignment=1)))
        story.append(Paragraph(f"Certificate {_esc(cert.get('certificate_id'))} &nbsp;·&nbsp; checked {checked} UTC", center_small))
    else:
        story.append(Paragraph(f"{brand_name} Preflight Report", title_style))
        story.append(Paragraph(f"Checked {checked} UTC", small_style))
    story.append(Spacer(1, 0.15 * inch))

    # The book
    # Plain table cells aren't parsed as markup, so they take the raw text (escaping here printed "&amp;").
    rows = [["Book", str(project_meta.get("title", "Untitled"))],
            ["Distributor", str(project_meta.get("platform", ""))],
            ["Trim size", str(project_meta.get("trim_size", ""))],
            ["Binding", str(project_meta.get("binding", ""))],
            ["Paper", str(project_meta.get("paper", ""))]]
    if project_meta.get("page_count"):
        rows.append(["Page count", str(project_meta["page_count"])])
    if project_meta.get("spine_width"):
        rows.append(["Spine width", f'{project_meta["spine_width"]:.3f}"'])
    for f in cert.get("files") or []:
        rows.append([f"{f['part']} file", f'{f["size_in"][0]}" x {f["size_in"][1]}"'
                     + (f", {f['pages']} pages" if f["part"] == "Interior" else "")])
    t = Table(rows, colWidths=[1.6 * inch, 5.4 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9.5), ("FONT", (1, 0), (1, -1), "Helvetica", 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#dddddd")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)

    # What was checked
    story.append(Paragraph("What SparkPrep checked on your final files", h2_style))
    ip = cert.get("interior_pages")
    if ip:
        story.append(Paragraph(f"Interior: {ip['checked']} of {ip['total']} pages checked, every page.", small_style))
    for c in cert.get("checks") or []:
        mark = f'<font color="{green}">&#10003;</font>' if c["passed"] else f'<font color="{amber}">!</font>'
        story.append(Paragraph(f"{mark} {_esc(c['label'])}", item_style))

    if not certified:
        story.append(Paragraph("Still open", h2_style))
        for o in cert.get("open") or []:
            story.append(Paragraph(f'<font color="{amber}">!</font> <b>{_esc(o["title"])}</b>', item_style))
            if o.get("why"):
                story.append(Paragraph(_esc(o["why"]), ParagraphStyle("PFWhy", parent=small_style, leftIndent=26)))
            if o.get("steps"):
                story.append(Paragraph("<b>How to fix it yourself:</b>", ParagraphStyle("PFHow", parent=body_style, leftIndent=26, spaceBefore=3)))
                for n, step in enumerate(o["steps"], start=1):
                    story.append(Paragraph(f"{n}. {_esc(step)}", ParagraphStyle("PFStep", parent=body_style, leftIndent=38, fontSize=9.5, leading=13)))
            if o.get("tools"):
                story.append(Paragraph(f"Tools that can do this: {_esc(', '.join(o['tools']))}",
                                       ParagraphStyle("PFTools", parent=small_style, leftIndent=26)))
            story.append(Spacer(1, 0.06 * inch))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph("SparkPrep couldn't fix these on its own, so the steps to fix each one are above. "
                               "Fix them and export again — once everything is clear, your book earns "
                               "<b>SparkPrep Certified</b>.", body_style))

    # What SparkPrep found and fixed along the way
    story.append(Paragraph("What SparkPrep found and fixed", h2_style))
    if not repair_log:
        story.append(Paragraph("Nothing needed fixing — every check passed the first time your files were scanned.", body_style))
    for i, entry in enumerate(repair_log or [], start=1):
        by_id = {f["id"]: f for f in entry.get("found", [])}
        when = (entry.get("at") or "")[:16].replace("T", " ")
        slot_label = {"full_wrap": "cover", "front_cover": "cover", "back_cover": "cover", "case_wrap": "case cover",
                      "spine": "cover spine", "interior": "interior"}.get(entry.get("slot"), entry.get("slot") or "file")
        how = " — AI Upscale" if entry.get("status") == "ai_upscale" else ""
        story.append(Paragraph(f"Pass {i} — {slot_label}{how} — {when} UTC", run_title_style))
        for f in entry.get("found", []):
            story.append(Paragraph(f"• Found: {_esc(f['label'])} — {_esc(f['message'])}", item_style))

        # Older log entries stored remaining issues as whole objects rather than ids.
        def _name(r):
            if isinstance(r, dict):
                return r.get("label") or r.get("id") or "issue"
            return by_id.get(r, {}).get("label", r)
        resolved = entry.get("resolved") or []
        if resolved:
            story.append(Paragraph(f'<font color="{green}">&#10003; Fixed: {_esc(", ".join(_name(r) for r in resolved))}</font>', item_style))
        remaining = entry.get("remaining") or []
        if remaining:
            story.append(Paragraph(f'<font color="{amber}">Still open at this pass: {_esc(", ".join(_name(r) for r in remaining))}</font>', item_style))
        story.append(Spacer(1, 0.08 * inch))

    # The statement
    story.append(Spacer(1, 0.12 * inch))
    platform = _esc(project_meta.get("platform", "the distributor"))
    if certified:
        story.append(Paragraph(
            f"<b>SparkPrep certifies</b> that on {checked} UTC the files listed above passed every check in this "
            f"report, measured on the final files themselves, against {platform}'s published file specifications.",
            body_style))
    story.append(Paragraph(
        f"The printer makes the final acceptance decision. SparkPrep checks files against {platform}'s published "
        "specifications; it does not control printing, trimming or binding. For the best result, order a printed proof.",
        small_style))

    doc.build(story)
    return output_path
