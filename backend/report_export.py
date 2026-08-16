"""Generates a downloadable PDF audit report from a findings list --
the actual export/download artifact the original spec calls for,
distinct from AuditReport.jsx (which is an in-app page, not a file the
user can save/attach to an email/keep as a record).
"""
from datetime import datetime, timezone
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


def generate_audit_report_pdf(
    findings: list,
    summary: dict,
    project_meta: dict,
    output_path: str,
) -> str:
    """Writes a formatted PDF report and returns the output path.

    findings: list of finding dicts (severity, title, why_it_fails,
        publisher_rule, fix_steps, ...) as produced by audit_engine.deep_audit()
        and pdfx_validator.run_pdf_structure_audit().
    summary: dict from audit_engine.audit_summary().
    project_meta: dict with at least 'title', 'platform', 'trim_size'.
    """
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ReportTitle", parent=styles["Title"], fontSize=20, spaceAfter=4)
    h2_style = ParagraphStyle("H2", parent=styles["Heading2"], spaceBefore=14, spaceAfter=6)
    body_style = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10, leading=14)
    small_style = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=9, textColor=colors.grey)
    finding_title_style = ParagraphStyle("FindingTitle", parent=styles["Heading3"], fontSize=12, spaceAfter=2)

    doc = SimpleDocTemplate(
        output_path, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )
    story = []

    # Header
    story.append(Paragraph("SparkPrep Print-Readiness Audit Report", title_style))
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
    story.append(Spacer(1, 0.25 * inch))

    # Summary table
    risk = summary.get("rejection_risk", "unknown")
    risk_color = {"high": _SEVERITY_COLOR["fail"], "medium": _SEVERITY_COLOR["warning"],
                  "low": _SEVERITY_COLOR["warning"], "minimal": _SEVERITY_COLOR["pass"]}.get(risk, colors.grey)
    summary_data = [
        ["Critical failures", "Warnings", "Est. fix time", "Rejection risk"],
        [
            str(summary.get("critical_failures", 0)),
            str(summary.get("warnings", 0)),
            f"{summary.get('estimated_fix_minutes', 0)} min",
            risk.upper(),
        ],
    ]
    summary_table = Table(summary_data, colWidths=[1.6 * inch] * 4)
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1efe8")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TEXTCOLOR", (3, 1), (3, 1), risk_color),
        ("FONTNAME", (3, 1), (3, 1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.3 * inch))

    # Findings, worst severity first
    order = {"fail": 0, "warning": 1, "pass": 2}
    sorted_findings = sorted(findings, key=lambda f: order.get(f.get("severity"), 3))

    if not sorted_findings:
        story.append(Paragraph("No issues found. This file is ready for print submission.", body_style))
    else:
        story.append(Paragraph("Findings", h2_style))
        for f in sorted_findings:
            severity = f.get("severity", "warning")
            color = _SEVERITY_COLOR.get(severity, colors.grey)
            badge = f'<font color="{color.hexval()}">[{severity.upper()}]</font>'
            story.append(Paragraph(f"{badge} {f.get('title', 'Untitled finding')}", finding_title_style))
            if f.get("why_it_fails"):
                story.append(Paragraph(f.get("why_it_fails"), body_style))
            if f.get("publisher_rule"):
                story.append(Paragraph(f"<i>Rule: {f['publisher_rule']}</i>", small_style))
            fix_steps = f.get("fix_steps") or []
            if fix_steps:
                steps_html = "<br/>".join(f"{i+1}. {step}" for i, step in enumerate(fix_steps))
                story.append(Paragraph(steps_html, body_style))
            story.append(Spacer(1, 0.18 * inch))

    doc.build(story)
    return output_path
