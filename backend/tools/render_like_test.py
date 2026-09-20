"""Render-like load test: the REAL API server in its own process, inside a Windows Job Object that enforces
a 2 GB memory cap and ONE CPU core (Render plan 1c-2g), measuring peak memory of the server + its helper
processes (Tesseract, Ghostscript), while N real cover jobs run at once through the production endpoints:
   slot-upload -> /autofix/verified -> /autofix/confirm -> /export -> download

Usage: python backend/tools/render_like_test.py "10.6,27" [--cap-mb 2048] [--cores 1] [--old-memory]
   first arg = comma list of cover sizes in megapixels, one concurrent job each (10.6 -> 3810x2775, 27 -> 6000x4500)
   --old-memory  = process the whole image at once (the pre-fix memory behaviour) for an A/B comparison
"""
import argparse
import ctypes
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = None
HERE = Path(__file__).resolve().parent
SIZES = {"10.6": (3810, 2775), "27": (6000, 4500), "63": (9000, 7000)}


# ---------------------------------------------------------------- Windows job object (memory cap + CPU affinity)
class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]


class BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class EXT(ctypes.Structure):
    _fields_ = [("Basic", BASIC), ("Io", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateJobObjectW.restype = wintypes.HANDLE
k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
LIMIT_AFFINITY, LIMIT_JOB_MEMORY = 0x10, 0x200


def make_job(cap_mb, cores):
    job = k32.CreateJobObjectW(None, None)
    info = EXT()
    info.Basic.LimitFlags = LIMIT_AFFINITY | (LIMIT_JOB_MEMORY if cap_mb else 0)
    info.Basic.Affinity = (1 << cores) - 1
    info.JobMemoryLimit = int(cap_mb * 1024 * 1024) if cap_mb else 0
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    return job


def job_peak_mb(job):
    info = EXT()
    k32.QueryInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info), None)
    return info.PeakJobMemoryUsed / 1024 / 1024


# ---------------------------------------------------------------- test data
def cover_bytes(w, h):
    img = Image.new("RGB", (w, h), (6, 4, 40))
    d = ImageDraw.Draw(img)
    for i in range(0, w, 300):
        d.rectangle([i, 0, i + 150, h], fill=(0, 10, 60))
    d.text((55, 60), "LAST LIGHT", font=ImageFont.load_default(size=130), fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", dpi=(300, 300), quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------- one job through the production endpoints
def run_job(base, tok, data, label, data_dir, out):
    H = {"Authorization": "Bearer " + tok}
    r = {"label": label}
    t0 = time.time()
    try:
        pid = requests.post(f"{base}/api/projects", headers=H, json={"name": label, "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                                                    "binding": "paperback", "page_count": 200, "project_type": "cover"}, timeout=60).json()["id"]
        t = time.time(); up = requests.post(f"{base}/api/projects/{pid}/slot-upload/full_wrap", headers=H, files={"file": ("c.jpg", data, "image/jpeg")}, timeout=3000)
        r["upload+scan_s"] = round(time.time() - t, 1); r["upload_http"] = up.status_code
        before = {c["id"]: c["status"] for c in up.json()["compliance"]}
        t = time.time(); fx = requests.post(f"{base}/api/projects/{pid}/autofix/verified", headers=H, params={"slot": "full_wrap", "stream": "false"}, timeout=3000)
        r["pipeline_s"] = round(time.time() - t, 1); r["pipeline_http"] = fx.status_code
        pl = fx.json()["pipeline"]
        r["pipeline_status"] = pl["status"]; r["health"] = f'{pl["health_before"]}->{pl["health_after"]}'
        r["still_open"] = [x["id"] for x in pl["remaining"]]; r["stage_timings"] = pl["timings"]
        t = time.time(); cf = requests.post(f"{base}/api/projects/{pid}/autofix/confirm", headers=H, params={"slot": "full_wrap"}, timeout=3000)
        r["confirm_s"] = round(time.time() - t, 1); r["confirm_http"] = cf.status_code
        real = [c for c in cf.json()["compliance"] if c["id"] not in ("bleed", "pdfx1a", "pdf_dpi")]
        r["confirm_all_pass"] = all(c["status"] == "pass" for c in real)
        requests.patch(f"{base}/api/projects/{pid}", headers=H, json={"name": label + " Book"}, timeout=60)
        t = time.time(); ex = requests.post(f"{base}/api/projects/{pid}/export", headers=H, timeout=3000)
        r["export_s"] = round(time.time() - t, 1); r["export_http"] = ex.status_code
        # ---- independent correctness checks on what is actually stored / delivered
        proj = requests.get(f"{base}/api/projects/{pid}", headers=H, timeout=60).json()
        stored = Path(data_dir) / "uploads" / proj["slots"]["full_wrap"]["stored_filename"]
        with Image.open(stored) as im:
            r["fixed_file"] = f"{im.mode} {im.width}x{im.height}"
            r["size_preserved"] = (im.width, im.height) == (SIZES_BY_DATA[id(data)])
            arr = np.asarray(im)
            peak_units = max(int(arr[y:y + 256].sum(axis=-1, dtype=np.int32).max()) for y in range(0, arr.shape[0], 256))
            r["true_max_ink_%"] = round(peak_units / 255 * 100, 2)
        if ex.status_code == 200:
            import pymupdf
            dl = requests.get(base + ex.json()["download_url"], headers=H, params={"token": tok}, timeout=3000)
            with pymupdf.open(stream=dl.content, filetype="pdf") as doc:
                r["export_colorspace"] = [doc.extract_image(i[0]).get("cs-name") for i in doc[0].get_images(full=True)]
        r["ok"] = True
    except Exception as e:  # noqa: BLE001
        r["ok"] = False
        r["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    r["total_s"] = round(time.time() - t0, 1)
    out.append(r)


SIZES_BY_DATA = {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sizes")
    ap.add_argument("--cap-mb", type=int, default=2048)
    ap.add_argument("--cores", type=int, default=1)
    ap.add_argument("--old-memory", action="store_true")
    a = ap.parse_args()
    sizes = a.sizes.split(",")
    port = 8300 + (os.getpid() % 500)
    data_dir = tempfile.mkdtemp(prefix="sp_load_")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("SPARKPREP_TIME_LIMITS", None)
    if a.old_memory:
        env["SPARKPREP_CMYK_BAND_PIXELS"] = "1000000000"          # one band = whole image = pre-fix memory behaviour
    job = make_job(a.cap_mb, a.cores)
    log = open(os.path.join(data_dir, "server.log"), "w")
    proc = subprocess.Popen([sys.executable, str(HERE / "serve_local.py"), str(port), data_dir], env=env, stdout=log, stderr=subprocess.STDOUT)
    k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if requests.get(base + "/api/health", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(1)
    idle = job_peak_mb(job)
    tok = requests.post(f"{base}/api/auth/login", json={"email": "load@example.com", "password": "TestPass123!"}).json()["token"]

    payloads = []
    for s in sizes:
        w, h = SIZES[s]
        b = cover_bytes(w, h)
        SIZES_BY_DATA[id(b)] = (w, h)
        payloads.append((s, b))

    results, lat, stop = [], [], threading.Event()

    def bystander():           # another customer just loading a page while the heavy jobs run
        while not stop.is_set():
            t = time.time()
            try:
                requests.get(base + "/api/health", timeout=120)
                lat.append(time.time() - t)
            except Exception:
                lat.append(999.0)
            time.sleep(1)

    bt = threading.Thread(target=bystander, daemon=True); bt.start()
    t0 = time.time()
    threads = [threading.Thread(target=run_job, args=(base, tok, b, f"job{i+1}-{s}MP", data_dir, results)) for i, (s, b) in enumerate(payloads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    stop.set(); bt.join(timeout=5)
    alive = proc.poll() is None
    peak = job_peak_mb(job)
    summary = {
        "scenario": f"{len(sizes)} concurrent job(s): {sizes} MP | cap {a.cap_mb} MB | {a.cores} core(s) | {'OLD whole-image memory mode' if a.old_memory else 'banded (new)'}",
        "server_idle_mb": round(idle), "PEAK_MEMORY_MB (server + Tesseract/Ghostscript helpers)": round(peak),
        "memory_cap_mb": a.cap_mb, "peak_as_%_of_cap": round(peak / a.cap_mb * 100) if a.cap_mb else None,
        "server_still_alive": alive, "wall_clock_s": round(wall, 1),
        "other_customer_health_check_latency_s": {"max": round(max(lat), 2) if lat else None, "median": round(sorted(lat)[len(lat) // 2], 2) if lat else None},
        "jobs": sorted(results, key=lambda r: r["label"]),
    }
    print(json.dumps(summary, indent=2))
    if not alive:
        print("SERVER LOG TAIL:\n" + "".join(open(os.path.join(data_dir, "server.log")).readlines()[-15:]))
    proc.kill()


if __name__ == "__main__":
    main()
