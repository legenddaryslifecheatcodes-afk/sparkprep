"""AI image upscaling: a trained super-resolution neural network (FSRCNN),
run on the server's own CPU through OpenCV's dnn_superres module.

This is the "AI" in AI Upscale. A plain resize (Lanczos) only spreads the
existing pixels out; the network was trained on real photos to rebuild the
fine edges and texture a low-resolution image is missing, so the enlarged
cover is sharper than a resize of the same file. No outside service, no API
key and no per-image fee: the three model files are ~40 KB each and ship
with the app (models/superres/, Apache 2.0).

Speed and memory on a 1-CPU / 2 GB server: the network works at the SOURCE
resolution, so its cost follows the source's pixel count (~2 s per source
megapixel, measured). The image is processed in tiles so memory stays small
no matter how big the cover is. Sources above MAX_AI_SOURCE_MP (rare for a
file that's genuinely low-resolution) fall back to a plain resize, and that
is reported honestly as method "resample", never as "ai".
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger("sparkprep.ai_superres")

MODEL_DIR = Path(__file__).resolve().parent / "models" / "superres"
SCALES = (2, 3, 4)
MAX_AI_SOURCE_MP = 8.0     # ~16 s of network time at the measured rate
TILE = 384                 # source pixels per tile side
PAD = 8                    # overlap so tile seams don't show

_lock = threading.Lock()
_models: dict = {}


def available() -> bool:
    try:
        import cv2  # noqa: F401
        return hasattr(cv2, "dnn_superres") and all((MODEL_DIR / f"FSRCNN_x{s}.pb").exists() for s in SCALES)
    except Exception:
        return False


def _model(scale: int):
    import cv2
    with _lock:
        sr = _models.get(scale)
        if sr is None:
            sr = cv2.dnn_superres.DnnSuperResImpl_create()
            sr.readModel(str(MODEL_DIR / f"FSRCNN_x{scale}.pb"))
            sr.setModel("fsrcnn", scale)
            _models[scale] = sr
        return sr


def pick_scale(needed: float) -> int:
    """Smallest network scale that reaches the needed enlargement (x4 at most;
    anything beyond is finished with a resize from the AI result)."""
    for s in SCALES:
        if needed <= s + 0.05:
            return s
    return SCALES[-1]


def upscale_rgb(rgb: np.ndarray, scale: int) -> np.ndarray:
    """Runs the network over an RGB uint8 array, tile by tile. Returns an
    RGB uint8 array exactly `scale` times larger on each side."""
    import cv2
    sr = _model(scale)
    h, w = rgb.shape[:2]
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    out = np.zeros((h * scale, w * scale, 3), dtype=np.uint8)
    with _lock:  # one OpenCV net instance per scale; keep calls serialized
        cv2.setNumThreads(1)
        for y in range(0, h, TILE):
            for x in range(0, w, TILE):
                y0, x0 = max(0, y - PAD), max(0, x - PAD)
                y1, x1 = min(h, y + TILE + PAD), min(w, x + TILE + PAD)
                up = sr.upsample(bgr[y0:y1, x0:x1])
                ty, tx = (y - y0) * scale, (x - x0) * scale
                th, tw = (min(h, y + TILE) - y) * scale, (min(w, x + TILE) - x) * scale
                out[y * scale:y * scale + th, x * scale:x * scale + tw] = up[ty:ty + th, tx:tx + tw]
    return np.ascontiguousarray(out[:, :, ::-1])


def ai_enlarge(rgb: np.ndarray, needed_scale: float) -> tuple[np.ndarray, dict]:
    """The AI step of an upscale: returns (enlarged RGB array, info). info
    says which method really ran -- "ai" (the network) or "resample" (when
    the network isn't installed or the source is too big for this server) --
    so nothing downstream ever calls a plain resize "AI"."""
    h, w = rgb.shape[:2]
    mp = h * w / 1e6
    if needed_scale <= 1.0:
        return rgb, {"method": "none", "scale": 1}
    if not available():
        return rgb, {"method": "resample", "reason": "AI model not installed on this server"}
    if mp > MAX_AI_SOURCE_MP:
        return rgb, {"method": "resample", "reason": f"source is {mp:.1f} MP, above the {MAX_AI_SOURCE_MP:.0f} MP AI limit"}
    scale = pick_scale(needed_scale)
    try:
        return upscale_rgb(rgb, scale), {"method": "ai", "model": f"FSRCNN x{scale}", "scale": scale}
    except Exception as e:  # never fail the whole fix because the network did
        logger.warning("AI upscale failed, falling back to resize: %s", e)
        return rgb, {"method": "resample", "reason": f"AI step failed: {e}"}
