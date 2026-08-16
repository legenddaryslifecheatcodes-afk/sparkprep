"""Audit engine — deep, pinpointed print-readiness report."""
from typing import List
from datetime import datetime, timezone


def compute_effective_dpi_report(width_px: int, height_px: int, target_w_in: float, target_h_in: float) -> dict:
    if target_w_in <= 0 or target_h_in <= 0:
        return {"dpi_x": 0, "dpi_y": 0, "effective_dpi": 0, "status": "fail"}
    dpi_x = width_px / target_w_in
    dpi_y = height_px / target_h_in
    eff = min(dpi_x, dpi_y)
    if eff >= 300: status = "pass"
    elif eff >= 250: status = "warning"
    elif eff >= 200: status = "warning"
    else: status = "fail"
    return {"dpi_x": round(dpi_x, 1), "dpi_y": round(dpi_y, 1), "effective_dpi": round(eff, 1), "status": status}


def deep_audit(file_metadata: dict, trim_w: float, trim_h: float, bleed: float, platform_name: str) -> List[dict]:
    """Return a list of pinpointed failure/risk findings.

    Each finding: {
      id, severity (fail|warning|pass), title, why_it_fails, publisher_rule,
      pinpoint (region + inches), fix_steps [str], fix_tools [str], est_fix_minutes
    }
    """
    findings = []

    # Bleed zone integrity
    if file_metadata.get("is_pdf"):
        w_in = (file_metadata.get("width_px") or 0) / 72.0
        h_in = (file_metadata.get("height_px") or 0) / 72.0
        expected_w = trim_w + bleed * 2
        expected_h = trim_h + bleed * 2
        if abs(w_in - expected_w) > 0.02 or abs(h_in - expected_h) > 0.02:
            findings.append({
                "id": "bleed_dimension_mismatch",
                "severity": "fail",
                "title": "Bleed dimensions don't match distributor requirement",
                "why_it_fails": (
                    f"Your PDF is {w_in:.3f}\" × {h_in:.3f}\" but {platform_name} expects "
                    f"{expected_w:.3f}\" × {expected_h:.3f}\" (trim {trim_w}\"×{trim_h}\" + {bleed}\" bleed each side). "
                    f"A mismatch of {abs(w_in - expected_w):.3f}\" wide / {abs(h_in - expected_h):.3f}\" tall "
                    "will cause the file to be rejected during automated preflight."
                ),
                "publisher_rule": f"{platform_name} File Creation Guide — Section: Bleed & Trim",
                "pinpoint": {
                    "region": "entire canvas",
                    "expected_inches": [round(expected_w, 3), round(expected_h, 3)],
                    "actual_inches": [round(w_in, 3), round(h_in, 3)],
                },
                "fix_steps": [
                    f"Open the PDF in Acrobat Pro or InDesign.",
                    f"Set the page size to exactly {expected_w:.3f}\" × {expected_h:.3f}\".",
                    "Reposition all artwork so trim guides align with the new canvas.",
                    "Extend background art to the outer edge (bleed zone) so nothing is cropped mid-image.",
                    "Re-export as PDF/X-1a with 'Crop marks' OFF.",
                ],
                "fix_tools": ["Adobe Acrobat Pro", "Adobe InDesign", "SparkPrep Auto-Fix"],
                "est_fix_minutes": 8,
                "one_click_fix": True,
            })
    else:
        w_px = file_metadata.get("width_px") or 0
        h_px = file_metadata.get("height_px") or 0
        expected_w = trim_w + bleed * 2
        expected_h = trim_h + bleed * 2
        report = compute_effective_dpi_report(w_px, h_px, expected_w, expected_h)
        if report["status"] == "fail":
            findings.append({
                "id": "resolution_too_low",
                "severity": "fail",
                "title": f"Resolution too low ({report['effective_dpi']} DPI)",
                "why_it_fails": (
                    f"To fill a {expected_w:.2f}\"×{expected_h:.2f}\" printed area (including {bleed}\" bleed), "
                    f"your image needs at least {int(expected_w * 300)}×{int(expected_h * 300)} pixels at 300 DPI. "
                    f"Your file is {w_px}×{h_px}px — that's roughly {report['effective_dpi']} DPI effective. "
                    "At this resolution, text will look fuzzy and photos will show visible pixelation once printed. "
                    f"{platform_name} auto-rejects covers below 240 DPI."
                ),
                "publisher_rule": f"{platform_name} File Creation Guide — Section: Resolution requirements",
                "pinpoint": {
                    "region": "entire image",
                    "actual_dpi": report["effective_dpi"],
                    "required_dpi": 300,
                    "actual_pixels": [w_px, h_px],
                    "required_pixels": [int(expected_w * 300), int(expected_h * 300)],
                },
                "fix_steps": [
                    f"Re-open your source file at the highest resolution available.",
                    f"Resize the canvas to at least {int(expected_w * 300)} × {int(expected_h * 300)} pixels.",
                    "If you don't have a higher-res source, AI upscaling (Topaz Gigapixel, Adobe Super Resolution) can help — but only if the original was at least 150 DPI.",
                    "Photo-shot elements below 150 DPI usually need re-shooting or replacing.",
                ],
                "fix_tools": ["Adobe Photoshop", "Topaz Gigapixel AI", "SparkPrep Auto-Fix (limited)"],
                "est_fix_minutes": 15,
                "one_click_fix": False,
            })
        elif report["status"] == "warning":
            findings.append({
                "id": "resolution_marginal",
                "severity": "warning",
                "title": f"Resolution is marginal ({report['effective_dpi']} DPI)",
                "why_it_fails": (
                    f"Your file is above the {platform_name} minimum rejection threshold, but at "
                    f"{report['effective_dpi']} DPI it will print noticeably softer than 300 DPI files. "
                    "Small text, thin lines and photographic detail may look blurry."
                ),
                "publisher_rule": f"{platform_name} — Recommended: 300 DPI minimum for print",
                "pinpoint": {"region": "entire image", "actual_dpi": report["effective_dpi"], "recommended_dpi": 300},
                "fix_steps": [
                    "Upscale to 300 DPI using a bicubic or AI method (Photoshop 'Preserve Details 2.0').",
                    "Or re-export from source at 300 DPI.",
                ],
                "fix_tools": ["Adobe Photoshop", "SparkPrep Auto-Fix"],
                "est_fix_minutes": 5,
                "one_click_fix": True,
            })

    # Color space
    color = (file_metadata.get("color_mode") or "").upper()
    if color in ("RGB", "RGBA", "P", "L", "LA"):
        findings.append({
            "id": "wrong_color_space",
            "severity": "fail",
            "title": f"Color space is {color} — must be CMYK",
            "why_it_fails": (
                "All commercial print production uses CMYK ink, not RGB screen colors. "
                "When your printer's RIP auto-converts RGB → CMYK, saturated blues become purple, "
                "vibrant reds go muddy, and neon greens turn olive. "
                f"{platform_name} may still accept the file but the printed result will not match your on-screen preview."
            ),
            "publisher_rule": f"{platform_name} — Cover/Interior File Guide: 'Submit files in CMYK color mode'",
            "pinpoint": {"region": "entire artwork", "actual": color, "required": "CMYK"},
            "fix_steps": [
                "In Photoshop: Image → Mode → CMYK Color (embed U.S. Web Coated SWOP v2 profile).",
                "In Illustrator: File → Document Color Mode → CMYK.",
                "Or use SparkPrep's one-click Auto-Fix (converts on export).",
                "After conversion, review saturated blues/greens for color shift.",
            ],
            "fix_tools": ["Adobe Photoshop", "Adobe Illustrator", "SparkPrep Auto-Fix"],
            "est_fix_minutes": 3,
            "one_click_fix": True,
        })

    # Transparency
    if file_metadata.get("has_transparency"):
        findings.append({
            "id": "unflattened_transparency",
            "severity": "warning",
            "title": "File contains transparency — will cause unpredictable printing",
            "why_it_fails": (
                "Transparency effects (drop shadows, gradients on transparent layers, PNG alpha) can "
                "render inconsistently across different RIP software. Some elements may disappear, others "
                "may print with visible edge artifacts. Print houses require fully flattened artwork."
            ),
            "publisher_rule": f"{platform_name} — 'All transparency must be flattened before submission'",
            "pinpoint": {"region": "layers with alpha channel", "layer_type": file_metadata.get("color_mode")},
            "fix_steps": [
                "In Photoshop: Layer → Flatten Image, then Save As PDF with 'Flatten transparency' checked.",
                "In Illustrator: Object → Flatten Transparency (High Resolution preset).",
                "Or export from SparkPrep — flattening happens automatically.",
            ],
            "fix_tools": ["Adobe Photoshop", "Adobe Illustrator", "SparkPrep Auto-Fix"],
            "est_fix_minutes": 4,
            "one_click_fix": True,
        })

    # PDF/X-1a
    findings.append({
        "id": "pdf_x1a_export",
        "severity": "warning",
        "title": "Output must be PDF/X-1a:2001",
        "why_it_fails": (
            f"{platform_name} requires PDF/X-1a — a subset of PDF designed for print with embedded fonts, "
            "flattened transparency, CMYK color, and no external references. Standard 'Save as PDF' from "
            "most apps produces PDF 1.7 with RGB elements and unembedded fonts, which will be rejected."
        ),
        "publisher_rule": f"{platform_name} — 'PDF/X-1a:2001 or PDF/X-1a:2003 required for print files'",
        "pinpoint": {"region": "PDF standard header", "required": "PDF/X-1a:2001"},
        "fix_steps": [
            "In Acrobat Pro: File → Save As Other → More Options → PDF/X-1a → Save.",
            "In InDesign: File → Export → PDF (Print) → Standard: PDF/X-1a:2001.",
            "Or use SparkPrep Export — every file leaves as PDF/X-1a automatically.",
        ],
        "fix_tools": ["Adobe Acrobat Pro", "Adobe InDesign", "SparkPrep Export"],
        "est_fix_minutes": 2,
        "one_click_fix": True,
    })

    return findings


def audit_summary(findings: list) -> dict:
    fails = sum(1 for f in findings if f["severity"] == "fail")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    est_minutes = sum(f.get("est_fix_minutes", 0) for f in findings if f["severity"] in ("fail", "warning"))
    rejection_risk = "high" if fails >= 2 else ("medium" if fails >= 1 else ("low" if warnings >= 2 else "minimal"))
    return {
        "total_issues": len(findings),
        "critical_failures": fails,
        "warnings": warnings,
        "estimated_fix_minutes": est_minutes,
        "rejection_risk": rejection_risk,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
