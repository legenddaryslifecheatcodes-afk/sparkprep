"""Same-book protection: one book pass covers one book (and its revisions), not several different books.

At a book's first export we record what the book *says* -- a compact sketch of the interior's wording and the
words on the cover. Later exports in the same window are compared against that record:

  * the interior decides first: a revised manuscript keeps most of its 5-word phrases, a different book shares
    almost none;
  * a new cover design on the same interior is fine (covers get redesigned);
  * cover-only books are compared by the words on the cover (title, author, back-cover copy);
  * anything in between is "unclear" -- allowed, but flagged for the owner to review. Honest customers are never
    blocked on a guess.

Comparing words instead of file hashes or pixels is deliberate: re-exporting the same book from Word, or
redesigning its cover art, changes every byte and pixel but not what the book says.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

SHINGLE_WORDS = 5
SKETCH_SIZE = 256
MIN_INTERIOR_WORDS = 60
MAX_INTERIOR_PAGES = 150
MIN_COVER_WORDS = 3
MAX_COVER_WORDS = 400
COVER_OCR_MAX_PX = 2400
COVER_OCR_MIN_CONFIDENCE = 65

INTERIOR_SAME = 0.20         # revisions of one manuscript stay far above this
INTERIOR_DIFFERENT = 0.05    # two different manuscripts sit near zero
COVER_SAME = 0.40
COVER_DIFFERENT = 0.12

_WORD_RE = re.compile(r"[a-z0-9']+")


def _hash(s: str) -> int:
    # 7 bytes keeps every value inside MongoDB's signed 64-bit integer range.
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=7).digest(), "big")


def words_of(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def text_sketch(words: list[str]) -> Optional[list[int]]:
    """Bottom-k MinHash sketch of the text's 5-word phrases, or None if there's too little text to judge."""
    if len(words) < MIN_INTERIOR_WORDS:
        return None
    phrases = {_hash(" ".join(words[i:i + SHINGLE_WORDS])) for i in range(len(words) - SHINGLE_WORDS + 1)}
    return sorted(phrases)[:SKETCH_SIZE]


def sketch_similarity(a: list[int], b: list[int]) -> float:
    """Estimated Jaccard similarity of the two texts' phrase sets."""
    sa, sb = set(a), set(b)
    union = sorted(sa | sb)[:SKETCH_SIZE]
    if not union:
        return 0.0
    return sum(1 for h in union if h in sa and h in sb) / len(union)


def word_similarity(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if (sa or sb) else 0.0


def interior_signature(pdf_path: str) -> Optional[dict]:
    import fitz
    words: list[str] = []
    doc = fitz.open(pdf_path)
    try:
        for i, page in enumerate(doc):
            if i >= MAX_INTERIOR_PAGES:
                break
            words += words_of(page.get_text())
    finally:
        doc.close()
    sketch = text_sketch(words)
    return {"sketch": sketch, "words": len(words)} if sketch else None


def cover_signature(path: str, is_pdf: bool) -> Optional[dict]:
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return None
    if is_pdf:
        import fitz
        doc = fitz.open(path)
        try:
            pix = doc[0].get_pixmap(dpi=150, colorspace=fitz.csRGB, alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        finally:
            doc.close()
    else:
        image = Image.open(path).convert("RGB")
    if max(image.size) > COVER_OCR_MAX_PX:
        scale = COVER_OCR_MAX_PX / max(image.size)
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
    data = pytesseract.image_to_data(image, config="--psm 11", output_type=pytesseract.Output.DICT)
    found = set()
    for text, conf in zip(data.get("text", []), data.get("conf", [])):
        try:
            if float(conf) < COVER_OCR_MIN_CONFIDENCE:
                continue
        except (TypeError, ValueError):
            continue
        w = re.sub(r"[^a-z]", "", (text or "").lower())
        if len(w) >= 3:
            found.add(w)
    if len(found) < MIN_COVER_WORDS:
        return None
    return {"words": sorted(found)[:MAX_COVER_WORDS]}


def _slot_path(upload_dir: Path, meta: Optional[dict]) -> Optional[Path]:
    if not meta or not meta.get("stored_filename"):
        return None
    path = Path(upload_dir) / meta["stored_filename"]
    return path if path.exists() else None


def project_fingerprint(project: dict, upload_dir) -> dict:
    """Fingerprint of whatever this project will export right now (cover, interior, or both)."""
    ptype = project.get("project_type", "cover")
    slots = project.get("slots") or {}
    parts = {"cover": ["cover"], "interior": ["interior"], "combined": ["cover", "interior"]}.get(ptype, ["cover"])
    fp = {"title": " ".join(words_of(project.get("name", ""))), "parts": parts, "interior": None, "cover": None}
    if ptype in ("interior", "combined"):
        path = _slot_path(upload_dir, slots.get("interior"))
        if path and path.suffix.lower() == ".pdf":
            fp["interior"] = interior_signature(str(path))
    if ptype in ("cover", "combined"):
        meta = slots.get("full_wrap") or slots.get("front_cover") or project.get("file_metadata")
        path = _slot_path(upload_dir, meta)
        if path:
            fp["cover"] = cover_signature(str(path), bool(meta.get("is_pdf")) or path.suffix.lower() == ".pdf")
    return fp


def compare(stored: dict, current: dict) -> tuple[str, dict]:
    """-> ("same" | "different" | "unclear", details). Interior evidence outranks cover evidence."""
    details: dict = {}
    # A book can gain its other half (cover -> cover + interior), never trade the half it started with for a
    # different one -- otherwise a cover bought for book A could be switched to "interior only" for book B.
    if stored.get("parts") and current.get("parts") is not None:
        dropped = sorted(set(stored["parts"]) - set(current["parts"]))
        if dropped:
            return "different", {"dropped_parts": dropped}
    interior_verdict = None
    if stored.get("interior") and current.get("interior"):
        s = sketch_similarity(stored["interior"]["sketch"], current["interior"]["sketch"])
        details["interior_similarity"] = round(s, 3)
        if s >= INTERIOR_SAME:
            return "same", details
        interior_verdict = "different" if s <= INTERIOR_DIFFERENT else "unclear"
        if interior_verdict == "different":
            return "different", details

    if stored.get("cover") and current.get("cover"):
        c = word_similarity(stored["cover"]["words"], current["cover"]["words"])
        details["cover_similarity"] = round(c, 3)
        if c >= COVER_SAME:
            return "same", details
        if c <= COVER_DIFFERENT and interior_verdict is None:
            return "different", details

    return "unclear", details


def merge(stored: dict, current: dict) -> dict:
    """Keep the original baseline; only add a part the book didn't have at first export (e.g. its cover)."""
    out = dict(stored)
    for part in ("interior", "cover"):
        if not out.get(part) and current.get(part):
            out[part] = current[part]
    if stored.get("parts") or current.get("parts"):
        out["parts"] = sorted(set(stored.get("parts") or []) | set(current.get("parts") or []))
    return out
