"""Suggested fixes: SparkPrep's best attempt at problems that normally need the
author's own judgement. The customer sees a before/after and either keeps the
fix or declines it (and can upload their own). Either way the full check runs
again on whatever file they keep. (trey, 2026-11-24)

Each fix works on a picture of the cover. A PDF cover is turned into a
400 DPI picture first and saved back as a PDF at its page size -- the summary
says so, because the customer is judging the result.

Fixes:
  cover_size                fit a cover that's a little the wrong shape:
                            up to 2% stretch (not visible), the rest by
                            extending (or trimming) the cover's own outer edges
  cover_template_leftovers  paint out the template's pink/blue guide boxes and
                            printed template labels from the surrounding art
  cover_spine_text_forbidden  clear text from a spine too thin to carry it
  cover_isbn_mismatch       cover the old barcode with this book's barcode
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageFilter

PDF_RASTER_DPI = 400
MAX_INVISIBLE_STRETCH = 0.02     # 2%: a change in proportions nobody can see on a cover
MAX_SHAPE_FIX = 0.08             # beyond 8% off, extending the edges would show -- rebuild instead

SUPPORTED = ("cover_size", "cover_template_leftovers", "cover_spine_text_forbidden", "cover_isbn_mismatch")


class NotOffered(Exception):
    """This file can't get a suggested fix for this problem; the message says why, in plain words."""


# ------------------------------------------------------------------ in / out
def load(path: str) -> tuple[Image.Image, dict]:
    """The cover as an RGB picture, plus what's needed to save it back the same way."""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        import fitz
        with fitz.open(str(p)) as doc:
            page = doc[0]
            w_pt, h_pt = page.rect.width, page.rect.height
            pix = page.get_pixmap(dpi=PDF_RASTER_DPI, colorspace=fitz.csRGB, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return img, {"pdf": True, "w_in": w_pt / 72.0, "h_in": h_pt / 72.0, "dpi": PDF_RASTER_DPI}
    with Image.open(p) as im:
        im.load()
        dpi = (im.info.get("dpi") or (300, 300))[0] or 300
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            img = bg
        else:
            img = im.convert("RGB")
    return img, {"pdf": False, "dpi": float(dpi)}


def save(img: Image.Image, info: dict, out_base: str, size_in: Optional[tuple] = None) -> str:
    """Save next to out_base (no extension): a PDF cover stays a PDF (at size_in or its own page size),
    a picture becomes a PNG at 300+ DPI (Auto-Fix's CMYK step runs on it later as usual)."""
    if info["pdf"]:
        import fitz
        w_in, h_in = size_in or (info["w_in"], info["h_in"])
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        doc = fitz.open()
        page = doc.new_page(width=w_in * 72.0, height=h_in * 72.0)
        page.insert_image(page.rect, stream=buf.getvalue())
        out = out_base + ".pdf"
        doc.save(out, deflate=True)
        doc.close()
        return out
    out = out_base + ".png"
    dpi = info.get("dpi") or 300
    if size_in:
        dpi = img.width / size_in[0]
    img.save(out, format="PNG", dpi=(dpi, dpi))
    return out


def _inpaint(img: Image.Image, mask: np.ndarray, radius: int = 6) -> Image.Image:
    import cv2
    arr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
    m = (mask > 0).astype(np.uint8) * 255
    out = cv2.inpaint(arr, m, radius, cv2.INPAINT_TELEA)
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))


def _ocr_boxes(img: Image.Image, scale: float = 1.0) -> list:
    """(text, x0, y0, x1, y1) in img pixels for every word tesseract reads. Empty if OCR isn't available."""
    try:
        import pytesseract
    except Exception:
        return []
    small = img if scale == 1.0 else img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    try:
        d = pytesseract.image_to_data(small, output_type=pytesseract.Output.DICT)
    except Exception:
        return []
    out = []
    for i, t in enumerate(d.get("text") or []):
        t = (t or "").strip()
        if not t:
            continue
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        out.append((t, int(x / scale), int(y / scale), int((x + w) / scale), int((y + h) / scale)))
    return out


# ------------------------------------------------------------------ the fixes
def fit_cover_shape(img: Image.Image, target_w_in: float, target_h_in: float) -> tuple[Image.Image, str]:
    """No squashing: at most a 2% stretch, then extend (or trim) the outer edges for the rest."""
    w, h = img.size
    need = target_w_in / target_h_in
    have = w / h
    off = need / have - 1.0                      # + = cover needs to be wider, - = narrower
    if abs(off) < 1e-4:
        raise NotOffered("This cover is already the right shape.")
    if abs(off) > MAX_SHAPE_FIX:
        raise NotOffered(f"This cover is {abs(off) * 100:.0f}% off the right shape. Fixing that much would show, "
                         "so it needs to be rebuilt at the right size.")
    stretch = max(-MAX_INVISIBLE_STRETCH, min(MAX_INVISIBLE_STRETCH, off))
    new_w = max(1, round(w * (1 + stretch)))
    out = img.resize((new_w, h), Image.LANCZOS)
    final_w = round(h * need)
    if final_w > new_w:                          # still too narrow: extend both outer edges
        add = final_w - new_w
        left, right = add // 2, add - add // 2
        arr = np.asarray(out)
        padded = np.pad(arr, ((0, 0), (left, right), (0, 0)), mode="reflect")
        ext = Image.fromarray(padded)
        # Soften only the new strips so a mirrored edge reads as background, not a copy.
        blur = ext.filter(ImageFilter.GaussianBlur(radius=max(2, h // 300)))
        mask = np.zeros((h, final_w), dtype=np.uint8)
        mask[:, :left] = 255
        mask[:, final_w - right:] = 255
        ext.paste(blur, (0, 0), Image.fromarray(mask))
        out = ext
        how = f"stretched {abs(stretch) * 100:.1f}% (not visible) and extended the left and right edges by {add} pixels"
    elif final_w < new_w:                        # still too wide: trim the outer edges evenly
        cut = new_w - final_w
        left = cut // 2
        out = out.crop((left, 0, left + final_w, h))
        how = f"narrowed {abs(stretch) * 100:.1f}% (not visible) and trimmed {cut} pixels evenly off the outer edges"
    else:
        how = f"adjusted the shape by {abs(stretch) * 100:.1f}% (not visible)"
    return out, (f"Your cover was {abs(off) * 100:.1f}% off the right shape. SparkPrep {how}, "
                 "so nothing on it is squashed. Please check the left and right edges.")


def remove_template_guides(img: Image.Image) -> tuple[Image.Image, str]:
    from pdfx_validator import _TEMPLATE_PINK, _TEMPLATE_BLUE, _TEMPLATE_COLOR_TOL, _TEMPLATE_WORDS
    import cv2
    arr = np.asarray(img).astype(np.int16)
    tol = _TEMPLATE_COLOR_TOL + 3
    mask = np.zeros(arr.shape[:2], dtype=np.uint8)
    for rgb in (_TEMPLATE_PINK, _TEMPLATE_BLUE):
        mask |= ((np.abs(arr - np.array(rgb, dtype=np.int16)).max(axis=-1) <= tol).astype(np.uint8))
    box_px = int(mask.sum())
    words = 0
    scale = min(1.0, 2400 / max(img.size))
    for text, x0, y0, x1, y1 in _ocr_boxes(img, scale):
        t = "".join(ch for ch in text.lower() if ch.isalpha())
        if len(t) >= 4 and any(t == k or (len(t) >= 6 and k in t) for k in _TEMPLATE_WORDS):
            pad = max(2, (y1 - y0) // 4)
            mask[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad] = 1
            words += 1
    if not mask.any():
        raise NotOffered("SparkPrep couldn't find the template's guide boxes or labels to remove on this file.")
    mask = cv2.dilate(mask * 255, np.ones((5, 5), np.uint8), iterations=1)
    out = _inpaint(img, mask, radius=max(4, min(img.size) // 400))
    share = box_px / (img.width * img.height)
    parts = []
    if box_px:
        parts.append(f"the pink and blue guide boxes ({share * 100:.1f}% of the cover)")
    if words:
        parts.append(f"{words} printed template label{'s' if words != 1 else ''}")
    return out, ("SparkPrep painted out " + " and ".join(parts) + " using the art around them. "
                 "Please look closely where they were, especially if a box sat over a photo or text.")


def clear_spine_text(img: Image.Image, img_w_in: float, spine_x_in: float, spine_w_in: float) -> tuple[Image.Image, str]:
    """Paint over everything that stands out from the spine's own background colour."""
    import cv2
    px_per_in = img.width / img_w_in
    x0 = max(0, int(spine_x_in * px_per_in) - 2)
    x1 = min(img.width, int((spine_x_in + spine_w_in) * px_per_in) + 2)
    if x1 - x0 < 2:
        raise NotOffered("The spine is too thin to find on this file.")
    arr = np.asarray(img).astype(np.int16)
    band = arr[:, x0:x1]
    bg = np.median(band.reshape(-1, 3), axis=0)
    diff = np.abs(band - bg).max(axis=-1)
    mask = np.zeros(arr.shape[:2], dtype=np.uint8)
    mask[:, x0:x1] = (diff > 40).astype(np.uint8)
    if not mask.any():
        raise NotOffered("SparkPrep didn't find any text on the spine to clear.")
    mask = cv2.dilate(mask * 255, np.ones((3, 3), np.uint8), iterations=2)
    mask[:, :x0] = 0
    mask[:, x1:] = 0
    out = _inpaint(img, mask, radius=4)
    return out, ("Your book is too thin for spine text, so SparkPrep cleared the spine using its own background colour. "
                 "Your title and name stay on the front cover.")


def replace_barcode(img: Image.Image, isbn: str, img_w_in: float, back_x_in: float = 0.0,
                    back_w_in: Optional[float] = None) -> tuple[Image.Image, str]:
    """Find the old barcode (or the old ISBN text) on the back cover and put this book's barcode over it."""
    import cv2
    from barcode_engine import generate_barcode_pil, normalize_isbn
    info = normalize_isbn(isbn) if isbn else None
    digits = info["isbn"] if info and info.get("valid") else None
    if not digits:
        raise NotOffered("Add this book's ISBN in SparkPrep first, then SparkPrep can put the right barcode on.")
    px_per_in = img.width / img_w_in
    region = None
    try:
        det = cv2.barcode.BarcodeDetector()
        bgr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
        ok, pts = det.detect(bgr)
        if ok and pts is not None and len(pts):
            p = np.asarray(pts).reshape(-1, 2)
            region = [int(p[:, 0].min()), int(p[:, 1].min()), int(p[:, 0].max()), int(p[:, 1].max())]
    except Exception:
        region = None
    if region is None:
        for text, x0, y0, x1, y1 in _ocr_boxes(img, min(1.0, 2400 / max(img.size))):
            d = "".join(ch for ch in text if ch.isdigit())
            if len(d) >= 10:
                h = y1 - y0
                region = [x0, y0 - int(h * 6), x1, y1 + int(h * 1.5)]
                break
    if region is None:
        raise NotOffered("SparkPrep couldn't find the old barcode on your cover to replace.")
    x0, y0, x1, y1 = region
    pad = int(0.1 * px_per_in)
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(img.width, x1 + pad), min(img.height, y1 + pad)
    # A real EAN-13 block is about 2" x 1.2"; never shrink the new one below 1.5" wide.
    min_w = int(1.5 * px_per_in)
    if x1 - x0 < min_w:
        cx = (x0 + x1) // 2
        x0, x1 = max(0, cx - min_w // 2), min(img.width, cx + min_w // 2)
    out = img.copy()
    out.paste((255, 255, 255), (x0, y0, x1, y1))
    code = generate_barcode_pil(digits).convert("RGB")
    bw, bh = x1 - x0 - 2 * pad, y1 - y0 - 2 * pad
    s = min(bw / code.width, bh / code.height)
    code = code.resize((max(1, int(code.width * s)), max(1, int(code.height * s))), Image.LANCZOS)
    out.paste(code, (x0 + (x1 - x0 - code.width) // 2, y0 + (y1 - y0 - code.height) // 2))
    return out, (f"SparkPrep covered the old barcode with a white box and this book's barcode (ISBN {digits}). "
                 "Please check it sits where you want it on the back cover.")
