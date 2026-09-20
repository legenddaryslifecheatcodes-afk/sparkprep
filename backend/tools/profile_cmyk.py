"""Where does convert_to_cmyk's time go? Times each internal step on a real-size cover.
Usage: python backend/tools/profile_cmyk.py [reps=3]"""
import io, os, sys, tempfile, time, statistics
from pathlib import Path
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import file_processor as fp

REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
d = tempfile.mkdtemp()
src = os.path.join(d, "c.jpg")
Image.new("RGB", (3810, 2775), (6, 4, 40)).save(src, "JPEG", dpi=(300, 300), quality=92)
out = os.path.join(d, "c.tif")
rows = {}


def t(label, fn):
    s = time.perf_counter(); r = fn(); rows.setdefault(label, []).append(round(time.perf_counter() - s, 2)); return r


for i in range(REPS):
    img = t("open + decode JPEG", lambda: Image.open(src).convert("RGB"))
    arr = t("to numpy array", lambda: np.array(img))
    cmyk = t("RGB -> CMYK math (rgb_array_to_cmyk_array)", lambda: fp.rgb_array_to_cmyk_array(arr))
    clamped = t("ink clamp (clamp_total_ink_coverage)", lambda: fp.clamp_total_ink_coverage(cmyk, 240))
    im2 = t("array -> PIL image", lambda: Image.fromarray(clamped, mode="CMYK"))
    t("save TIFF (LZW compression)", lambda: im2.save(out, format="TIFF", dpi=(300, 300), compression="tiff_lzw"))
    t("TOTAL convert_to_cmyk (real function)", lambda: fp.convert_to_cmyk(src, out, 300, tac_limit=240))
print(f"cover 3810x2775 = {3810*2775/1e6:.1f} MP, RGB array = {arr.nbytes/1e6:.0f} MB, float64 working copy ~ {arr.nbytes*8/1e6:.0f} MB per channel-stack")
for k, v in rows.items():
    print(f"  {k:52s} median {statistics.median(v):6.2f}s  [{min(v):.2f} - {max(v):.2f}]")
