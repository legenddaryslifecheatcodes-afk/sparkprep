"""ISBN validation + EAN-13 barcode generation for the reserved barcode zone."""
import re
import io
from barcode import EAN13
from barcode.writer import ImageWriter
from PIL import Image


def _check_digit_13(base_12: str) -> str:
    s = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(base_12))
    return str((10 - (s % 10)) % 10)


def normalize_isbn(isbn: str) -> dict:
    """Accept ISBN-10 or ISBN-13 (with dashes/spaces), return normalized 13-digit ISBN + validity."""
    clean = re.sub(r"[^\dXx]", "", isbn or "")
    if len(clean) == 10:
        # Convert ISBN-10 → ISBN-13
        base_12 = "978" + clean[:-1]
        check = _check_digit_13(base_12)
        clean = base_12 + check
    if len(clean) != 13:
        return {"valid": False, "isbn": clean, "error": "ISBN must be 10 or 13 digits"}
    # Validate check digit
    expected = _check_digit_13(clean[:12])
    if expected != clean[12]:
        return {"valid": False, "isbn": clean, "error": f"Invalid check digit (expected {expected})"}
    return {"valid": True, "isbn": clean}


def generate_barcode_png_bytes(isbn: str, module_width_mm: float = 0.33, height_mm: float = 25.0) -> bytes:
    """Render an EAN-13 barcode PNG. Returns raw PNG bytes."""
    info = normalize_isbn(isbn)
    if not info["valid"]:
        raise ValueError(info.get("error", "Invalid ISBN"))
    clean = info["isbn"]
    # python-barcode EAN13 takes 12 digits (auto-appends check digit)
    ean = EAN13(clean[:12], writer=ImageWriter())
    buf = io.BytesIO()
    ean.write(
        buf,
        options={
            "module_width": module_width_mm,
            "module_height": height_mm,
            "quiet_zone": 4,
            "font_size": 10,
            "text_distance": 3,
            "background": "white",
            "foreground": "black",
            "write_text": True,
        },
    )
    return buf.getvalue()


def generate_barcode_pil(isbn: str) -> Image.Image:
    """Return a PIL Image for compositing into a cover PDF."""
    png_bytes = generate_barcode_png_bytes(isbn)
    return Image.open(io.BytesIO(png_bytes))
