"""The book-work line (owner, 2026-11-27: "fix that ASAP"). The server has 1 CPU and 2 GB of memory; a full book
fix uses about 280 MB, so about 6 or 7 at the same moment run it out of memory, it restarts, and every file being
worked on fails. This lets only BOOK_WORK_SLOTS heavy book jobs run at once; the others wait their turn.

- Requests (upload, auto-fix, final review, AI enhance, ...) are gated by BookWorkLine, a pure ASGI wrapper keyed
  on the paths in HEAVY_PATHS. A request waits up to REQUEST_WAIT_S for a free slot; if none frees up it gets a
  friendly "you're in line, press it again in a minute" answer (503) instead of a crash. Nothing was changed yet,
  so pressing again is safe.
- Background exports wait as long as they need (in_line() tells the status page where they are), and only
  EXPORT_SLOTS of them run at once because the colour conversion is the hungriest step.
- The free check (/api/audit/...) is never gated: it's tiny and fast.
"""
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager

import memory_release

BOOK_WORK_SLOTS = int(os.environ.get("BOOK_WORK_SLOTS", "3"))
EXPORT_SLOTS = int(os.environ.get("EXPORT_SLOTS", "1"))
REQUEST_WAIT_S = float(os.environ.get("BOOK_WORK_WAIT_S", "20"))

HEAVY_PATHS = re.compile(
    r"^/api/projects/[^/]+/("
    r"upload|slot-upload/[^/]+|template-upload|autofix|autofix/verified|autofix/confirm|final-review|export|"
    r"interior-check/run|interior-studio|ai-cover|ai-enhance/[^/]+|suggested-fix|suggested-fix/[^/]+/accept|cover-template"
    r")$"
)

BUSY_MESSAGE = ("Lots of authors are fixing books right now, so yours is next in line. Nothing was changed yet. "
                "Please press the button again in a minute.")

_state = {}          # per event loop: {"work": Semaphore, "export": Semaphore, "waiting": int, "export_waiting": list}


def _st():
    loop = asyncio.get_running_loop()
    st = _state.get(id(loop))
    if st is None or st["loop"] is not loop:
        st = {"loop": loop, "work": asyncio.Semaphore(BOOK_WORK_SLOTS), "export": asyncio.Semaphore(EXPORT_SLOTS),
              "waiting": 0, "export_waiting": []}
        _state[id(loop)] = st
    return st


def snapshot() -> dict:
    """For the owner: how busy the line is right now."""
    try:
        st = _st()
    except RuntimeError:
        return {}
    return {"slots": BOOK_WORK_SLOTS, "free": st["work"]._value, "waiting": st["waiting"],
            "exports_waiting": len(st["export_waiting"])}


def in_line(job_id: str):
    """-> 1-based place in the export line, or None if that export isn't waiting."""
    try:
        q = _st()["export_waiting"]
    except RuntimeError:
        return None
    return q.index(job_id) + 1 if job_id in q else None


@asynccontextmanager
async def export_turn(job_id: str):
    """Waits (as long as needed) for an export turn and a book-work slot, then holds both."""
    st = _st()
    st["export_waiting"].append(job_id)
    try:
        await st["export"].acquire()
    finally:
        if job_id in st["export_waiting"]:
            st["export_waiting"].remove(job_id)
    try:
        await st["work"].acquire()
        try:
            yield
        finally:
            st["work"].release()
    finally:
        st["export"].release()
        await _hand_back_memory()


async def _hand_back_memory():
    """After a heavy job: give its leftover memory back to the server (memory_release.py). Runs after the answer
    has already gone to the customer, off the main loop, and can never fail their request."""
    try:
        await asyncio.to_thread(memory_release.release)
    except Exception:
        pass


class BookWorkLine:
    """Pure ASGI wrapper: lets HEAVY_PATHS requests through BOOK_WORK_SLOTS at a time. Everything else is untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") in ("OPTIONS", "GET", "HEAD") \
                or not HEAVY_PATHS.search(scope.get("path", "")):
            return await self.app(scope, receive, send)
        st = _st()
        st["waiting"] += 1
        try:
            await asyncio.wait_for(st["work"].acquire(), timeout=REQUEST_WAIT_S)
            got = True
        except asyncio.TimeoutError:
            got = False
        finally:
            st["waiting"] -= 1
        if not got:
            body = json.dumps({"detail": BUSY_MESSAGE, "in_line": True}).encode()
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"retry-after", b"60"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        try:
            await self.app(scope, receive, send)
        finally:
            st["work"].release()
            await _hand_back_memory()
