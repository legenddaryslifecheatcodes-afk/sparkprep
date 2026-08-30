"""Real image upscaling via Real-ESRGAN (RRDBNet, x4plus), running on CPU --
no GPU, no external API, no per-use billing.

This replaces the old OpenAI-based "AI Upscale" fix. OpenAI's image-edit
endpoint caps out around 1024-1536px of output and re-paints the image
generatively rather than truly upscaling it -- usually not enough
resolution to reach 300 DPI at real trim sizes, and not a guarantee of
preserving the original content. Real-ESRGAN instead adds genuine pixel
detail to the existing image and can be resampled to an exact target size.

Uses a plain PyTorch implementation of the RRDBNet architecture
(rrdbnet_arch.py) loading the official RealESRGAN_x4plus.pth checkpoint,
rather than the `realesrgan-ncnn-py` package -- that package's CPU code
path (gpuid=-1) crashes with an ncnn allocator error in practice, and the
`basicsr`/`realesrgan` pip packages have a history of breaking against
newer torchvision releases. Plain PyTorch's CPU path is the most
mature/widely-used option available.
"""
import io
import urllib.request
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from rrdbnet_arch import RRDBNet

WEIGHTS_PATH = Path(__file__).resolve().parent / "weights" / "RealESRGAN_x4plus.pth"
WEIGHTS_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"

TILE_SIZE = 256
TILE_OVERLAP = 16
SCALE = 4

_model = None


def _get_model():
    global _model
    if _model is None:
        if not WEIGHTS_PATH.exists():
            WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(WEIGHTS_URL, WEIGHTS_PATH)

        model = RRDBNet(num_in_ch=3, num_out_ch=3, scale=SCALE, num_feat=64, num_block=23, num_grow_ch=32)
        state_dict = torch.load(WEIGHTS_PATH, map_location="cpu")
        state_dict = state_dict.get("params_ema") or state_dict.get("params") or state_dict
        model.load_state_dict(state_dict)
        model.eval()
        _model = model
    return _model


def _to_tensor(img: Image.Image) -> torch.Tensor:
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)


def _to_image(tensor: torch.Tensor) -> Image.Image:
    arr = tensor.squeeze(0).clamp(0, 1).permute(1, 2, 0).numpy()
    return Image.fromarray((arr * 255.0).round().astype(np.uint8))


def _run_4x(img: Image.Image) -> Image.Image:
    """Runs one 4x pass, tiling the image so CPU memory use stays bounded
    regardless of source image size."""
    model = _get_model()
    w, h = img.size

    if max(w, h) <= TILE_SIZE:
        with torch.no_grad():
            out = model(_to_tensor(img))
        return _to_image(out)

    out_img = Image.new("RGB", (w * SCALE, h * SCALE))
    step = TILE_SIZE - TILE_OVERLAP
    for y0 in range(0, h, step):
        for x0 in range(0, w, step):
            x1, y1 = min(x0 + TILE_SIZE, w), min(y0 + TILE_SIZE, h)
            tile = img.crop((x0, y0, x1, y1))
            with torch.no_grad():
                out_tile = _to_image(model(_to_tensor(tile)))

            # Crop off the overlap margin (except at the image edges) before
            # pasting, so tiles line up without visible seams.
            left_trim = TILE_OVERLAP * SCALE if x0 > 0 else 0
            top_trim = TILE_OVERLAP * SCALE if y0 > 0 else 0
            out_tile = out_tile.crop((left_trim, top_trim, out_tile.width, out_tile.height))
            out_img.paste(out_tile, (x0 * SCALE + left_trim, y0 * SCALE + top_trim))

    return out_img


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

    scale_so_far = 1.0
    passes = 0
    while scale_so_far < needed_scale and passes < 2:
        img = _run_4x(img)
        scale_so_far *= SCALE
        passes += 1

    if img.width != target_width_px or img.height != target_height_px:
        img = img.resize((target_width_px, target_height_px), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
