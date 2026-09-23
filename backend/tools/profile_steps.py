"""Per-step timings at real customer file sizes, printed as each finishes (no waiting for a whole run).

Times each expensive step once on a ~12 MB and a ~30 MB cover and interior, and prints how many times the
normal customer flow runs it, so it's obvious where the minutes go.

Usage:  python -u backend/tools/profile_steps.py
"""
import io
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_steps_"), USE_MEMORY_DB="1", JWT_SECRET="steps-" + "x" * 32)
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tools"))

import file_processor as fp  # noqa: E402
import pdfx_validator as pv  # noqa: E402
from book_pass import fingerprint as fpr  # noqa: E402
from profile_sizes import cover_png, interior_pdf_with_images  # noqa: E402

OUT = Path(tempfile.mkdtemp(prefix="sp_steps_files_"))


def t(label, fn, runs_per_flow=None):
    s = time.perf_counter()
    r = fn()
    d = time.perf_counter() - s
    extra = f"   x{runs_per_flow} per customer flow = {d * runs_per_flow:6.1f}s" if runs_per_flow else ""
    print(f"  {label:48s} {d:6.1f}s{extra}", flush=True)
    return r


def cover(name, data, w_in=12.63, h_in=9.25):
    print(f"\n{name}  ({len(data)/1e6:.1f} MB)", flush=True)
    src = OUT / f"{name}.png"
    src.write_bytes(data)
    meta = t("analyze_file", lambda: fp.analyze_file(str(src)))
    chk = lambda path: pv.check_cover_safety_margins(str(path), False, w_in, h_in, "IngramSpark", spine_x_in=6.125,  # noqa: E731
                                                     spine_w_in=0.45, page_count=200, binding="paperback")
    t("cover text check, original file (first read)", lambda: chk(src))
    t("cover text check, original file (repeat)", lambda: chk(src))
    t("ink coverage check", lambda: fp.check_total_ink_coverage(str(src), False, "ingramspark", "IngramSpark"), runs_per_flow=6)
    dst = OUT / f"{name}_cmyk.tif"
    t("CMYK conversion + ink clamp (repair)", lambda: fp.convert_to_cmyk(str(src), str(dst)), runs_per_flow=1)
    t("cover text check, repaired file (first read)", lambda: chk(dst))
    t("cover text check, repaired file (repeat)", lambda: chk(dst))
    t("same-book fingerprint (cover OCR, per export)", lambda: fpr.cover_signature(str(dst), False), runs_per_flow=1)
    pdf = OUT / f"{name}_export.pdf"
    t("export: build cover PDF/X-1a", lambda: fp.build_print_ready_pdf(str(dst), str(pdf), 6, 9, 0.125, spine_w=0.45), runs_per_flow=1)


def interior(name, data):
    print(f"\n{name}  ({len(data)/1e6:.1f} MB, 300 pages)", flush=True)
    src = OUT / f"{name}.pdf"
    src.write_bytes(data)
    t("analyze_file", lambda: fp.analyze_file(str(src)), runs_per_flow=5)
    t("basic scan: structure audit (page 1)", lambda: pv.run_pdf_structure_audit(str(src), "IngramSpark", max_pages=1), runs_per_flow=5)
    t("basic scan: margin check (page 1)", lambda: pv.check_interior_safety_margins(str(src), "IngramSpark", 6, 9, max_pages=1), runs_per_flow=5)
    t("advanced: structure audit (300 pages)", lambda: pv.run_pdf_structure_audit(str(src), "IngramSpark", max_pages=300), runs_per_flow=2)
    t("advanced: margin check (300 pages)", lambda: pv.check_interior_safety_margins(str(src), "IngramSpark", 6, 9, max_pages=300), runs_per_flow=2)
    fixed = OUT / f"{name}_fixed.pdf"
    t("repair: margins / page size (300 pages)", lambda: fp.autofix_interior_safety_margins(str(src), str(fixed), 6, 9), runs_per_flow=1)
    t("same-book fingerprint (interior text, per export)", lambda: fpr.interior_signature(str(src)), runs_per_flow=1)
    out = OUT / f"{name}_x1a.pdf"
    t("export: build interior PDF/X-1a", lambda: fp.build_interior_pdf_x1a(str(fixed if fixed.exists() else src), str(out), 6, 9, 0.125), runs_per_flow=1)


if __name__ == "__main__":
    print("building test files ...", flush=True)
    cover("COVER small", cover_png(3780, 2775, 17, "10-15"))
    cover("COVER big", cover_png(5400, 3960, 21, "24-32"))
    interior("INTERIOR small", interior_pdf_with_images(300, 80, "10-15", (560, 375), 5))
    interior("INTERIOR big", interior_pdf_with_images(300, 85, "24-32", (740, 490), 6))
    print("\ndone", flush=True)
