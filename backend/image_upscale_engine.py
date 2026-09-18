"""Fast image upscaling via Pillow LANCZOS resampling + an adaptive unsharp
mask -- consistently well under 2 seconds even for a large full-wrap cover,
no ML model, no GPU, no per-use billing, no multi-minute request.

This replaces the previous Real-ESRGAN (RRDBNet) implementation. That model
added genuinely more pixel detail than a plain resample, but running a
23-block CPU-only super-resolution network (even tiled, single-threaded, to
stay under the host's memory ceiling) took 90 seconds to 4.5 minutes on this
service's 1-core Render instance in practice -- real requests came back as
502/499 (the client or the platform's own proxy gave up waiting). A fast,
reliable fix beats a marginally sharper one that regularly times out.

Two choices here are what keep this fast regardless of the TARGET size
(a full-wrap cover's target can be 50-100+ megapixels at 300 DPI, where the
old pipeline's cost -- and a naive "resize then sharpen" replacement -- would
both scale up):
  1. The unsharp mask runs on the small SOURCE image, before the resize, not
     after. Sharpening cost scales with pixel count; the source is exactly
     the low-res image this endpoint exists to fix, so it's cheap almost by
     definition. Sharpening pre-resize also means LANCZOS is upsampling
     already-crisp edges rather than softening them further and asking a
     second pass to compensate.
  2. Final encode is JPEG (quality=95, no chroma subsampling), not PNG.
     Pillow's PNG encoder cost is dominated by its per-row filtering step,
     which doesn't meaningfully speed up at any compress_level and became
     the single largest cost at real full-wrap resolutions in testing
     (~1s+ on its own at 50+ megapixels); JPEG encodes the same pixel count
     in a fraction of that. This file is an intermediate the CMYK/PDF export
     step processes further, not a final deliverable, so the quality
     difference at quality=95 is not a real-world tradeoff worth the wait.

If a genuinely sharper upscale is worth the latency/cost later, this is the
seam to swap in a GPU API call -- upscale_to_size()'s signature doesn't need
to change for that.
"""
import io

from PIL import Image, ImageFilter


def _adaptive_unsharp_mask(img: Image.Image, scale_factor: float) -> Image.Image:
    """Scales unsharp-mask strength with how much the image is about to be
    enlarged. A ~1x resize (already near the target size) gets a light
    touch; a large enlargement gets meaningfully stronger sharpening so the
    extra detail survives LANCZOS interpolation. Clamped so it never
    oversharpens into visible haloing on a modest resize.
    """
    radius = min(1.0 + scale_factor * 0.4, 3.0)
    percent = int(min(70 + scale_factor * 25, 200))
    return img.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=2))


def upscale_to_size(image_bytes: bytes, target_width_px: int, target_height_px: int) -> bytes:
    """Resizes an image to exactly the target pixel dimensions using
    high-quality LANCZOS resampling, sharpening the source first (see
    module docstring for why that ordering) with an adaptive unsharp mask.
    Always returns a JPEG at exactly target_width_px x target_height_px.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    scale_factor = max(
        target_width_px / img.width,
        target_height_px / img.height,
        1.0,
    )

    img = _adaptive_unsharp_mask(img, scale_factor)

    if img.width != target_width_px or img.height != target_height_px:
        img = img.resize((target_width_px, target_height_px), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95, subsampling=0)
    return out.getvalue()
