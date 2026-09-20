"""CMYK conversion time and PEAK memory at a given size (run each size in a fresh process).
Usage: python backend/tools/profile_cmyk_scale.py WIDTH HEIGHT"""
import ctypes, os, sys, tempfile, time
from ctypes import wintypes
from pathlib import Path
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import file_processor as fp
Image.MAX_IMAGE_PIXELS = None


def peak_mb():
    if os.name == "nt":
        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
        k32, ps = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 1e6
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


if __name__ == "__main__":
    w, h = int(sys.argv[1]), int(sys.argv[2])
    d = tempfile.mkdtemp(); src = os.path.join(d, "c.jpg"); out = os.path.join(d, "c.tif")
    Image.new("RGB", (w, h), (6, 4, 40)).save(src, "JPEG", dpi=(300, 300), quality=90)
    base = peak_mb()
    t = time.perf_counter(); fp.convert_to_cmyk(src, out, 300, tac_limit=240); dur = time.perf_counter() - t
    mp = w * h / 1e6
    print(f"{w}x{h} = {mp:5.1f} MP | convert_to_cmyk {dur:6.1f}s ({dur/mp:.2f} s/MP) | peak memory {peak_mb():6.0f} MB (before conversion {base:.0f} MB; ~{(peak_mb()-base)/mp:.0f} MB per megapixel)")
