"""Hands memory back to the server after each heavy book job (owner, 2026-11-27: memory kept after a book
"for no good reason" -> "change that").

Why it was kept: a book fix works on big pictures in background threads. When it finishes, Python frees those
pictures, but the C memory allocator keeps the freed space for itself instead of giving it back to the server,
so the server's memory stayed ~300 MB higher after one book until the next restart. This has nothing to do with
the files saved on disk.

Measured locally with the real AI upscaler, 3 books at once, 6 rounds: without this the process sat at ~600 MB
after every round; with MALLOC_ARENA_MAX=2 (Dockerfile) plus release() it fell back to ~140-195 MB each time.

release() is safe to call at any time and never raises: a failed trim must never break a customer's request.
"""
import ctypes
import ctypes.util
import gc
import logging

logger = logging.getLogger("sparkprep.memory_release")

_libc = None
try:
    _name = ctypes.util.find_library("c") or "libc.so.6"
    _libc = ctypes.CDLL(_name)
    if not hasattr(_libc, "malloc_trim"):      # not glibc (e.g. macOS dev box): nothing to trim
        _libc = None
except Exception:                              # pragma: no cover - only on unusual systems
    _libc = None


def release() -> bool:
    """Collect Python garbage, then ask the allocator to return its free memory to the server.
    -> True if a trim ran."""
    try:
        gc.collect()
        if _libc is not None:
            _libc.malloc_trim(0)
            return True
    except Exception as e:                     # pragma: no cover
        logger.warning("memory release skipped: %s", e)
    return False
