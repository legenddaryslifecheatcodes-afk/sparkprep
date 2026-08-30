"""Real image upscaling via Real-ESRGAN, running on CPU -- no GPU, no
external API, no per-use billing.

This replaces the old OpenAI-based "AI Upscale" fix. OpenAI's image-edit
endpoint caps out around 1024-1536px of output and re-paints the image
generatively rather than truly upscaling it -- usually not enough resolution
to reach 300 DPI at real trim sizes, and not a guarantee of preserving the
original content. Real-ESRGAN instead adds genuine pixel detail to the
existing image and can be resampled to an exact target size.

Uses the ncnn runtime bundled inside the realesrgan-ncnn-py package (model
weights included, nothing downloaded at runtime), which is why the backend's
Dockerfile was moved off Python 3.14 -- ncnn/torch wheels weren't yet
published for that version at the time this was written.
"""
import io
import math

from PIL import Image

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from realesrgan_ncnn_py import Realesrgan
        # model=4 -> realesrgan-x4plus, the general-purpose model (not the
        # anime-specific variants) -- appropriate for book cover art/photos.
        # gpuid=-1 -> CPU only.
        _engine = Realesrgan(gpuid=-1, model=4)
    return _engine


def upscale_to_size(image_bytes: bytes, target_width_px: int, target_height_px: int) -> bytes:
    """Upscales an image so its pixel dimensions meet or exceed the target,
    then resizes down to the exact target with high-quality resampling.

    The model's scale factor is a fixed 4x per pass. If 4x overshoots what's
    needed (the common case), the final resize brings it to the exact size
    while keeping the added detail -- much sharper than a naive stretch of
    the original low-res source. Capped at 2 passes (16x) since anything
    needing more than that is not a realistic "slightly low DPI" case.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    needed_scale = max(
        target_width_px / img.width,
        target_height_px / img.height,
        1.0,
    )

    engine = _get_engine()
    scale_so_far = 1.0
    passes = 0
    while scale_so_far < needed_scale and passes < 2:
        img = engine.process_pil(img)
        scale_so_far *= 4
        passes += 1

    if img.width != target_width_px or img.height != target_height_px:
        img = img.resize((target_width_px, target_height_px), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
