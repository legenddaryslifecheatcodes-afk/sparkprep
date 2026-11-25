"""The members-only Library: exclusive articles (and videos) for Author Studio members.

Articles are JSON files in author_studio/library/ so every one is reviewed in GitHub before it goes up (trey's
rule: everything lives in GitHub). They are served ONLY through the members-only API, never as public pages,
so Google and non-members can't read them. A file with a future `publish_on` date stays hidden until that day
(Pacific time), which lets a week of articles be approved at once and appear 3 times a week by themselves.

Article file shape:
{"slug": "...", "title": "...", "summary": "...", "category": "marketing|launch|publishing|business|craft",
 "publish_on": "YYYY-MM-DD", "minutes": 6, "video_url": null, "body": [ {"h": "..."} | {"p": "..."} |
 {"list": ["..."]} | {"tip": "..."} ]}
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

LIBRARY_DIR = Path(__file__).resolve().parent / "library"
CATEGORIES = {
    "marketing": "Marketing and promotion",
    "launch": "Launch week",
    "publishing": "Publishing know-how",
    "business": "The author business",
    "craft": "Writing craft",
}


def _today_pacific() -> date:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def load_all(directory: Path = LIBRARY_DIR) -> List[dict]:
    out = []
    for f in sorted(directory.glob("*.json")):
        try:
            a = json.loads(f.read_text(encoding="utf-8"))
            date.fromisoformat(a["publish_on"])
            if a.get("slug") and a.get("title") and isinstance(a.get("body"), list):
                out.append(a)
        except (ValueError, KeyError, OSError):
            continue                      # a broken file must never take the Library down
    return out


def published(today: Optional[date] = None, directory: Path = LIBRARY_DIR) -> List[dict]:
    today = today or _today_pacific()
    items = [a for a in load_all(directory) if date.fromisoformat(a["publish_on"]) <= today]
    return sorted(items, key=lambda a: (a["publish_on"], a["slug"]), reverse=True)


def card(a: dict) -> dict:
    return {"slug": a["slug"], "title": a["title"], "summary": a.get("summary"), "category": a.get("category"),
            "category_label": CATEGORIES.get(a.get("category"), "Library"), "publish_on": a["publish_on"],
            "minutes": a.get("minutes"), "has_video": bool(a.get("video_url"))}
