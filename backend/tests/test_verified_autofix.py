"""Tests for the four-agent verified auto-fix (backend/autofix_agents/).

Runs the real FastAPI app in-process against the in-memory database and a
temp upload folder -- no network, no real accounts. Covers the happy path,
early exit, and every way the pipeline must refuse to keep a repair
(engine crash, verification failure, no progress, timeout, tampering).
"""
import ast
import asyncio
import hashlib
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

DATA_DIR = tempfile.mkdtemp(prefix="sp_verified_")
os.environ.update(DATA_DIR=DATA_DIR, USE_MEMORY_DB="1", JWT_SECRET="test-secret-" + "x" * 32,
                  ADMIN_EMAIL="admin@example.com", ADMIN_SEED_PASSWORD="TestPass123!")
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import server  # noqa: E402
import autofix_agents  # noqa: E402
from autofix_agents import common, pipeline  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

server.ANTHROPIC_API_KEY = ""  # never call a real AI service from tests
UPLOADS = Path(server.UPLOAD_DIR)


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": "admin@example.com", "password_hash": server.hash_password("TestPass123!"), "name": "T",
        "tier": "studio", "created_at": datetime.now(timezone.utc).isoformat(),
        "exports_this_month": 0, "books_this_month": 0,
    }))
    with TestClient(server.app) as c:
        r = c.post("/api/auth/login", json={"email": "admin@example.com", "password": "TestPass123!"})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = "Bearer " + r.json()["token"]
        yield c


def _project(c, ptype="cover"):
    r = c.post("/api/projects", json={"name": "T", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                      "binding": "paperback", "page_count": 200, "project_type": ptype})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _rgb_cover(w=3810, h=2775):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 40, 140)).save(buf, "JPEG", dpi=(300, 300), quality=92)
    return buf.getvalue()


def _upload_cover(c, data=None):
    pid = _project(c)
    r = c.post(f"/api/projects/{pid}/slot-upload/full_wrap", files={"file": ("c.jpg", data or _rgb_cover(), "image/jpeg")})
    assert r.status_code == 200, r.text
    return pid


def _run(c, pid, slot="full_wrap"):
    """Streaming run -> (events, result_data)."""
    events = []
    with c.stream("POST", f"/api/projects/{pid}/autofix/verified", params={"slot": slot}) as r:
        assert r.status_code == 200, r.read()
        for line in r.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    assert events[-1]["type"] == "result"
    return events, events[-1]["data"]


def _slot(pid):
    p = asyncio.run(server.db.projects.find_one({"_id": server.ObjectId(pid)}))
    return p, (p.get("slots") or {}).get("full_wrap") or {}


def _sha(name):
    return hashlib.sha256((UPLOADS / name).read_bytes()).hexdigest()


def _agents_started(events):
    return [e["agent"] for e in events if e["type"] == "agent_start"]


# ---------------------------------------------------------------- happy path
def test_full_pipeline_fixes_confirms_and_prompts(client):
    pid = _upload_cover(client)
    _, before = _slot(pid)
    events, data = _run(client, pid)

    assert _agents_started(events) == [1, 2, 3, 4]                       # strictly in order
    assert data["pipeline"]["status"] == "confirmed"
    assert data["pipeline"]["prompt"] == "Fixed — press Enter for rescan and system confirmation"
    assert [e for e in events if e["type"] == "prompt"][0]["message"] == data["pipeline"]["prompt"]
    assert data["pipeline"]["health_before"] < 100 == data["pipeline"]["health_after"]
    assert events.index(next(e for e in events if e["type"] == "prompt")) > events.index(
        next(e for e in events if e["type"] == "agent_start" and e["agent"] == 4))   # prompt only after Agent 4

    # Evidence exists: problem shown BEFORE, per-check flip AFTER.
    assert [e for e in events if e["type"] == "issues_before"]
    flips = [e for e in events if e["type"] == "check_result"]
    assert flips and all(f["data"]["resolved"] for f in flips)
    assert any(f["data"]["id"] == "colorspace" for f in flips)

    # The saved file really is CMYK now (checked by opening it, not by trusting a report).
    _, after = _slot(pid)
    with Image.open(UPLOADS / after["stored_filename"]) as img:
        assert img.mode == "CMYK"
    assert (UPLOADS / after["original_stored_filename"]).exists()

    # Enter -> system confirmation re-runs the original scan fresh.
    r = client.post(f"/api/projects/{pid}/autofix/confirm", params={"slot": "full_wrap"})
    assert r.status_code == 200
    real = [c for c in r.json()["compliance"] if c["id"] not in common.INFORMATIONAL_IDS]
    assert real and all(c["status"] == "pass" for c in real)


def test_non_streaming_returns_full_audit_trail(client, monkeypatch):
    monkeypatch.setattr(server, "REQUEST_TIMEOUT_S", 150.0)   # this PC converts ~2x slower than production
    pid = _upload_cover(client)
    r = client.post(f"/api/projects/{pid}/autofix/verified", params={"slot": "full_wrap", "stream": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["pipeline"]["status"] == "confirmed"
    assert {e["agent"] for e in body["audit_trail"]} >= {0, 1, 2, 3, 4}
    assert body["file_metadata"]["color_mode"] == "CMYK" and body["compliance"]


def test_legacy_no_slot_path_works(client):
    pid = _upload_cover(client)
    events = []
    with client.stream("POST", f"/api/projects/{pid}/autofix/verified") as r:      # no ?slot=
        events = [json.loads(l) for l in r.iter_lines() if l.strip()]
    assert events[-1]["data"]["pipeline"]["status"] == "confirmed"


# ----------------------------------------------------------------- early exit
def test_clean_file_exits_early_without_touching_anything(client):
    pid = _upload_cover(client)
    _run(client, pid)                                   # fix it once
    _, fixed = _slot(pid)
    h = _sha(fixed["stored_filename"])
    events, data = _run(client, pid)                    # nothing left to reproduce
    assert data["pipeline"]["status"] == "no_action"
    assert _agents_started(events) == [1]               # Agents 2, 3, 4 never ran
    _, again = _slot(pid)
    assert again["stored_filename"] == fixed["stored_filename"] and _sha(again["stored_filename"]) == h


# --------------------------------------------------- refusing to keep a repair
def _assert_untouched(pid, before, before_hash):
    _, now = _slot(pid)
    assert now["stored_filename"] == before["stored_filename"]
    assert _sha(now["stored_filename"]) == before_hash
    assert not [n for n in os.listdir(UPLOADS) if n.startswith(pid) and n not in
                {before["stored_filename"], before.get("original_stored_filename") or before["stored_filename"]}]
    assert not [n for n in os.listdir(UPLOADS) if n.startswith(".bak_")]


def test_engine_crash_rolls_back(client, monkeypatch):
    pid = _upload_cover(client)
    _, before = _slot(pid)
    h = _sha(before["stored_filename"])

    def boom(*a, **k):
        raise RuntimeError("simulated conversion crash")
    monkeypatch.setattr(server, "convert_to_cmyk", boom)
    events, data = _run(client, pid)
    assert data["pipeline"]["status"] == "repair_failed"
    assert 3 not in _agents_started(events) and 4 not in _agents_started(events)
    _assert_untouched(pid, before, h)


def test_wrong_output_is_caught_by_verifier_and_rolled_back(client, monkeypatch):
    """Engine 'succeeds' but hands back an image with the wrong pixel size --
    only an independent check catches that."""
    pid = _upload_cover(client)
    _, before = _slot(pid)
    h = _sha(before["stored_filename"])
    real = server.convert_to_cmyk

    def shrinks(src, dst, dpi=300, **k):
        real(src, dst, dpi, **k)
        with Image.open(dst) as im:
            im.resize((im.width // 2, im.height // 2)).save(dst, "TIFF", dpi=(300, 300))
        return dst
    monkeypatch.setattr(server, "convert_to_cmyk", shrinks)
    events, data = _run(client, pid)
    assert data["pipeline"]["status"] == "rolled_back"
    assert 4 not in _agents_started(events)             # supervisor never asked to bless a rejected fix
    assert any(e["type"] == "rollback" for e in events)
    _assert_untouched(pid, before, h)


def test_no_progress_is_reverted(client, monkeypatch):
    pid = _upload_cover(client)
    _, before = _slot(pid)
    h = _sha(before["stored_filename"])

    def does_nothing(src, dst, dpi=300, **k):          # still RGB -> colour-space problem remains
        with Image.open(src) as im:
            im.convert("RGB").save(dst, "TIFF", dpi=(300, 300))
        return dst
    monkeypatch.setattr(server, "convert_to_cmyk", does_nothing)
    _, data = _run(client, pid)
    assert data["pipeline"]["status"] == "no_progress"
    _assert_untouched(pid, before, h)


def test_time_limits_lifted_slow_repair_still_completes(client, monkeypatch):
    """Owner's instruction: correct beats fast. With the limits switch off (the
    default), a repair that runs far past the old caps must still finish and be verified."""
    monkeypatch.delenv("SPARKPREP_TIME_LIMITS", raising=False)
    assert not common.time_limits_on()
    pid = _upload_cover(client)
    real = server.convert_to_cmyk

    def slow(*a, **k):
        time.sleep(8)
        return real(*a, **k)
    monkeypatch.setattr(server, "convert_to_cmyk", slow)
    monkeypatch.setattr(pipeline, "Budget", lambda total=None: common.Budget(total=3.0))   # would have cut it off at 5s
    _, data = _run(client, pid)
    assert data["pipeline"]["status"] == "confirmed", data["pipeline"]


def test_slow_repair_is_cut_off_within_budget_and_rolled_back(client, monkeypatch):
    monkeypatch.setenv("SPARKPREP_TIME_LIMITS", "on")          # restore the original guard rails
    pid = _upload_cover(client)
    _, before = _slot(pid)
    h = _sha(before["stored_filename"])
    real = server.convert_to_cmyk

    def slow(*a, **k):
        time.sleep(9)
        return real(*a, **k)
    monkeypatch.setattr(server, "convert_to_cmyk", slow)
    monkeypatch.setattr(pipeline, "Budget", lambda total=None: common.Budget(total=12.0))   # repair gets max(12-20, 5)=5s
    t = time.time()
    _, data = _run(client, pid)
    assert time.time() - t < 25       # Agent 1 scan + 5s repair cap + rollback; nowhere near a hang
    assert data["pipeline"]["status"] == "repair_failed"
    time.sleep(5)                                       # let the orphaned worker finish, then it must not matter
    p, now = _slot(pid)
    assert now["stored_filename"] == before["stored_filename"]


def test_second_run_while_first_running_is_refused(client):
    pid = _upload_cover(client)
    assert pipeline.try_claim(pid, "full_wrap")
    try:
        r = client.post(f"/api/projects/{pid}/autofix/verified", params={"slot": "full_wrap"})
        assert r.status_code == 409
    finally:
        pipeline._RUNNING.discard(f"{pid}:full_wrap")


def test_ownership_and_missing_file_are_rejected(client):
    assert client.post("/api/projects/000000000000000000000000/autofix/verified").status_code == 404
    pid = _project(client)                              # nothing uploaded
    assert client.post(f"/api/projects/{pid}/autofix/verified", params={"slot": "full_wrap"}).status_code == 404
    assert client.post(f"/api/projects/{pid}/autofix/verified", params={"slot": "bogus"}).status_code == 400


# ------------------------------------------------------------------ interior
def test_interior_pdf_pipeline_keeps_page_count(client):
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6.25 * inch, 9.25 * inch))
    for _ in range(3):
        c.setFont("Times-Roman", 11)
        for i in range(30):
            c.drawString(0.05 * inch, (9.1 - i * 0.28) * inch, "text hugging the very edge of the page " * 2)   # margin violation
        c.showPage()
    c.save()
    pid = _project(client, "interior")
    r = client.post(f"/api/projects/{pid}/slot-upload/interior", files={"file": ("b.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 200, r.text
    with client.stream("POST", f"/api/projects/{pid}/autofix/verified", params={"slot": "interior"}) as r:
        events = [json.loads(l) for l in r.iter_lines() if l.strip()]
    data = events[-1]["data"]
    assert data["pipeline"]["status"] in ("confirmed", "partial", "no_action", "no_progress"), data["pipeline"]
    p = asyncio.run(server.db.projects.find_one({"_id": server.ObjectId(pid)}))
    import pymupdf
    with pymupdf.open(str(UPLOADS / p["slots"]["interior"]["stored_filename"])) as d:
        assert d.page_count == 3


# ------------------------------------------------- separation of duties (unit)
def test_agent2_cannot_verify_its_own_work():
    """Structural guard: the repair agent's source must not reference any
    scanning/verification machinery, and 1 & 3 must not reach into repair."""
    def names(mod):
        tree = ast.parse((BACKEND / "autofix_agents" / mod).read_text(encoding="utf-8"))
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                out.update(a.name for a in n.names)
                if isinstance(n, ast.ImportFrom) and n.module:
                    out.add(n.module)
        return out
    forbidden = ("scan", "compliance", "verify", "normalize_issues", "health_score", "analyze", "diagnose", "supervise")
    bad = [n for n in names("agent2_repair.py") if any(f in n.lower() for f in forbidden)]
    assert not bad, f"Agent 2 references verification machinery: {bad}"
    for mod in ("agent1_triage.py", "agent3_verify.py", "agent4_supervisor.py"):
        tree = ast.parse((BACKEND / "autofix_agents" / mod).read_text(encoding="utf-8"))
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                imported.update([n.module or ""] + [a.name for a in n.names])
            elif isinstance(n, ast.Import):
                imported.update(a.name for a in n.names)
        assert not [i for i in imported if "repair" in i.lower()], f"{mod} imports the repair agent: {imported}"


def test_agent2_strips_the_engines_self_grade():
    trail = common.Trail()

    async def engine():
        return {"compliance": [{"id": "x", "status": "pass"}], "ghostscript_fix": None, "interior_margin_fix": None, "file_metadata": {}}
    from autofix_agents.agent2_repair import repair
    out = asyncio.run(repair(repair_fn=engine, diagnosis={"fixable": []}, trail=trail, timeout_s=5))
    assert "compliance" not in out and "file_metadata" not in out


def test_supervisor_rejects_a_file_changed_after_verification(client):
    """Agent 4 fingerprints the saved file; if it differs from what Agent 3 verified it must refuse."""
    from autofix_agents.agent4_supervisor import supervise
    pid = _upload_cover(client)
    _, slot = _slot(pid)
    trail = common.Trail()
    for a in (1, 2, 3):
        trail.start_agent(a, "x")
        trail.finish_agent(a, {})
    verification = {"verdict": "approved", "file": slot["stored_filename"], "sha256": "0" * 64, "scanned_at": trail.now()}

    async def getp():
        return await server.db.projects.find_one({"_id": server.ObjectId(pid)})
    out = asyncio.run(supervise(get_project=getp, slot="full_wrap", resolve_target=server._resolve_slot_target,
                                upload_dir=UPLOADS, snapshot={"other_slots": {}, "user_id": (asyncio.run(getp()))["user_id"]},
                                diagnosis={}, repair={"status": "applied"}, verification=verification, trail=trail,
                                budget=common.Budget()))
    assert out["decision"] == "rejected"
    assert [c for c in out["criteria"] if c["id"] == "verified_file_is_saved_file" and not c["passed"]]


def test_supervisor_ai_review_is_advisory_only(client):
    from autofix_agents.agent4_supervisor import supervise
    pid = _upload_cover(client)
    _, slot = _slot(pid)
    trail = common.Trail()
    for a in (1, 2, 3):
        trail.start_agent(a, "x")
        trail.finish_agent(a, {})

    async def getp():
        return await server.db.projects.find_one({"_id": server.ObjectId(pid)})
    good = {"verdict": "approved", "file": slot["stored_filename"], "sha256": _sha(slot["stored_filename"]), "scanned_at": trail.now()}
    uid = asyncio.run(getp())["user_id"]

    async def alarmist(_):
        return {"summary": "looks fishy", "concerns": ["everything"]}

    async def broken(_):
        raise RuntimeError("api down")
    for reviewer, expect_review in ((alarmist, True), (broken, False), (None, False)):
        out = asyncio.run(supervise(get_project=getp, slot="full_wrap", resolve_target=server._resolve_slot_target,
                                    upload_dir=UPLOADS, snapshot={"other_slots": {}, "user_id": uid}, diagnosis={},
                                    repair={"status": "applied"}, verification=good, trail=trail, budget=common.Budget(),
                                    ai_review=reviewer))
        assert out["decision"] == "confirmed"           # the AI can neither block nor bless
        assert bool(out["ai_review"]) == expect_review


def test_ink_checker_does_not_invent_overshoot_at_hard_edges(tmp_path):
    """Regression: a file whose every pixel is <= 240% total ink must PASS the checker even with
    razor-sharp edges (the old bicubic shrink overshot at edges and reported phantom 260%+ pixels)."""
    import numpy as np
    import file_processor as fp
    n = 2400                                            # bigger than the checker's 1200px sample -> forces a shrink
    arr = np.zeros((n, n, 4), dtype=np.uint8)
    arr[:, (np.arange(n) // 150) % 2 == 0, :3] = 204    # 150px bars at exactly 240% total ink (204*3/255), alternating with white
    # (with the old bicubic shrink this reported a phantom 242% over 1.25% of the area -- a false "still broken")
    path = tmp_path / "hard_edges.tif"
    Image.fromarray(arr, mode="CMYK").save(path, "TIFF")
    assert np.array(Image.open(path)).astype(float).sum(axis=-1).max() / 255 * 100 <= 240.0
    assert fp.check_total_ink_coverage(str(path), False, "kdp", "KDP") is None


def test_broken_ocr_is_loud_not_silent(client, monkeypatch, caplog, tmp_path):
    """The cover text-margin check must never skip in silence: a Tesseract failure is logged as an
    error, and /api/health reports the OCR engine's state."""
    import logging
    import pytesseract
    import pdfx_validator as pv
    img = tmp_path / "c.jpg"
    Image.new("RGB", (600, 400), (255, 255, 255)).save(img, "JPEG")

    def boom(*a, **k):
        raise RuntimeError("tesseract is not installed")
    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    with caplog.at_level(logging.ERROR, logger="sparkprep.ocr"):
        assert pv.check_cover_safety_margins(str(img), False, 6.25, 9.25, "KDP") == []
    assert any("SKIPPED" in r.message for r in caplog.records)

    h = client.get("/api/health").json()
    assert h["status"] == "ok" and "available" in h["ocr"]

    def no_binary():
        raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(pytesseract, "get_tesseract_version", no_binary)
    pv._OCR_STATUS_CACHE.update(at=0.0, value=None)
    assert client.get("/api/health").json()["ocr"]["available"] is False
    pv._OCR_STATUS_CACHE.update(at=0.0, value=None)


def _reference_cmyk(pil_img, tac_limit):
    """The ORIGINAL whole-image path (unchanged public functions) -- ground truth for the banded version."""
    import numpy as np
    import file_processor as fp
    if pil_img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", pil_img.size, (255, 255, 255))
        bg.paste(pil_img, mask=pil_img.split()[-1])
        pil_img = bg
    elif pil_img.mode in ("P", "L"):
        pil_img = pil_img.convert("RGB")
    if pil_img.mode == "RGB":
        arr = fp.rgb_array_to_cmyk_array(np.array(pil_img))
    elif pil_img.mode != "CMYK":
        arr = np.array(pil_img.convert("CMYK"))
    else:
        arr = np.array(pil_img)
    return fp.clamp_total_ink_coverage(arr, tac_limit)


def test_banded_cmyk_conversion_is_pixel_identical_to_whole_image(tmp_path, monkeypatch):
    """The memory-safe banded conversion must produce EXACTLY the same pixels as the old whole-image path,
    for every input mode, odd sizes (band boundaries), extreme colours, and both ink limits."""
    import numpy as np
    import file_processor as fp
    monkeypatch.setattr(fp, "_CMYK_BAND_PIXELS", 5000)          # force many bands even on small test images
    rng = np.random.default_rng(7)
    cases = {
        "noise RGB odd size": Image.fromarray(rng.integers(0, 256, (137, 251, 3), dtype=np.uint8), "RGB"),
        "noise RGB tall 1px wide": Image.fromarray(rng.integers(0, 256, (333, 1, 3), dtype=np.uint8), "RGB"),
        "noise RGB wide 1px tall": Image.fromarray(rng.integers(0, 256, (1, 900, 3), dtype=np.uint8), "RGB"),
        "pure black": Image.new("RGB", (64, 300), (0, 0, 0)),
        "pure white": Image.new("RGB", (64, 300), (255, 255, 255)),
        "dark saturated": Image.new("RGB", (200, 151), (6, 4, 40)),
        "RGBA with transparency": Image.fromarray(rng.integers(0, 256, (120, 90, 4), dtype=np.uint8), "RGBA"),
        "grayscale L": Image.fromarray(rng.integers(0, 256, (101, 77), dtype=np.uint8), "L"),
        "palette P": Image.fromarray(rng.integers(0, 256, (101, 77), dtype=np.uint8), "L").convert("P"),
        "already CMYK": Image.fromarray(rng.integers(0, 256, (145, 133, 4), dtype=np.uint8), "CMYK"),
    }
    for n, (name, im) in enumerate(cases.items()):
        src = tmp_path / f"in{n}.tif"
        im.save(src)
        with Image.open(src) as reloaded:
            for limit in (240, 270):
                expected = _reference_cmyk(reloaded.copy(), limit)
                out = tmp_path / f"out{n}_{limit}.tif"
                fp.convert_to_cmyk(str(src), str(out), 300, tac_limit=limit)
                with Image.open(out) as res:
                    assert res.mode == "CMYK" and res.size == reloaded.size, name
                    assert (np.array(res) == expected).all(), f"banded output differs from whole-image output: {name} @ {limit}%"


def test_ink_checker_is_exact_for_cmyk_files(tmp_path):
    """A CMYK file whose every pixel is <= the limit must never be flagged (old shrink rounding reported 241.6%
    over 99.8% of such a file), while a file genuinely over the limit must still be flagged with the right peak."""
    import numpy as np
    import file_processor as fp
    rng = np.random.default_rng(1)
    n = 2400
    c, m, y = (rng.integers(120, 205, (n, n)) for _ in range(3))
    k = 612 - (c + m + y)                                   # every pixel totals exactly 240%
    ok = np.stack([c, m, y, k], -1).astype(np.uint8)
    p_ok = tmp_path / "at_limit.tif"
    Image.fromarray(ok, mode="CMYK").save(p_ok, "TIFF")
    assert fp.check_total_ink_coverage(str(p_ok), False, "kdp", "KDP") is None

    bad = ok.copy()
    bad[: n // 10, :, :3] = 255                              # top 10% of the file: C=M=Y=100% (+K) -> 300%+
    p_bad = tmp_path / "over_limit.tif"
    Image.fromarray(bad, mode="CMYK").save(p_bad, "TIFF")
    finding = fp.check_total_ink_coverage(str(p_bad), False, "kdp", "KDP")
    assert finding is not None and finding["id"] == "total_ink_coverage"
    true_peak = int(bad.astype(np.int32).sum(-1).max()) / 255 * 100
    assert f"{round(true_peak)}%" in finding["title"]


def test_audit_workflow_never_repairs():
    """The audit only IDENTIFIES problems and where they are; it must never call any repair/fix function.
    Repairs belong to the Repair Bay only."""
    import inspect
    import re
    src = inspect.getsource(server.audit_upload) + inspect.getsource(server.batch_audit)
    repair_calls = ["convert_to_cmyk", "autofix_cover_safe_margin", "autofix_interior_safety_margins", "convert_to_pdfx1a",
                    "clamp_total_ink_coverage", "build_print_ready_pdf", "build_interior_pdf_x1a", "upscale_to_size", "autofix_agents", "autofix("]
    hit = [r for r in repair_calls if re.search(re.escape(r), src)]
    assert not hit, f"audit code references repair functions: {hit}"
