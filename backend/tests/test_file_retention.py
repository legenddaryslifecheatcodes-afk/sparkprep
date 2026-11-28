"""The 7-day file erase (owner, 2026-11-27). See file_retention.py. In-process, in-memory database."""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="sp_fr_"), USE_MEMORY_DB="1",
                  JWT_SECRET=os.environ.get("JWT_SECRET") or "fr-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bson import ObjectId  # noqa: E402

import file_retention  # noqa: E402
import server  # noqa: E402

NOW = datetime.now(timezone.utc)


def _file(path: Path, days_old: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 test")
    t = time.time() - days_old * 86400
    os.utime(path, (t, t))
    return path


async def _book(days_old: float, **extra) -> tuple[str, list]:
    oid = ObjectId()
    pid = str(oid)
    up = _file(server.UPLOAD_DIR / f"{pid}_interior_abc.pdf", days_old)
    fixed = _file(server.UPLOAD_DIR / f"{pid}_interior_fixed_def.pdf", max(days_old - 1, 0))
    export = _file(server.EXPORT_DIR / f"{pid}_interior_x1.pdf", max(days_old - 2, 0))
    design = _file(server.UPLOAD_DIR / f"{pid}_cover_design.json", days_old)
    await server.db.projects.insert_one({"_id": oid, "user_id": "u1", "name": "Test book",
                                         "slots": {"interior": {"stored_filename": fixed.name,
                                                                "original_stored_filename": up.name}},
                                         "uploaded_file": None, "file_metadata": None, "compliance": [], **extra})
    return pid, [up, fixed, export, design]


@pytest.fixture(autouse=True)
def rule_started_long_ago(monkeypatch):
    monkeypatch.setattr(file_retention, "RULE_START", NOW - timedelta(days=365))
    server.db.projects._docs.clear()
    server.db.audits._docs.clear()
    server.db.book_credits._docs.clear()
    server.db.settings._docs.clear()


def run():
    return asyncio.run(server.run_file_retention(NOW))


def test_a_book_is_erased_7_days_after_its_first_upload_with_its_fixes_and_exports():
    pid, (up, fixed, export, design) = asyncio.run(_book(7.2))
    result = run()
    assert not up.exists() and not fixed.exists() and not export.exists()
    assert design.exists()                                   # Cover Studio design settings are not a book file
    p = asyncio.run(server.db.projects.find_one({"_id": ObjectId(pid)}))
    assert p["slots"] == {} and p["files_erased_at"]
    assert p["name"] == "Test book"                          # the book itself stays on the author's dashboard
    assert result["books"] == 1


def test_a_book_younger_than_7_days_is_kept():
    _, files = asyncio.run(_book(6.5))
    run()
    assert all(f.exists() for f in files)


def test_a_paid_book_keeps_its_files_until_its_export_window_ends():
    pid, files = asyncio.run(_book(7.5))
    asyncio.run(server.db.book_credits.insert_one({
        "user_id": "u1", "project_id": pid, "started_at": (NOW - timedelta(days=2)).isoformat(),
        "window_ends_at": (NOW + timedelta(days=5)).isoformat(), "created_at": NOW.isoformat()}))
    run()
    assert all(f.exists() for f in files)


def test_a_special_hold_keeps_the_files_longer():
    _, files = asyncio.run(_book(9, keep_files_until=(NOW + timedelta(days=3)).isoformat()))
    run()
    assert all(f.exists() for f in files)


def test_files_already_there_get_7_days_from_when_the_rule_went_live(monkeypatch):
    monkeypatch.setattr(file_retention, "RULE_START", NOW - timedelta(days=2))
    _, files = asyncio.run(_book(30))
    run()
    assert all(f.exists() for f in files)


def test_free_check_files_are_erased_after_7_days():
    old_id, new_id = "a" * 32, "b" * 32
    old = _file(server.UPLOAD_DIR / f"audit_{old_id}_cover.pdf", 8)
    new = _file(server.UPLOAD_DIR / f"audit_{new_id}_cover.pdf", 1)
    for aid, days in ((old_id, 8), (new_id, 1)):
        asyncio.run(server.db.audits.insert_one({"audit_id": aid,
                                                 "created_at": (NOW - timedelta(days=days)).isoformat()}))
    run()
    assert not old.exists() and new.exists()
    a = asyncio.run(server.db.audits.find_one({"audit_id": old_id}))
    assert a["files_deleted_at"]


def test_leftover_files_with_no_book_are_erased_after_7_days_too():
    stale = _file(server.UPLOAD_DIR / "leftover_tmp_123.png", 8)
    fresh = _file(server.EXPORT_DIR / "leftover_tmp_456.pdf", 1)
    run()
    assert not stale.exists() and fresh.exists()


def test_the_notice_says_7_days_and_no_responsibility():
    n = file_retention.NOTICE
    assert "7 days" in n and "erased for good" in n and "isn't responsible" in n


def _pdf_text(path) -> str:
    from pypdf import PdfReader
    return " ".join(pg.extract_text() for pg in PdfReader(str(path)).pages).replace("\n", " ")


def test_both_report_pdfs_end_with_the_7_day_reminder(tmp_path):
    import report_export
    report_export.generate_preflight_report_pdf({"certified": False, "checks": [], "still_open": []}, [],
                                                 {"platform": "Amazon KDP", "title": "T"}, str(tmp_path / "r.pdf"))
    report_export.generate_audit_brief_pdf([], {}, {"audit_label": "Audit"}, str(tmp_path / "a.pdf"))
    for name in ("r.pdf", "a.pdf"):
        text = _pdf_text(tmp_path / name)
        assert "7 days after you upload them" in text and "erased for good" in text
