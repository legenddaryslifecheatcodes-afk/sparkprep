"""Book files are kept 7 days, then erased for good (owner, 2026-11-27: "at exactly 7 days, that file erases
forever ... SparkPrep holds no responsibility for files not managed properly").

What counts: everything an author runs through SparkPrep -- a book's uploads, its repaired versions and its
print-ready exports (DATA_DIR/uploads, DATA_DIR/exports), and the files of a free check. A book's clock starts at
its FIRST upload, so its repaired files and exports go with it on the same day.

Two guards, both only ever keep a file longer, never shorter:
- A paid book's export window (book_pass, 7 days from when the book was started) is never cut short: its files
  stay until that window ends, so nobody loses downloads they paid for.
- Files that were already on the server before this rule went live get their 7 days counted from RULE_START,
  because those authors were never shown the notice.

Not touched: Cover Studio design settings (<project>_cover_design.json), Author Studio writing, nightly backups.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

KEEP_DAYS = int(os.environ.get("FILE_KEEP_DAYS", "7"))
RULE_START = datetime.fromisoformat(os.environ.get("FILE_ERASE_RULE_START", "2026-11-28T00:00:00+00:00"))
KEEP_NAMES_ENDING = ("_cover_design.json",)

NOTICE = ("SparkPrep keeps your book files for 7 days after you upload them. After that they are erased for good "
          "and can't be brought back, so save your files now. SparkPrep isn't responsible for files that weren't "
          "saved in time. In special cases, if you tell us before the 7 days are up, we may keep a file longer, "
          "at SparkPrep's choice.")


def file_time(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def clock_start(times: Iterable[Optional[datetime]]) -> Optional[datetime]:
    """When the 7 days began: the first upload, but never before the rule went live."""
    known = [t for t in times if t is not None]
    if not known:
        return None
    return max(min(known), RULE_START)


def erase_at(start: Optional[datetime], paid_window_ends: Optional[datetime] = None) -> Optional[datetime]:
    if start is None:
        return None
    due = start + timedelta(days=KEEP_DAYS)
    if paid_window_ends and paid_window_ends > due:
        due = paid_window_ends
    return due


def is_due(start: Optional[datetime], now: datetime, paid_window_ends: Optional[datetime] = None) -> bool:
    due = erase_at(start, paid_window_ends)
    return due is not None and now >= due


def kept_anyway(path: Path) -> bool:
    return path.name.endswith(KEEP_NAMES_ENDING)
