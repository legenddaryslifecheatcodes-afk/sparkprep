"""AI Upscale is a real super-resolution network (2026-11-23), and Auto-Fix
uses it to fix low resolution instead of only relabelling the file 300 DPI."""
import io

import numpy as np
import pytest
from PIL import Image

import ai_superres
from image_upscale_engine import upscale_file_for_print, upscale_to_size_with_info

needs_ai = pytest.mark.skipif(not ai_superres.available(), reason="OpenCV dnn_superres not installed")


def _photo(w, h):
    y, x = np.mgrid[0:h, 0:w]
    arr = np.stack([np.sin(x / 7) * 120 + 128, np.cos(y / 11) * 120 + 128, (x + y) % 255], -1)
    return Image.fromarray(arr.astype("uint8"), "RGB")


def test_pick_scale():
    assert [ai_superres.pick_scale(s) for s in (1.2, 2.0, 2.4, 3.0, 3.6, 9)] == [2, 2, 3, 3, 4, 4]


@needs_ai
def test_tiles_join_without_seams():
    img = np.array(_photo(500, 420))
    tiled = ai_superres.upscale_rgb(img, 2)
    whole = ai_superres._model(2).upsample(np.ascontiguousarray(img[:, :, ::-1]))[:, :, ::-1]
    assert tiled.shape == whole.shape
    assert np.abs(tiled.astype(int) - whole.astype(int)).max() <= 2


@needs_ai
def test_upscale_to_size_reports_ai_and_hits_exact_size():
    buf = io.BytesIO()
    _photo(600, 900).save(buf, "PNG")
    out, info = upscale_to_size_with_info(buf.getvalue(), 1875, 2775)
    assert info["method"] == "ai" and info["model"].startswith("FSRCNN")
    assert Image.open(io.BytesIO(out)).size == (1875, 2775)


@needs_ai
def test_ai_beats_plain_resize_on_detail():
    """Shrink a detailed image, enlarge it back both ways: the network's result is closer to the original."""
    from PIL import ImageDraw, ImageFont
    original = Image.new("RGB", (800, 800), "white")
    draw = ImageDraw.Draw(original)
    font = ImageFont.load_default(size=40)
    for i in range(12):
        draw.text((20, 20 + i * 62), f"The Legend of Spark {i}", fill=(20, 30, 90), font=font)
    draw.ellipse((500, 500, 780, 780), fill=(200, 40, 40))
    small = original.resize((400, 400), Image.Resampling.BICUBIC)
    ref = np.array(original).astype(float)
    ai = ai_superres.upscale_rgb(np.array(small), 2).astype(float)
    plain = np.array(small.resize((800, 800), Image.Resampling.LANCZOS)).astype(float)
    assert ((ai - ref) ** 2).mean() < 0.8 * ((plain - ref) ** 2).mean()


def test_falls_back_honestly_when_source_is_too_big(monkeypatch):
    monkeypatch.setattr(ai_superres, "MAX_AI_SOURCE_MP", 0.01)
    arr = np.array(_photo(200, 200))
    out, info = ai_superres.ai_enlarge(arr, 2.0)
    assert info["method"] == "resample" and "limit" in info["reason"]


@needs_ai
def test_upscale_file_for_print_keeps_proportions(tmp_path):
    src = tmp_path / "cover.jpg"
    _photo(600, 900).save(src, "JPEG")
    info = upscale_file_for_print(str(src), str(tmp_path / "out.png"), 1838, 2775)
    with Image.open(tmp_path / "out.png") as out:
        w, h = out.size
    assert w >= 1838 and h >= 2775 and abs(w / h - 600 / 900) < 0.01
    assert info["method"] == "ai"
