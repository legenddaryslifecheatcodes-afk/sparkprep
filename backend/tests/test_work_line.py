"""The book-work line (owner, 2026-11-27): only BOOK_WORK_SLOTS heavy book jobs run at once, the rest wait their
turn or get a friendly "you're in line" answer, and the free check is never held up."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import work_line  # noqa: E402


def test_heavy_paths():
    hp = work_line.HEAVY_PATHS
    assert hp.search("/api/projects/abc/autofix")
    assert hp.search("/api/projects/abc/autofix/verified")
    assert hp.search("/api/projects/abc/slot-upload/front_cover")
    assert hp.search("/api/projects/abc/export")
    assert not hp.search("/api/projects/abc/export-jobs")        # starts a background job; the job waits instead
    assert not hp.search("/api/audit/abc/upload")                # the free check is never gated
    assert not hp.search("/api/projects")


def _run_gate(n_requests, slots, wait_s, work_s):
    async def main():
        work_line._state.clear()
        old = (work_line.BOOK_WORK_SLOTS, work_line.REQUEST_WAIT_S)
        work_line.BOOK_WORK_SLOTS, work_line.REQUEST_WAIT_S = slots, wait_s
        running = {"now": 0, "peak": 0}

        async def app(scope, receive, send):
            running["now"] += 1
            running["peak"] = max(running["peak"], running["now"])
            await asyncio.sleep(work_s)
            running["now"] -= 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        gate = work_line.BookWorkLine(app)

        async def one(path):
            out = {}

            async def send(msg):
                if msg["type"] == "http.response.start":
                    out["status"] = msg["status"]
                else:
                    out["body"] = msg.get("body", b"")
            await gate({"type": "http", "method": "POST", "path": path}, None, send)
            return out
        try:
            heavy = [one("/api/projects/p/autofix") for _ in range(n_requests)]
            free = [one("/api/audit/a/upload")]
            results = await asyncio.gather(*heavy, *free)
        finally:
            work_line.BOOK_WORK_SLOTS, work_line.REQUEST_WAIT_S = old
        return results, running["peak"]
    return asyncio.run(main())


def test_only_three_at_once_and_the_rest_wait_their_turn():
    results, peak = _run_gate(n_requests=7, slots=3, wait_s=5, work_s=0.05)
    assert all(r["status"] == 200 for r in results)
    assert peak <= 3 + 1          # 3 book jobs + the free check, which isn't held up


def test_too_long_a_wait_gets_a_friendly_answer_not_a_crash():
    results, _ = _run_gate(n_requests=5, slots=1, wait_s=0.05, work_s=0.3)
    busy = [r for r in results if r["status"] == 503]
    assert busy and all(json.loads(r["body"])["in_line"] for r in busy)
    assert "next in line" in json.loads(busy[0]["body"])["detail"]
    assert results[-1]["status"] == 200        # the free check still went straight through


def test_exports_take_turns():
    async def main():
        work_line._state.clear()
        order = []

        async def export(name):
            async with work_line.export_turn(name):
                await asyncio.sleep(0.02)                 # by now the other export has asked for its turn
                order.append(("start", name, work_line.in_line("b")))
                order.append(("end", name))
        await asyncio.gather(export("a"), export("b"))
        return order
    order = asyncio.run(main())
    assert order[0][:2] == ("start", "a") and order[0][2] == 1     # b waited, first in line
    assert order[1] == ("end", "a") and order[2][:2] == ("start", "b")
