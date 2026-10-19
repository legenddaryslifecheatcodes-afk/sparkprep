"""The 24-hour Open House: SparkPrep free for everyone, no sign-up, no paywall, for one fixed window.

While it's on, every paid gate on the server lets people through (the same way it already lets beta testers
through), the no-account audit shows its full results, and a visitor can start working as a guest without
creating an account. When the window ends, everything goes back to normal on its own -- nothing is deleted.

Three ways to control it, strongest first:
1. The admin switch (POST /api/admin/open-house {"mode": "off"}) -- takes effect within CACHE_SECONDS, no redeploy.
2. The OPEN_HOUSE env var: "off" (never on), "on" (on now, ignoring the clock) or "auto" (default: follow the clock).
3. The clock: OPEN_HOUSE_START / OPEN_HOUSE_END (ISO times with offset). Default: noon Mon Oct 19 2026 to noon
   Tue Oct 20 2026, Pacific time.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

DEFAULT_START = "2026-10-19T12:00:00-07:00"
DEFAULT_END = "2026-10-20T12:00:00-07:00"
MODES = ("auto", "on", "off")
SETTINGS_ID = "open_house"
CACHE_SECONDS = 15

_cache: dict = {"at": 0.0, "mode": None}


def _parse(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def window() -> tuple[datetime, datetime]:
    return (_parse(os.environ.get("OPEN_HOUSE_START") or DEFAULT_START),
            _parse(os.environ.get("OPEN_HOUSE_END") or DEFAULT_END))


def env_mode() -> str:
    m = (os.environ.get("OPEN_HOUSE") or "auto").strip().lower()
    return m if m in MODES else "auto"


async def admin_mode(db) -> Optional[str]:
    """The admin switch's setting, if one was ever saved (cached briefly so every request isn't a DB read)."""
    if time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["mode"]
    doc = await db.settings.find_one({"_id": SETTINGS_ID})
    mode = doc.get("mode") if doc and doc.get("mode") in MODES else None
    _cache.update(at=time.monotonic(), mode=mode)
    return mode


async def set_admin_mode(db, mode: str, by: str) -> None:
    doc = {"mode": mode, "updated_by": by, "updated_at": datetime.now(timezone.utc).isoformat()}
    if await db.settings.find_one({"_id": SETTINGS_ID}):
        await db.settings.update_one({"_id": SETTINGS_ID}, {"$set": doc})
    else:
        await db.settings.insert_one({"_id": SETTINGS_ID, **doc})
    _cache.update(at=time.monotonic(), mode=mode)


def _decide(mode: str, now: datetime) -> bool:
    if mode == "off":
        return False
    if mode == "on":
        return True
    start, end = window()
    return start <= now < end


async def is_active(db, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if env_mode() == "off":                          # the env kill switch always wins
        return False
    return _decide(await admin_mode(db) or env_mode(), now)


async def status(db) -> dict:
    start, end = window()
    return {"active": await is_active(db), "starts_at": start.isoformat(), "ends_at": end.isoformat(),
            "mode": await admin_mode(db) or env_mode()}


def reset_cache() -> None:
    _cache.update(at=0.0, mode=None)
