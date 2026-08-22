"""PDF structural validation engine.

Unlike audit_engine.py (which checks the *source file* metadata before a
PDF exists -- pixel dimensions, PIL color mode, etc.), this module opens
an actual PDF and inspects its internal structure directly with pikepdf:
whether it's really flagged as PDF/X-1a, whether every font is actually
embedded, whether it contains live transparency or optional-content
layers, and whether declared color profiles are present. These are
things that can only be checked by reading the PDF itself, not by
looking at the file that was used to build it.

Each check returns a finding in the same shape audit_engine.py uses
(id, severity, title, why_it_fails, publisher_rule, pinpoint, fix_steps,
fix_tools, est_fix_minutes, one_click_fix) or a list of findings, so
these can be merged directly into the existing deep_audit() output.
"""
from typing import List, Optional
import pikepdf


def _finding(**kwargs) -> dict:
    base = {
        "severity": "warning",
        "pinpoint": {},
        "fix_steps": [],
        "fix_tools": [],
        "est_fix_minutes": 5,
        "one_click_fix": False,
    }
    base.update(kwargs)
    return base


def check_pdfx1a_declared(pdf: pikepdf.Pdf, platform_name: str) -> Optional[dict]:
    """Checks whether the PDF actually declares itself PDF/X-1a and has a
    valid OutputIntent -- not whether it merely *could* be made compliant.
    A file with no /GTS_PDFXVersion key was never flagged as PDF/X-1a by
    whatever produced it, regardless of how correct its content is.
    """
    version = pdf.Root.get("/GTS_PDFXVersion")
    has_output_intent = "/OutputIntents" in pdf.Root and len(pdf.Root.OutputIntents) > 0

    if version is None:
        return _finding(
            id="pdfx1a_not_declared",
            severity="fail",
            title="File is not declared as PDF/X-1a",
            why_it_fails=(
                "This PDF has no /GTS_PDFXVersion entry, meaning nothing marked it as "
                "PDF/X-1a when it was created. Most print distributors check for this "
                "flag specifically during automated preflight and reject files without it, "
                "even if the content itself would otherwise pass."
            ),
            publisher_rule=f"{platform_name} — 'File must be PDF/X-1a:2001 or PDF/X-1a:2003'",
            pinpoint={"region": "PDF document catalog", "expected": "GTS_PDFXVersion present", "actual": "missing"},
            fix_steps=[
                "Re-export from your design tool with the PDF/X-1a preset selected explicitly.",
                "Or run this file through SparkPrep's PDF/X-1a export, which sets this flag correctly.",
            ],
            fix_tools=["Adobe Acrobat Pro", "Adobe InDesign", "SparkPrep Export"],
            one_click_fix=True,
        )

    if not has_output_intent:
        return _finding(
            id="pdfx1a_missing_output_intent",
            severity="fail",
            title="PDF/X-1a is declared but missing a required output intent",
            why_it_fails=(
                "PDF/X-1a requires an /OutputIntents entry describing the target color "
                "condition (e.g. CGATS TR 001 / SWOP). This file claims PDF/X-1a compliance "
                "but has no output intent, which makes the PDF/X-1a declaration invalid — "
                "some preflight tools will flag this as a corrupt or fraudulent PDF/X claim."
            ),
            publisher_rule=f"{platform_name} — 'PDF/X-1a output intent required'",
            pinpoint={"region": "PDF document catalog", "expected": "OutputIntents array present", "actual": "missing"},
            fix_steps=["Re-export as PDF/X-1a with an output intent / ICC profile selected, not just the PDF/X-1a checkbox alone."],
            fix_tools=["Adobe Acrobat Pro", "SparkPrep Export"],
            one_click_fix=True,
        )

    return None


def check_transparency(pdf: pikepdf.Pdf, max_pages: Optional[int] = None) -> Optional[dict]:
    """Checks the PDF's actual page content for live transparency groups,
    soft masks, or non-opaque ExtGState entries -- not just whether the
    *source image* had an alpha channel before the PDF was built.

    max_pages limits the scan to the first N pages -- this is what makes
    the Basic (page-1-only) check distinct from the paid Advanced Interior
    Check (up to 300 pages). None means no limit.
    """
    pages_to_scan = pdf.pages[:max_pages] if max_pages is not None else pdf.pages
    found = []
    for page_num, page in enumerate(pages_to_scan, start=1):
        if "/Group" in page and page.Group.get("/S") == pikepdf.Name("/Transparency"):
            found.append(page_num)
            continue
        resources = page.get("/Resources", {})
        for gs in resources.get("/ExtGState", {}).values() if "/ExtGState" in resources else []:
            ca = gs.get("/ca")
            CA = gs.get("/CA")
            if (ca is not None and float(ca) < 1.0) or (CA is not None and float(CA) < 1.0):
                found.append(page_num)
                break
        xobjects = resources.get("/XObject", {})
        for xobj in xobjects.values() if "/XObject" in xobjects else []:
            if "/SMask" in xobj:
                found.append(page_num)
                break

    if not found:
        return None

    pages = sorted(set(found))
    return _finding(
        id="live_transparency_detected",
        severity="fail",
        title=f"Unflattened transparency on page{'s' if len(pages) > 1 else ''} {', '.join(map(str, pages[:10]))}",
        why_it_fails=(
            "This PDF contains live transparency (a transparency group, soft mask, or "
            "partial opacity), not flattened artwork. PDF/X-1a forbids live transparency "
            "-- everything must be pre-flattened to opaque, final-appearance content before "
            "export, because RIP software handles unflattened transparency inconsistently."
        ),
        publisher_rule="PDF/X-1a:2001 — 'No live transparency permitted'",
        pinpoint={"region": f"page(s) {pages}", "pages_affected": pages},
        fix_steps=[
            "In Acrobat Pro: Print Production → Flattener Preview → Flatten.",
            "In InDesign/Illustrator: Object → Flatten Transparency (High Resolution).",
            "Re-export as PDF/X-1a after flattening.",
        ],
        fix_tools=["Adobe Acrobat Pro", "Adobe Illustrator", "SparkPrep Auto-Fix (Ghostscript flatten)"],
        est_fix_minutes=4,
        one_click_fix=True,
    )


def check_layers(pdf: pikepdf.Pdf) -> Optional[dict]:
    """Checks for Optional Content Groups (PDF's real name for layers).
    A PDF authored with layers left in (rather than flattened to one
    visible state) can print unpredictably -- some RIPs honor OCG
    visibility, others ignore it and print every layer regardless.
    """
    if "/OCProperties" not in pdf.Root:
        return None
    ocgs = pdf.Root.OCProperties.get("/OCGs", [])
    count = len(ocgs)
    if count == 0:
        return None
    return _finding(
        id="layers_detected",
        severity="fail",
        title=f"PDF contains {count} layer{'s' if count != 1 else ''} — must be flattened",
        why_it_fails=(
            f"This file has {count} Optional Content Group{'s' if count != 1 else ''} "
            "(PDF layers) still present. Print production requires a single flattened "
            "visual state -- some RIP software ignores layer visibility settings and "
            "prints hidden layers anyway, or drops layers that were meant to show."
        ),
        publisher_rule="PDF/X-1a:2001 — 'No optional content / layers permitted'",
        pinpoint={"region": "PDF document catalog", "layer_count": count},
        fix_steps=[
            "In Acrobat Pro: flatten layers before export (Layers panel → Flatten Layers, or export without layers).",
            "In Illustrator/InDesign: merge all layers into one before exporting to PDF.",
        ],
        fix_tools=["Adobe Acrobat Pro", "Adobe Illustrator", "SparkPrep Auto-Fix (Ghostscript flatten)"],
        est_fix_minutes=4,
        one_click_fix=True,
    )


def check_fonts_embedded(pdf: pikepdf.Pdf, max_pages: Optional[int] = None) -> Optional[dict]:
    """Walks every font actually USED to draw text -- not merely declared
    in a page's font resource dictionary -- and checks its FontDescriptor
    for an embedded font program (FontFile/FontFile2/FontFile3).

    This distinction matters: PDF authoring tools (reportlab included)
    routinely leave unused font resources declared on a page (e.g. a
    default Helvetica reference that's selected via a Tf operator but
    never followed by any text-showing operator). Flagging those would
    produce false positives on files that are actually fine. A font only
    matters if the content stream selects it (Tf) and then actually
    draws glyphs with it (Tj, TJ, ' or ") before another font is selected.

    The 14 PDF base fonts (Helvetica, Times, etc.) are never embedded --
    they're resolved from whatever fonts happen to be installed on the
    machine that opens the PDF, which is exactly the kind of
    file-creation-guide violation distributors reject for.
    """
    missing = set()
    show_text_ops = {pikepdf.Operator("Tj"), pikepdf.Operator("TJ"), pikepdf.Operator("'"), pikepdf.Operator('"')}

    pages_to_scan = pdf.pages[:max_pages] if max_pages is not None else pdf.pages
    for page in pages_to_scan:
        resources = page.get("/Resources", {})
        fonts = resources.get("/Font", {})
        if "/Font" not in resources:
            continue

        used_font_keys = set()
        try:
            current_font = None
            for op in pikepdf.parse_content_stream(page):
                if op.operator == pikepdf.Operator("Tf") and op.operands:
                    current_font = op.operands[0]
                elif op.operator in show_text_ops and current_font is not None:
                    used_font_keys.add(str(current_font))
        except Exception:
            # If the content stream can't be parsed, fall back to treating
            # every declared font as used rather than silently skipping
            # this page's font check.
            used_font_keys = {str(k) for k in fonts.keys()}

        for key, font in fonts.items():
            if str(key) not in used_font_keys:
                continue
            base_font = str(font.get("/BaseFont", "unknown")).lstrip("/")
            descriptor = font.get("/FontDescriptor")
            if descriptor is None:
                descendants = font.get("/DescendantFonts")
                if descendants:
                    descriptor = descendants[0].get("/FontDescriptor")
            if descriptor is None:
                missing.add(base_font)
                continue
            embedded = any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3"))
            if not embedded:
                missing.add(base_font)

    if not missing:
        return None

    names = sorted(missing)
    return _finding(
        id="fonts_not_embedded",
        severity="fail",
        title=f"{len(names)} font{'s' if len(names) != 1 else ''} not embedded: {', '.join(names[:5])}{'...' if len(names) > 5 else ''}",
        why_it_fails=(
            "These fonts are referenced by name only, not embedded in the file. "
            "On a printer's RIP (which almost never has your exact fonts installed), "
            "unembedded fonts get substituted with a default font -- text reflows, "
            "line breaks shift, and special characters may render as boxes or blanks."
        ),
        publisher_rule="PDF/X-1a:2001 — 'All fonts must be embedded, including base-14 fonts'",
        pinpoint={"region": "font resources", "unembedded_fonts": names},
        fix_steps=[
            "In Acrobat Pro: File → Properties → Fonts tab to see which aren't embedded.",
            "Re-export from the source app with 'Embed all fonts' (or 'Subset fonts') enabled -- "
            "this includes Helvetica/Times/Arial, which most tools skip embedding by default.",
        ],
        fix_tools=["Adobe Acrobat Pro", "Adobe InDesign", "Adobe Illustrator"],
        est_fix_minutes=6,
        one_click_fix=False,
    )


def check_icc_output_intent(pdf: pikepdf.Pdf, platform_name: str) -> Optional[dict]:
    """Checks whether the declared OutputIntent actually carries an
    embedded ICC profile (not just a named condition string). A
    PDF/X-1a output intent needs either an embedded /DestOutputProfile
    ICC stream or a well-known registry name -- a bare, profile-less
    intent is what most 'PDF/X-1a-ish' exporters produce and it's a
    common silent preflight failure.
    """
    if "/OutputIntents" not in pdf.Root or len(pdf.Root.OutputIntents) == 0:
        return None  # already covered by check_pdfx1a_declared

    intent = pdf.Root.OutputIntents[0]
    has_profile = "/DestOutputProfile" in intent
    if has_profile:
        return None

    registry = str(intent.get("/RegistryName", ""))
    if "color.org" in registry:
        return None  # well-known registry is acceptable without an embedded profile

    return _finding(
        id="icc_profile_missing",
        severity="warning",
        title="Output intent has no embedded ICC profile",
        why_it_fails=(
            "The output intent names a color condition but doesn't embed the actual "
            "ICC color profile (/DestOutputProfile). Without it, printers can't guarantee "
            "which exact CMYK values your colors were intended to produce -- different "
            "presses may interpret the same numbers slightly differently."
        ),
        publisher_rule=f"{platform_name} — recommends embedding U.S. Web Coated (SWOP) v2 ICC profile",
        pinpoint={"region": "OutputIntent dictionary"},
        fix_steps=["Re-export as PDF/X-1a with 'U.S. Web Coated (SWOP) v2' ICC profile embedded, not just named."],
        fix_tools=["Adobe Acrobat Pro", "Adobe InDesign"],
        est_fix_minutes=3,
        one_click_fix=True,
    )


def run_pdf_structure_audit(pdf_path: str, platform_name: str = "your distributor", max_pages: Optional[int] = None) -> List[dict]:
    """Runs all structural checks against a real PDF file and returns
    the combined findings list, ready to merge into deep_audit()'s
    output. Non-PDF files should never reach this -- callers should
    only invoke it when file_metadata['is_pdf'] is true.

    max_pages=1 (or omitted -> defaults applied by callers) is the Basic
    Interior Check -- first page only, what every free/subscription audit
    gets. max_pages=None here means unlimited, which callers should only
    pass for the paid Advanced Interior Check, explicitly capped by the
    caller at ADVANCED_INTERIOR_MAX_PAGES. Layer/PDFX1a-declaration/ICC
    checks are document-level flags, not per-page, so max_pages doesn't
    apply to them.
    """
    findings = []
    try:
        with pikepdf.open(pdf_path) as pdf:
            for check in (
                lambda: check_pdfx1a_declared(pdf, platform_name),
                lambda: check_transparency(pdf, max_pages=max_pages),
                lambda: check_layers(pdf),
                lambda: check_fonts_embedded(pdf, max_pages=max_pages),
                lambda: check_icc_output_intent(pdf, platform_name),
            ):
                result = check()
                if result:
                    findings.append(result)
    except Exception as e:
        findings.append(_finding(
            id="pdf_unreadable",
            severity="fail",
            title="Could not open PDF for structural validation",
            why_it_fails=f"The PDF structure couldn't be parsed: {e}. The file may be corrupt or use unsupported PDF features.",
            publisher_rule="File must be a valid, well-formed PDF",
            fix_steps=["Try re-exporting the PDF from the original source file."],
        ))
    return findings
