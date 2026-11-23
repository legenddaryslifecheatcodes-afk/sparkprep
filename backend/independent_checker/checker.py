"""The checks themselves. Standard library + pypdf + Pillow + numpy only."""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Optional

# Printer rules, from the printers' own published specs (not copied from the
# main scanner's settings):
MIN_DPI = 300                    # KDP / IngramSpark: images 300 DPI at print size
SIZE_TOLERANCE_IN = 0.07         # a little exporter rounding
MIN_INTERIOR_MARGIN_IN = 0.25    # KDP's smallest allowed margin from any trim edge
MARGIN_TOLERANCE_IN = 0.02


def _check(id_: str, label: str, passed: Optional[bool], evidence: str) -> dict:
    """passed=None means "not something this checker can judge for this file"."""
    return {"id": id_, "label": label, "passed": passed, "evidence": evidence}


# ------------------------------------------------------------------ images
def check_image(path: str, expect: dict) -> list:
    from PIL import Image
    import numpy as np

    out = []
    try:
        with Image.open(path) as img:
            img.load()
            w, h, mode = img.width, img.height, img.mode
            arr = None
            if mode == "CMYK":
                small = img.copy()
                small.thumbnail((1200, 1200))
                arr = np.asarray(small, dtype=np.uint16)
    except Exception as e:  # noqa: BLE001
        return [_check("opens", "File opens", False, f"{type(e).__name__}: {e}")]
    out.append(_check("opens", "File opens", True, f"{w}×{h}px, {mode}"))

    w_in, h_in = expect.get("w_in"), expect.get("h_in")
    if w_in and h_in:
        dpi = min(w / w_in, h / h_in)
        out.append(_check("resolution", "Resolution for print", dpi >= MIN_DPI - 0.5,
                          f"{dpi:.0f} DPI at {w_in:g}\"×{h_in:g}\" (needs {MIN_DPI})"))
        ratio_file, ratio_book = w / h, w_in / h_in
        out.append(_check("page_size", "Shape matches the book", abs(ratio_file / ratio_book - 1) <= 0.01,
                          f"file {ratio_file:.4f} vs book {ratio_book:.4f} width÷height"))
    out.append(_check("color_cmyk", "Colours are print colours (CMYK)", mode == "CMYK", f"image mode {mode}"))

    limit = expect.get("ink_limit_pct")
    if arr is not None and limit:
        peak = float(arr.sum(axis=-1).max()) / 255 * 100
        out.append(_check("ink_limit", "Total ink within the printer's limit", peak <= limit + 1.0,
                          f"peak {peak:.0f}% (limit {limit:g}%)"))
    return out


# ------------------------------------------------------------------ PDFs
_SHOW_OPS = {b"Tj", b"TJ", b"'", b'"'}


def _resolve(obj):
    return obj.get_object() if hasattr(obj, "get_object") else obj


def _font_embedded(font) -> bool:
    font = _resolve(font)
    if font.get("/Subtype") == "/Type3":
        return True  # glyphs are drawn by the file itself
    desc = font.get("/FontDescriptor")
    if desc is None and font.get("/DescendantFonts"):
        desc = _resolve(_resolve(font["/DescendantFonts"])[0]).get("/FontDescriptor")
    if desc is None:
        return False
    desc = _resolve(desc)
    return any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))


def _walk_content(reader, contents, resources, seen: set, report: dict, depth: int = 0) -> None:
    """Fonts actually used to draw text, plus transparency and colour use,
    following Form XObjects (and their own resources) as they're drawn."""
    from pypdf.generic import ContentStream

    resources = _resolve(resources) or {}
    fonts = _resolve(resources.get("/Font")) or {}
    xobjects = _resolve(resources.get("/XObject")) or {}
    gstates = _resolve(resources.get("/ExtGState")) or {}
    for name, gs in gstates.items():
        gs = _resolve(gs)
        smask = gs.get("/SMask")
        if (smask is not None and smask != "/None") or float(gs.get("/CA", 1)) < 1 or float(gs.get("/ca", 1)) < 1:
            report["transparency"].add(f"graphics state {name}")
        bm = gs.get("/BM")
        if bm not in (None, "/Normal", "/Compatible"):
            report["transparency"].add(f"blend mode {bm}")
    for cs in (_resolve(resources.get("/ColorSpace")) or {}).values():
        if _is_rgb(cs):
            report["rgb"].add("colour space")
    try:
        ops = ContentStream(contents, reader).operations
    except Exception:  # noqa: BLE001 - unreadable stream: count every font as used
        ops = []
        for name in fonts.keys():
            report["fonts_used"].append((name, fonts[name]))
    current = None
    for operands, op in ops:
        if op == b"Tf" and operands:
            current = operands[0]
        elif op in _SHOW_OPS and current is not None and current in fonts:
            report["fonts_used"].append((current, fonts[current]))
        elif op in (b"rg", b"RG"):
            report["rgb"].add("RGB fill/stroke")
        elif op == b"Do" and operands and operands[0] in xobjects and depth < 12:
            xo = _resolve(xobjects[operands[0]])
            key = id(xo)
            if key in seen:
                continue
            seen.add(key)
            sub = xo.get("/Subtype")
            if sub == "/Image":
                report["images"] += 1
                if xo.get("/SMask") is not None:
                    report["transparency"].add("image with soft mask")
                if _is_rgb(xo.get("/ColorSpace")):
                    report["rgb"].add("RGB image")
            elif sub == "/Form":
                grp = _resolve(xo.get("/Group"))
                if grp is not None and grp.get("/S") == "/Transparency":
                    report["transparency"].add("transparency group")
                _walk_content(reader, xo, xo.get("/Resources") or resources, seen, report, depth + 1)


def _is_rgb(cs) -> bool:
    cs = _resolve(cs)
    if cs is None:
        return False
    if cs in ("/DeviceRGB", "/CalRGB"):
        return True
    try:
        if cs[0] == "/ICCBased":
            return int(_resolve(cs[1]).get("/N", 0)) == 3
        if cs[0] in ("/Indexed", "/Separation", "/DeviceN"):
            return _is_rgb(cs[1] if cs[0] == "/Indexed" else cs[2])
    except Exception:  # noqa: BLE001
        return False
    return False


def _ghostscript() -> Optional[str]:
    return shutil.which("gs")


def _ink_boxes(path: str, pages: int) -> Optional[list]:
    """Where ink really lands on each page, measured by Ghostscript rendering
    it (bbox device): one (x0, y0, x1, y1) in points per page, None if blank."""
    gs = _ghostscript()
    if not gs:
        return None
    r = subprocess.run([gs, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=bbox",
                        f"-dLastPage={pages}", path], capture_output=True, text=True, timeout=120)
    boxes = re.findall(r"%%HiResBoundingBox: ([\d.\-]+) ([\d.\-]+) ([\d.\-]+) ([\d.\-]+)", r.stderr)
    out = [tuple(float(v) for v in b) for b in boxes]
    return [None if (b[2] - b[0] <= 0 or b[3] - b[1] <= 0) else b for b in out]


def check_pdf(path: str, expect: dict) -> list:
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            return [_check("opens", "File opens", False, "file is password-protected")]
        pages = reader.pages
        n = len(pages)
    except Exception as e:  # noqa: BLE001
        return [_check("opens", "File opens", False, f"{type(e).__name__}: {e}")]
    out = [_check("opens", "File opens", n > 0, f"{n} page(s), read with pypdf")]
    if not n:
        return out
    max_pages = min(n, int(expect.get("max_pages") or n))

    report = {"fonts_used": [], "transparency": set(), "rgb": set(), "images": 0}
    page_has_images = []
    for i in range(max_pages):
        page = pages[i]
        before = report["images"]
        grp = _resolve(page.get("/Group"))
        if grp is not None and grp.get("/S") == "/Transparency":
            report["transparency"].add(f"page {i + 1} transparency group")
        contents = page.get_contents()
        if contents is not None:
            _walk_content(reader, contents, page.get("/Resources") or {}, set(), report)
        page_has_images.append(report["images"] > before)

    missing = sorted({str(_resolve(f).get("/BaseFont", name)).lstrip("/") for name, f in report["fonts_used"]
                      if not _font_embedded(f)})
    used = sorted({str(_resolve(f).get("/BaseFont", name)).lstrip("/") for name, f in report["fonts_used"]})
    out.append(_check("fonts", "Every font is embedded", not missing,
                      f"not embedded: {', '.join(missing)}" if missing else f"{len(used)} font(s), all embedded"))

    root = reader.trailer["/Root"]
    version = root.get("/GTS_PDFXVersion")
    intents = _resolve(root.get("/OutputIntents")) or []
    out.append(_check("pdfx", "Marked as PDF/X-1a with an output intent", bool(version) and len(intents) > 0,
                      f"GTS_PDFXVersion={version}, output intents={len(intents)}"))
    profile_ok, profile_ev = False, "no output intent"
    if intents:
        intent = _resolve(intents[0])
        if intent.get("/DestOutputProfile") is not None:
            profile_ok, profile_ev = True, "ICC profile embedded"
        elif "color.org" in str(intent.get("/RegistryName", "")) and intent.get("/OutputConditionIdentifier"):
            profile_ok, profile_ev = True, f"registered condition {intent.get('/OutputConditionIdentifier')}"
        else:
            profile_ev = "output intent has no profile and no registered condition"
    out.append(_check("color_profile", "Print colour profile present", profile_ok, profile_ev))

    out.append(_check("transparency", "No live transparency", not report["transparency"],
                      "; ".join(sorted(report["transparency"])[:4]) or "none found"))
    out.append(_check("layers", "No layers", "/OCProperties" not in root,
                      "layers (optional content) present" if "/OCProperties" in root else "none"))
    out.append(_check("color_cmyk", "No RGB colour", not report["rgb"],
                      "; ".join(sorted(report["rgb"])) or "none found"))

    out.extend(_pdf_geometry(path, pages, max_pages, page_has_images, expect))
    return out


def _pdf_geometry(path, pages, max_pages, page_has_images, expect) -> list:
    trim_w, trim_h = expect.get("trim_w_in"), expect.get("trim_h_in")
    if not (trim_w and trim_h):
        return []
    bleed = float(expect.get("bleed_in") or 0)
    allowed = [(trim_w, trim_h)] + ([(trim_w + bleed, trim_h + 2 * bleed), (trim_w + 2 * bleed, trim_h + 2 * bleed)] if bleed else [])
    wrong = []
    for i in range(max_pages):
        mb = pages[i].mediabox
        w, h = float(mb.width) / 72, float(mb.height) / 72
        if not any(abs(w - aw) <= SIZE_TOLERANCE_IN and abs(h - ah) <= SIZE_TOLERANCE_IN for aw, ah in allowed):
            wrong.append(f"p{i + 1} {w:.3f}\"×{h:.3f}\"")
    out = [_check("page_size", "Page size matches the trim", not wrong,
                  ("; ".join(wrong[:4])) if wrong else f"{max_pages} page(s) at {trim_w:g}\"×{trim_h:g}\" (±bleed)")]
    if not expect.get("check_margins") or wrong:
        return out

    boxes = _ink_boxes(path, max_pages)
    if boxes is None:
        return out + [_check("margins", "Content inside the safe area", None, "Ghostscript not available")]
    tight, measured = [], 0
    for i, box in enumerate(boxes[:max_pages]):
        if box is None or page_has_images[i]:
            continue  # blank page, or art that may bleed on purpose
        measured += 1
        mb = pages[i].mediabox
        pw, ph = float(mb.width) / 72, float(mb.height) / 72
        x0, y0 = (box[0] - float(mb.left)) / 72, (box[1] - float(mb.bottom)) / 72
        x1, y1 = (box[2] - float(mb.left)) / 72, (box[3] - float(mb.bottom)) / 72
        extra_w, extra_h = pw - trim_w, ph - trim_h
        bottom = extra_h / 2
        best = None
        for left in {0.0, extra_w, extra_w / 2}:   # bleed on the outside edge of either page side, or centered
            m = min(x0 - left, (left + trim_w) - x1, y0 - bottom, (bottom + trim_h) - y1)
            best = m if best is None else max(best, m)
        if best < MIN_INTERIOR_MARGIN_IN - MARGIN_TOLERANCE_IN:
            tight.append(f"p{i + 1} {best:.2f}\"")
    if not measured:
        return out + [_check("margins", "Content inside the safe area", None, "no text-only pages to measure")]
    return out + [_check("margins", "Content inside the safe area", not tight,
                         ("closest: " + "; ".join(tight[:4])) if tight else
                         f"{measured} page(s): all ink at least {MIN_INTERIOR_MARGIN_IN}\" from the trim")]


def run(path: str, kind: str, expect: dict) -> dict:
    checks = check_pdf(path, expect) if kind == "pdf" else check_image(path, expect)
    return {"checker": "sparkprep-independent-checker/1", "checks": checks,
            "ok": all(c["passed"] is not False for c in checks)}
