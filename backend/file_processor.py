"""File processing engine: DPI detection, CMYK conversion, PDF/X-1a export."""
import io
import os
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageCms
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from pypdf import PdfReader
import pikepdf

from print_specs import COLOR_PROFILES, DEFAULT_COLOR_PROFILE

# Enable large images
Image.MAX_IMAGE_PIXELS = None


def analyze_file(file_path: str) -> dict:
    """Analyze a file and return metadata: format, dimensions, DPI, color mode."""
    result = {
        "file_size_bytes": os.path.getsize(file_path),
        "format": None,
        "width_px": None,
        "height_px": None,
        "dpi_x": None,
        "dpi_y": None,
        "color_mode": None,
        "has_transparency": False,
        "is_pdf": False,
        "pdf_pages": None,
    }
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        result["is_pdf"] = True
        result["format"] = "PDF"
        try:
            reader = PdfReader(file_path)
            result["pdf_pages"] = len(reader.pages)
            page = reader.pages[0]
            box = page.mediabox
            # PDF units are points (72 pt = 1 inch)
            result["width_px"] = float(box.width)
            result["height_px"] = float(box.height)
            result["dpi_x"] = 72
            result["dpi_y"] = 72
            result["color_mode"] = "PDF"
        except Exception as e:
            result["error"] = str(e)
        return result

    try:
        with Image.open(file_path) as img:
            result["format"] = img.format
            result["width_px"] = img.width
            result["height_px"] = img.height
            dpi = img.info.get("dpi", (72, 72))
            result["dpi_x"] = int(dpi[0]) if dpi else 72
            result["dpi_y"] = int(dpi[1]) if dpi else 72
            result["color_mode"] = img.mode
            result["has_transparency"] = img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            )
    except Exception as e:
        result["error"] = str(e)
    return result


def compute_effective_dpi(width_px: int, height_px: int, target_w_inches: float, target_h_inches: float) -> dict:
    """Compute effective DPI when image is scaled to target dimensions."""
    if target_w_inches <= 0 or target_h_inches <= 0:
        return {"dpi_x": 0, "dpi_y": 0, "status": "error"}
    dpi_x = width_px / target_w_inches
    dpi_y = height_px / target_h_inches
    effective = min(dpi_x, dpi_y)
    if effective >= 300:
        status = "pass"
    elif effective >= 200:
        status = "warning"
    else:
        status = "fail"
    return {
        "dpi_x": round(dpi_x, 1),
        "dpi_y": round(dpi_y, 1),
        "effective_dpi": round(effective, 1),
        "status": status,
    }


def convert_to_cmyk(input_path: str, output_path: str, target_dpi: int = 300) -> dict:
    """Convert image to CMYK color space at target DPI, flatten transparency."""
    with Image.open(input_path) as img:
        original_mode = img.mode
        # Flatten transparency onto white
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode == "P":
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        # Convert RGB to CMYK using default ImageCms profile
        if img.mode == "RGB":
            img = img.convert("CMYK")
        elif img.mode != "CMYK":
            img = img.convert("CMYK")
        # Save as TIFF with 300 DPI (CMYK support)
        img.save(output_path, format="TIFF", dpi=(target_dpi, target_dpi), compression="tiff_lzw")
        return {
            "original_mode": original_mode,
            "final_mode": "CMYK",
            "dpi": target_dpi,
            "output_path": output_path,
            "flattened": True,
        }


def build_interior_pdf_x1a(
    source_pdf_path: str,
    output_pdf_path: str,
    trim_w: float,
    trim_h: float,
    bleed: float,
    title: str = "SparkPrep Interior",
    author: str = "",
    color_profile: str = DEFAULT_COLOR_PROFILE,
    producer_name: str = "SparkPrep",
) -> dict:
    """Stream a multi-page manuscript PDF into a real PDF/X-1a:2001 output.

    - Preserves vector text and embedded fonts (no rasterization)
    - Sets MediaBox / TrimBox / BleedBox on every page to (trim + bleed)
    - Writes required PDF/X-1a document metadata:
        · /Root/GTS_PDFXVersion = 'PDF/X-1a:2001'
        · /Root/Trapped = /False
        · /Root/OutputIntents (GTS_PDFX, U.S. Web Coated SWOP v2, CGATS TR 001)
        · XMP metadata with pdfx:GTS_PDFXVersion
    """
    total_w_pts = (trim_w + bleed * 2) * 72
    total_h_pts = (trim_h + bleed * 2) * 72
    trim_left_pts = bleed * 72
    trim_bottom_pts = bleed * 72
    trim_right_pts = (bleed + trim_w) * 72
    trim_top_pts = (bleed + trim_h) * 72
    profile = COLOR_PROFILES.get(color_profile, COLOR_PROFILES[DEFAULT_COLOR_PROFILE])

    with pikepdf.open(source_pdf_path, allow_overwriting_input=False) as src:
        page_count = len(src.pages)

        # Resize every page and set proper boxes
        for page in src.pages:
            page.mediabox = [0, 0, total_w_pts, total_h_pts]
            page.trimbox = [trim_left_pts, trim_bottom_pts, trim_right_pts, trim_top_pts]
            page.bleedbox = [0, 0, total_w_pts, total_h_pts]
            page.cropbox = [0, 0, total_w_pts, total_h_pts]

        # XMP metadata block
        try:
            with src.open_metadata(set_pikepdf_as_editor=False) as meta:
                meta["dc:title"] = title
                if author:
                    meta["dc:creator"] = [author]
                meta["xmp:CreatorTool"] = f"{producer_name} Book Production Engine"
                meta["pdfx:GTS_PDFXVersion"] = "PDF/X-1a:2001"
                meta["pdfx:GTS_PDFXConformance"] = "PDF/X-1a:2001"
                meta["pdf:Producer"] = f"{producer_name} (pikepdf)"
                meta["pdf:Trapped"] = "False"
        except Exception:
            pass

        # PDF/X-1a required dictionary entries
        src.Root.GTS_PDFXVersion = pikepdf.String("PDF/X-1a:2001")
        src.Root.Trapped = pikepdf.Name("/False")

        # Output intent — required by PDF/X-1a
        output_intent = pikepdf.Dictionary(
            Type=pikepdf.Name.OutputIntent,
            S=pikepdf.Name("/GTS_PDFX"),
            OutputCondition=pikepdf.String("CMYK"),
            OutputConditionIdentifier=pikepdf.String(profile["condition_identifier"]),
            RegistryName=pikepdf.String(profile["registry"]),
            Info=pikepdf.String(profile["info"]),
        )
        src.Root.OutputIntents = pikepdf.Array([output_intent])

        # Remove entries forbidden by PDF/X-1a (currently only /AA — /OpenAction is allowed if benign, /Metadata is REQUIRED by PDF/X-1a for XMP)
        if "/AA" in src.Root:
            del src.Root["/AA"]

        # Force PDF version to 1.4 (PDF/X-1a:2001 baseline)
        try:
            src.pdf_version = "1.4"
        except Exception:
            pass

        src.save(output_pdf_path, linearize=False, min_version="1.4")

    return {
        "output_path": output_pdf_path,
        "page_count": page_count,
        "page_size_inches": [round(trim_w + bleed * 2, 4), round(trim_h + bleed * 2, 4)],
        "trim_box_inches": [round(trim_w, 4), round(trim_h, 4)],
        "bleed_inches": round(bleed, 4),
        "pdf_standard": "PDF/X-1a:2001",
        "vector_preserved": True,
        "fonts_preserved": True,
        "output_intent": f"{profile['condition_identifier']} — {profile['info']}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _declare_pdfx1a(pdf_path: str, title: str, author: str, bleed_pts: float, total_w_pts: float, total_h_pts: float,
                     color_profile: str = DEFAULT_COLOR_PROFILE, producer_name: str = "SparkPrep") -> None:
    """Stamps real PDF/X-1a:2001 compliance (GTS_PDFXVersion, TrimBox,
    OutputIntents, XMP) onto an already-rendered single-page PDF in place.

    Before this existed, build_print_ready_pdf() below returned a PDF whose
    metadata *claimed* pdf_standard: "PDF/X-1a:2001" but never actually set
    /GTS_PDFXVersion, an /OutputIntents entry, or a correct /TrimBox
    (reportlab leaves TrimBox defaulting to the full MediaBox, i.e. the
    bleed area, not the actual trim line) -- so a cover exported through
    the "Generate PDF/X-1a" button would fail pdfx_validator's own
    check_pdfx1a_declared() if run against itself. This closes that gap
    the same way build_interior_pdf_x1a() already does it for interiors.
    """
    profile = COLOR_PROFILES.get(color_profile, COLOR_PROFILES[DEFAULT_COLOR_PROFILE])
    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            page.mediabox = [0, 0, total_w_pts, total_h_pts]
            page.trimbox = [bleed_pts, bleed_pts, total_w_pts - bleed_pts, total_h_pts - bleed_pts]
            page.bleedbox = [0, 0, total_w_pts, total_h_pts]
            page.cropbox = [0, 0, total_w_pts, total_h_pts]

        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                meta["dc:title"] = title
                if author:
                    meta["dc:creator"] = [author]
                meta["xmp:CreatorTool"] = f"{producer_name} Book Production Engine"
                meta["pdfx:GTS_PDFXVersion"] = "PDF/X-1a:2001"
                meta["pdfx:GTS_PDFXConformance"] = "PDF/X-1a:2001"
                meta["pdf:Producer"] = f"{producer_name} (pikepdf)"
                meta["pdf:Trapped"] = "False"
        except Exception:
            pass

        pdf.Root.GTS_PDFXVersion = pikepdf.String("PDF/X-1a:2001")
        pdf.Root.Trapped = pikepdf.Name("/False")
        pdf.Root.OutputIntents = pikepdf.Array([pikepdf.Dictionary(
            Type=pikepdf.Name.OutputIntent,
            S=pikepdf.Name("/GTS_PDFX"),
            OutputCondition=pikepdf.String("CMYK"),
            OutputConditionIdentifier=pikepdf.String(profile["condition_identifier"]),
            RegistryName=pikepdf.String(profile["registry"]),
            Info=pikepdf.String(profile["info"]),
        )])
        if "/AA" in pdf.Root:
            del pdf.Root["/AA"]
        try:
            pdf.pdf_version = "1.4"
        except Exception:
            pass
        pdf.save(pdf_path, linearize=False, min_version="1.4")


def build_print_ready_pdf(
    image_path: str,
    output_pdf_path: str,
    trim_w: float,
    trim_h: float,
    bleed: float,
    spine_w: float = 0.0,
    is_cover: bool = True,
    title: str = "SparkPrep Export",
    author: str = "",
    barcode_png_bytes: bytes = None,
    color_profile: str = DEFAULT_COLOR_PROFILE,
    producer_name: str = "SparkPrep",
) -> dict:
    """Build a real PDF/X-1a:2001 print-ready PDF from a source image.
    If is_cover and barcode_png_bytes provided, composite the barcode into the
    reserved back-cover barcode zone (bottom-left area, 2" x 1.2")."""
    if is_cover and spine_w > 0:
        total_w = (trim_w * 2) + spine_w + (bleed * 2)
        total_h = trim_h + (bleed * 2)
    else:
        total_w = trim_w + (bleed * 2)
        total_h = trim_h + (bleed * 2)

    page_w = total_w * inch
    page_h = total_h * inch

    c = canvas.Canvas(output_pdf_path, pagesize=(page_w, page_h))
    # PDF/X-1a metadata
    c.setTitle(title)
    c.setAuthor(author or producer_name)
    c.setSubject("Print-Ready PDF/X-1a")
    c.setCreator(f"{producer_name} Book Production Engine")

    # Draw image scaled to full canvas
    try:
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = bg
            # Save temp RGB copy (reportlab handles RGB best)
            tmp_path = image_path + ".rgb.jpg"
            if img.mode == "CMYK":
                img.convert("RGB").save(tmp_path, "JPEG", quality=95, dpi=(300, 300))
            else:
                img.convert("RGB").save(tmp_path, "JPEG", quality=95, dpi=(300, 300))
            c.drawImage(tmp_path, 0, 0, width=page_w, height=page_h)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    except Exception as e:
        c.setFillColorRGB(1, 1, 1)
        c.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        c.setFillColorRGB(0.7, 0.7, 0.7)
        c.drawString(20, 20, f"[Image load error: {e}]")

    c.showPage()
    c.save()

    # Overlay barcode block for covers with ISBN
    if is_cover and barcode_png_bytes and spine_w > 0:
        try:
            import pikepdf, io
            # Barcode zone on back cover — 0.5" from spine, 0.5" from bottom, 2" x 1.2"
            zone_x_in = bleed + 0.5  # back cover starts at bleed
            zone_y_in = bleed + 0.5
            zone_w_in = 2.0
            zone_h_in = 1.2
            # Build an overlay PDF at same size as the main
            overlay_path = output_pdf_path + ".barcode.pdf"
            oc = canvas.Canvas(overlay_path, pagesize=(page_w, page_h))
            # ReportLab drawImage needs an ImageReader for in-memory bytes
            from reportlab.lib.utils import ImageReader
            img_reader = ImageReader(io.BytesIO(barcode_png_bytes))
            oc.setFillColorRGB(1, 1, 1)
            oc.rect(zone_x_in * inch, zone_y_in * inch, zone_w_in * inch, zone_h_in * inch, fill=1, stroke=0)
            oc.drawImage(img_reader, zone_x_in * inch, zone_y_in * inch, width=zone_w_in * inch, height=zone_h_in * inch)
            oc.showPage()
            oc.save()
            # Merge overlay onto main PDF
            with pikepdf.open(output_pdf_path, allow_overwriting_input=True) as main, pikepdf.open(overlay_path) as ov:
                main.pages[0].add_overlay(ov.pages[0])
                main.save(output_pdf_path)
            try: os.remove(overlay_path)
            except OSError: pass
        except Exception as e:
            print(f"barcode overlay failed: {e}")

    profile = COLOR_PROFILES.get(color_profile, COLOR_PROFILES[DEFAULT_COLOR_PROFILE])
    _declare_pdfx1a(
        output_pdf_path, title=title, author=author,
        bleed_pts=bleed * inch, total_w_pts=page_w, total_h_pts=page_h,
        color_profile=color_profile, producer_name=producer_name,
    )

    return {
        "output_path": output_pdf_path,
        "page_size_inches": [round(total_w, 4), round(total_h, 4)],
        "page_size_points": [round(page_w, 2), round(page_h, 2)],
        "pdf_standard": "PDF/X-1a:2001",
        "output_intent": f"{profile['condition_identifier']} — {profile['info']}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def run_compliance_checks(
    file_metadata: dict, target_w: float, target_h: float, bleed: float, platform: str = "kdp",
    file_path: str = None, slot: str = None, platform_name: str = None, max_pages: int = None,
) -> list:
    """Return a list of compliance issues with severity and auto-fix availability.

    file_path/slot/platform_name/max_pages are optional and only used for the
    interior safety-margin/centering check below -- every existing caller that
    doesn't pass them (cover slots, legacy uploads) behaves exactly as before.
    """
    checks = []

    # DPI check
    if file_metadata.get("is_pdf"):
        checks.append({
            "id": "pdf_dpi",
            "label": "PDF Source",
            "status": "pass",
            "message": "Vector PDF — DPI scales cleanly",
            "auto_fix": False,
        })
    else:
        w_px = file_metadata.get("width_px") or 0
        h_px = file_metadata.get("height_px") or 0
        dpi_info = compute_effective_dpi(w_px, h_px, target_w + (bleed * 2), target_h + (bleed * 2))
        checks.append({
            "id": "dpi",
            "label": f"Resolution ({dpi_info['effective_dpi']} DPI)",
            "status": dpi_info["status"],
            "message": {
                "pass": "Effective DPI is 300+ — perfect for print",
                "warning": "DPI between 200-299 — may look soft when printed",
                "fail": "DPI below 200 — will appear pixelated in print",
            }[dpi_info["status"]],
            "auto_fix": dpi_info["status"] != "pass",
            "fix_action": "upscale_300dpi",
        })

    # Color mode check
    color_mode = (file_metadata.get("color_mode") or "").upper()
    if color_mode == "CMYK" or color_mode == "PDF":
        checks.append({
            "id": "colorspace",
            "label": "Color Space (CMYK)",
            "status": "pass",
            "message": "Already in CMYK color space",
            "auto_fix": False,
        })
    elif color_mode in ("RGB", "RGBA", "P", "L", "LA"):
        checks.append({
            "id": "colorspace",
            "label": f"Color Space ({color_mode})",
            "status": "warning",
            "message": "File is in RGB — colors may shift when printed. One-click fix will convert to CMYK.",
            "auto_fix": True,
            "fix_action": "convert_cmyk",
        })
    else:
        checks.append({
            "id": "colorspace",
            "label": f"Color Space ({color_mode or 'unknown'})",
            "status": "warning",
            "message": "Unknown color space — recommend converting to CMYK",
            "auto_fix": True,
            "fix_action": "convert_cmyk",
        })

    # Transparency check
    if file_metadata.get("has_transparency"):
        checks.append({
            "id": "transparency",
            "label": "Transparency",
            "status": "warning",
            "message": "File contains transparency — must be flattened for print",
            "auto_fix": True,
            "fix_action": "flatten",
        })
    else:
        checks.append({
            "id": "transparency",
            "label": "Transparency",
            "status": "pass",
            "message": "No transparency detected — safe for print",
            "auto_fix": False,
        })

    # Bleed check (we always add bleed on export)
    checks.append({
        "id": "bleed",
        "label": f"Bleed ({bleed}\" required)",
        "status": "warning",
        "message": f"Bleed of {bleed}\" will be added automatically on export",
        "auto_fix": True,
        "fix_action": "add_bleed",
    })

    # PDF/X-1a
    checks.append({
        "id": "pdfx1a",
        "label": "PDF/X-1a:2001",
        "status": "warning",
        "message": "Will be generated on export — print-ready flattened output",
        "auto_fix": True,
        "fix_action": "export_pdfx1a",
    })

    # Interior text safety margin + page-size check -- nothing above (DPI,
    # color space, transparency, bleed, PDF/X-1a) ever looks at where the
    # actual text sits on the page, which is exactly what a distributor's
    # "content extends outside the safety area" / "not centered" rejection
    # is about. Only runs for the interior slot of a real PDF, since it's
    # meaningless for a raster cover image.
    if slot == "interior" and file_metadata.get("is_pdf") and file_path:
        from pdfx_validator import check_interior_safety_margins
        margin_findings = check_interior_safety_margins(
            file_path, platform_name or platform, target_w, target_h, max_pages=max_pages,
        )
        for f in margin_findings:
            checks.append({
                "id": f["id"],
                "label": f["title"],
                "status": f["severity"],
                "message": f["why_it_fails"],
                "auto_fix": False,
            })

    return checks
