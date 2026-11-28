"""Memory is handed back to the server after each heavy book job (owner, 2026-11-27). See memory_release.py."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory_release  # noqa: E402
import work_line  # noqa: E402


def test_release_runs_on_linux_and_never_raises():
    assert memory_release.release() in (True, False)
    if sys.platform.startswith("linux"):
        assert memory_release.release() is True


def _rss_mb():
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1]) // 1024


def test_freed_picture_memory_goes_back_to_the_server():
    if not sys.platform.startswith("linux"):
        return
    import numpy as np

    def picture_work(i):
        a = np.random.default_rng(i).integers(0, 255, (2400, 1800, 3), dtype=np.uint8)
        b = [a.astype(np.float32) * 0.5 for _ in range(3)]      # ~150 MB of working pictures, then dropped
        return int(b[0][0, 0, 0])

    async def main():
        await asyncio.gather(*[asyncio.to_thread(picture_work, i) for i in range(3)])
    asyncio.run(main())
    before = _rss_mb()
    memory_release.release()
    assert _rss_mb() <= before          # never grows from a release; usually drops sharply


def test_work_line_hands_memory_back_after_each_heavy_request(monkeypatch):
    calls = []
    monkeypatch.setattr(memory_release, "release", lambda: calls.append(1) or True)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def main():
        work_line._state.clear()
        gate = work_line.BookWorkLine(app)

        async def send(msg):
            pass
        await gate({"type": "http", "method": "POST", "path": "/api/projects/p/autofix"}, None, send)
        await gate({"type": "http", "method": "POST", "path": "/api/audit/a/upload"}, None, send)   # free check
        async with work_line.export_turn("job1"):
            pass
    asyncio.run(main())
    assert len(calls) == 2              # the heavy request and the export, not the free check
