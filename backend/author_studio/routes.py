"""HTTP endpoints for the Legenddary SparkPrep Author Studio (members' workspace, 2026-11-25).

Built with injected dependencies (no import of server.py), like book_pass. Only plain equality filters, $set
and $inc are used, so it runs the same on MongoDB and on the in-memory dev database.

Who can do what:
- Members (active $9.99/month membership) and the owner can create and change everything.
- Anyone who once was a member can still open and download what they stored (files are never held hostage),
  but can't add or change anything until they rejoin.
"""
from __future__ import annotations

import mimetypes
import os
import re
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from book_pass import entitlements as ent

from . import config as C
from . import library as L
from .storage import make_storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _oid(s: str) -> ObjectId:
    try:
        return ObjectId(s)
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "Not found")


def _clean(s: Optional[str], n: int = 200) -> Optional[str]:
    if s is None:
        return None
    return s.strip()[:n]


class BookIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    subtitle: Optional[str] = None
    genre: Optional[str] = None
    book_type: Optional[str] = None
    target_words: Optional[int] = Field(None, ge=0, le=2_000_000)
    project_id: Optional[str] = None


class BookPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    subtitle: Optional[str] = None
    genre: Optional[str] = None
    book_type: Optional[str] = None
    target_words: Optional[int] = Field(None, ge=0, le=2_000_000)
    chapters_total: Optional[int] = Field(None, ge=0, le=2000)
    chapters_done: Optional[int] = Field(None, ge=0, le=2000)
    daily_goal: Optional[int] = Field(None, ge=0, le=100_000)
    weekly_goal: Optional[int] = Field(None, ge=0, le=700_000)
    project_id: Optional[str] = None          # "" unlinks
    launch_date: Optional[str] = None         # YYYY-MM-DD, "" clears
    manuscript_done: Optional[bool] = None


class WordsIn(BaseModel):
    total: Optional[int] = Field(None, ge=0, le=5_000_000)   # the manuscript's word count now
    added: Optional[int] = Field(None, ge=-500_000, le=500_000)  # or: words written today
    day: Optional[str] = None


class CheckIn(BaseModel):
    key: str
    done: bool


class NoteIn(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = ""
    kind: Optional[str] = "note"              # note | template (made from a template)


class TaskIn(BaseModel):
    label: Optional[str] = None
    due: Optional[str] = None
    group: Optional[str] = None
    done: Optional[bool] = None


class PlanIn(BaseModel):
    launch_date: str


class LedgerIn(BaseModel):
    kind: str                                 # expense | marketing | sale
    label: str = Field(..., min_length=1, max_length=200)
    amount_cents: int = Field(..., ge=0, le=100_000_000)
    day: Optional[str] = None
    copies: Optional[int] = Field(None, ge=0, le=10_000_000)


def _valid_day(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        raise HTTPException(400, "Dates look like 2026-12-31")


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    base = re.sub(r"[^\w.\- ()]+", "_", base).strip() or "file"
    return base[:150]


# ------------------------------------------------------------------ progress maths (pure, easy to test)
def writing_progress(b: dict) -> int:
    if b.get("manuscript_done"):
        return 100
    target = b.get("target_words") or 0
    if target > 0:
        return max(0, min(100, round(100 * (b.get("words_written") or 0) / target)))
    total = b.get("chapters_total") or 0
    if total > 0:
        return max(0, min(100, round(100 * (b.get("chapters_done") or 0) / total)))
    return 0


def readiness(b: dict, project: Optional[dict]) -> List[dict]:
    """The 7 readiness steps, each done / in_progress / not_started. A linked SparkPrep book fills in what the
    server already knows (files uploaded, the final check) so the author doesn't tick it twice."""
    checks = b.get("checklist") or {}
    by_step: dict = {}
    for item in C.CHECKLIST:
        by_step.setdefault(item["step"], []).append(bool(checks.get(item["key"])))
    slots = (project or {}).get("slots") or {}
    final = ((project or {}).get("last_final_review") or {}).get("status")
    out = []
    for step in C.READINESS_STEPS:
        k = step["key"]
        if k == "manuscript":
            wp = writing_progress(b)
            state = "done" if wp >= 100 else ("in_progress" if wp > 0 else "not_started")
            source = None
        else:
            marks = by_step.get(k, [])
            state = "done" if marks and all(marks) else ("in_progress" if any(marks) else "not_started")
            source = None
            if project:
                if k == "cover" and any(slots.get(s) for s in ("front_cover", "full_wrap", "case_wrap")) and state != "done":
                    state, source = "in_progress", "Cover uploaded to your SparkPrep book"
                if k == "interior" and slots.get("interior") and state != "done":
                    state, source = "in_progress", "Inside pages uploaded to your SparkPrep book"
                if k == "inspection":
                    if final == "green":
                        state, source = "done", "Passed the final SparkPrep check"
                    elif final in ("yellow", "red") and state == "not_started":
                        state, source = "in_progress", "Final check run, some items to fix"
        out.append({**step, "state": state, "source": source})
    return out


def publishing_progress(steps: List[dict]) -> int:
    if not steps:
        return 0
    score = sum(1 if s["state"] == "done" else 0.4 if s["state"] == "in_progress" else 0 for s in steps)
    return round(100 * score / len(steps))


def next_step(steps: List[dict]) -> Optional[dict]:
    for s in steps:
        if s["state"] != "done":
            return s
    return None


def word_stats(b: dict, today: Optional[date] = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    log = {e["day"]: e["words"] for e in (b.get("word_log") or [])}
    week_start = today - timedelta(days=today.weekday())
    this_week = sum(w for d, w in log.items() if week_start.isoformat() <= d <= today.isoformat())
    streak, d = 0, today
    if log.get(d.isoformat(), 0) <= 0:
        d -= timedelta(days=1)            # today not written yet: the streak still counts up to yesterday
    while log.get(d.isoformat(), 0) > 0:
        streak += 1
        d -= timedelta(days=1)
    last14 = [{"day": (today - timedelta(days=i)).isoformat(), "words": log.get((today - timedelta(days=i)).isoformat(), 0)}
              for i in range(13, -1, -1)]
    return {"today": log.get(today.isoformat(), 0), "this_week": this_week, "streak_days": streak, "last_14_days": last14}


# ------------------------------------------------------------------ router
def build_router(*, db, get_current_user, stripe, data_dir: Path, is_admin) -> APIRouter:
    router = APIRouter(prefix="/studio")
    storage = make_storage(data_dir)

    async def _member(user: dict) -> bool:
        if is_admin(user):
            return True
        await ent.refresh_member_status(db, stripe, user["id"])
        return await ent.is_member(db, user["id"])

    async def _require_member(user: dict):
        if not await _member(user):
            raise HTTPException(403, {"code": "members_only",
                                      "msg": "The Author Studio is part of the Legenddary SparkPrep membership."})

    async def _book(book_id: str, user: dict) -> dict:
        b = await db.studio_books.find_one({"_id": _oid(book_id), "user_id": user["id"]})
        if not b:
            raise HTTPException(404, "Book not found")
        return b

    async def _project(b: dict, user: dict) -> Optional[dict]:
        pid = b.get("project_id")
        if not pid:
            return None
        try:
            return await db.projects.find_one({"_id": ObjectId(pid), "user_id": user["id"]})
        except Exception:  # noqa: BLE001
            return None

    async def _used_bytes(user_id: str) -> int:
        total = 0
        async for f in db.studio_files.find({"user_id": user_id}):
            if not f.get("shares_key_of"):
                total += f.get("size") or 0
        return total

    def _file_out(f: dict) -> dict:
        return {"id": str(f["_id"]), "group_id": f["group_id"], "folder": f["folder"], "name": f["name"],
                "version": f["version"], "size": f.get("size") or 0, "note": f.get("note"),
                "created_at": f.get("created_at"), "restored_from": f.get("restored_from")}

    async def _files(b: dict) -> List[dict]:
        groups: dict = {}
        async for f in db.studio_files.find({"book_id": str(b["_id"])}):
            groups.setdefault(f["group_id"], []).append(f)
        out = []
        for gid, versions in groups.items():
            versions.sort(key=lambda f: f["version"], reverse=True)
            cur = versions[0]
            out.append({"group_id": gid, "folder": cur["folder"], "name": cur["name"], "current": _file_out(cur),
                        "versions": [_file_out(v) for v in versions], "updated_at": cur.get("created_at")})
        out.sort(key=lambda g: g["updated_at"] or "", reverse=True)
        return out

    async def _summary(b: dict, user: dict, project: Optional[dict] = None, *, full: bool = False) -> dict:
        if project is None:
            project = await _project(b, user)
        steps = readiness(b, project)
        out = {
            "id": str(b["_id"]), "title": b.get("title"), "subtitle": b.get("subtitle"), "genre": b.get("genre"),
            "book_type": b.get("book_type"), "target_words": b.get("target_words") or 0,
            "words_written": b.get("words_written") or 0, "chapters_total": b.get("chapters_total") or 0,
            "chapters_done": b.get("chapters_done") or 0, "daily_goal": b.get("daily_goal") or 0,
            "weekly_goal": b.get("weekly_goal") or 0, "manuscript_done": bool(b.get("manuscript_done")),
            "launch_date": b.get("launch_date"), "project_id": b.get("project_id"),
            "project_name": (project or {}).get("name"),
            "writing_progress": writing_progress(b), "publishing_progress": publishing_progress(steps),
            "next_step": next_step(steps), "updated_at": b.get("updated_at"), "created_at": b.get("created_at"),
            "file_count": await db.studio_files.count_documents({"book_id": str(b["_id"])}),
        }
        if full:
            ledger = b.get("ledger") or []
            out.update(
                readiness=steps, checklist=b.get("checklist") or {}, words=word_stats(b),
                notes=sorted(b.get("notes") or [], key=lambda n: n.get("updated_at") or "", reverse=True),
                launch_tasks=sorted(b.get("launch_tasks") or [], key=lambda t: (t.get("due") or "9999", t.get("label") or "")),
                ledger=sorted(ledger, key=lambda e: e.get("day") or "", reverse=True),
                totals={k: sum(e["amount_cents"] for e in ledger if e["kind"] == k) for k in ("expense", "marketing", "sale")},
                copies_sold=sum(e.get("copies") or 0 for e in ledger if e["kind"] == "sale"),
                files=await _files(b),
            )
        return out

    async def _save(b: dict, **fields):
        fields["updated_at"] = _now()
        await db.studio_books.update_one({"_id": b["_id"]}, {"$set": fields})

    # ---------------------------------------------------------------- status + library
    @router.get("/status")
    async def status(user: dict = Depends(get_current_user)):
        member = await _member(user)
        used = await _used_bytes(user["id"])
        return {"member": member, "has_books": await db.studio_books.count_documents({"user_id": user["id"]}) > 0,
                "storage": {"used_bytes": used, "quota_bytes": C.QUOTA_MB * 1024 * 1024, "where": storage.kind,
                            "max_file_mb": C.MAX_FILE_MB},
                "folders": C.FOLDERS, "checklist": C.CHECKLIST, "readiness_steps": C.READINESS_STEPS}

    @router.get("/books")
    async def list_books(user: dict = Depends(get_current_user)):
        items = [await _summary(b, user) async for b in db.studio_books.find({"user_id": user["id"]}).sort("updated_at", -1)]
        return {"books": items, "member": await _member(user)}

    @router.post("/books")
    async def create_book(payload: BookIn, user: dict = Depends(get_current_user)):
        await _require_member(user)
        if await db.studio_books.count_documents({"user_id": user["id"]}) >= C.MAX_BOOKS:
            raise HTTPException(400, f"You can keep up to {C.MAX_BOOKS} books in the Author Studio.")
        if payload.project_id:
            try:
                ok = await db.projects.find_one({"_id": ObjectId(payload.project_id), "user_id": user["id"]})
            except Exception:  # noqa: BLE001
                ok = None
            if not ok:
                raise HTTPException(404, "That SparkPrep book wasn't found")
        now = _now()
        doc = {"user_id": user["id"], "title": payload.title.strip(), "subtitle": _clean(payload.subtitle),
               "genre": _clean(payload.genre, 80), "book_type": _clean(payload.book_type, 80),
               "target_words": payload.target_words or 0, "words_written": 0, "chapters_total": 0, "chapters_done": 0,
               "daily_goal": 0, "weekly_goal": 0, "manuscript_done": False, "project_id": payload.project_id or None,
               "launch_date": None, "checklist": {}, "word_log": [], "notes": [], "launch_tasks": [], "ledger": [],
               "created_at": now, "updated_at": now}
        res = await db.studio_books.insert_one(doc)
        doc["_id"] = res.inserted_id
        return await _summary(doc, user, full=True)

    @router.get("/books/{book_id}")
    async def get_book(book_id: str, user: dict = Depends(get_current_user)):
        b = await _book(book_id, user)
        return {**await _summary(b, user, full=True), "member": await _member(user)}

    @router.patch("/books/{book_id}")
    async def update_book(book_id: str, payload: BookPatch, user: dict = Depends(get_current_user)):
        await _require_member(user)
        b = await _book(book_id, user)
        upd = {k: v for k, v in payload.model_dump().items() if v is not None}
        for k in ("title", "subtitle", "genre", "book_type"):
            if k in upd:
                upd[k] = _clean(upd[k], 200 if k in ("title", "subtitle") else 80)
        if "title" in upd and not upd["title"]:
            raise HTTPException(400, "Your book needs a title")
        if "launch_date" in upd:
            upd["launch_date"] = _valid_day(upd["launch_date"]) if upd["launch_date"] else None
        if "project_id" in upd:
            if upd["project_id"]:
                try:
                    ok = await db.projects.find_one({"_id": ObjectId(upd["project_id"]), "user_id": user["id"]})
                except Exception:  # noqa: BLE001
                    ok = None
                if not ok:
                    raise HTTPException(404, "That SparkPrep book wasn't found")
            else:
                upd["project_id"] = None
        await _save(b, **upd)
        return await get_book(book_id, user)

    @router.delete("/books/{book_id}")
    async def delete_book(book_id: str, user: dict = Depends(get_current_user)):
        b = await _book(book_id, user)                 # deleting your own data never needs a membership
        async for f in db.studio_files.find({"book_id": str(b["_id"])}):
            if not f.get("shares_key_of"):
                storage.delete(f["storage_key"])
        await db.studio_files.delete_many({"book_id": str(b["_id"])})
        await db.studio_books.delete_one({"_id": b["_id"]})
        return {"ok": True}

    # ---------------------------------------------------------------- writing goals
    @router.post("/books/{book_id}/words")
    async def log_words(book_id: str, payload: WordsIn, user: dict = Depends(get_current_user)):
        await _require_member(user)
        b = await _book(book_id, user)
        day = _valid_day(payload.day) or datetime.now(timezone.utc).date().isoformat()
        current = b.get("words_written") or 0
        if payload.total is not None:
            added, total = payload.total - current, payload.total
        elif payload.added is not None:
            added, total = payload.added, max(0, current + payload.added)
        else:
            raise HTTPException(400, "Send the new total or the words you wrote")
        log = [e for e in (b.get("word_log") or [])]
        for e in log:
            if e["day"] == day:
                e["words"] = e["words"] + added
                break
        else:
            log.append({"day": day, "words": added})
        log = sorted(log, key=lambda e: e["day"])[-400:]
        await _save(b, words_written=total, word_log=log)
        return await get_book(book_id, user)

    # ---------------------------------------------------------------- checklist
    @router.put("/books/{book_id}/checklist")
    async def set_check(book_id: str, payload: CheckIn, user: dict = Depends(get_current_user)):
        await _require_member(user)
        if payload.key not in C.CHECKLIST_KEYS:
            raise HTTPException(400, "Unknown checklist item")
        b = await _book(book_id, user)
        checks = dict(b.get("checklist") or {})
        checks[payload.key] = payload.done
        await _save(b, checklist=checks)
        return await get_book(book_id, user)

    # ---------------------------------------------------------------- notes (and templates turned into notes)
    @router.post("/books/{book_id}/notes")
    async def add_note(book_id: str, payload: NoteIn, user: dict = Depends(get_current_user)):
        await _require_member(user)
        b = await _book(book_id, user)
        notes = list(b.get("notes") or [])
        if len(notes) >= C.MAX_NOTES:
            raise HTTPException(400, f"A book can hold up to {C.MAX_NOTES} notes.")
        now = _now()
        note = {"id": uuid.uuid4().hex[:12], "title": _clean(payload.title) or "Untitled note",
                "body": (payload.body or "")[:C.MAX_NOTE_CHARS], "kind": payload.kind if payload.kind in ("note", "template") else "note",
                "created_at": now, "updated_at": now}
        notes.append(note)
        await _save(b, notes=notes)
        return note

    @router.patch("/books/{book_id}/notes/{note_id}")
    async def edit_note(book_id: str, note_id: str, payload: NoteIn, user: dict = Depends(get_current_user)):
        await _require_member(user)
        b = await _book(book_id, user)
        notes = list(b.get("notes") or [])
        for n in notes:
            if n["id"] == note_id:
                if payload.title is not None:
                    n["title"] = _clean(payload.title) or "Untitled note"
                if payload.body is not None:
                    n["body"] = payload.body[:C.MAX_NOTE_CHARS]
                n["updated_at"] = _now()
                await _save(b, notes=notes)
                return n
        raise HTTPException(404, "Note not found")

    @router.delete("/books/{book_id}/notes/{note_id}")
    async def delete_note(book_id: str, note_id: str, user: dict = Depends(get_current_user)):
        b = await _book(book_id, user)
        await _save(b, notes=[n for n in (b.get("notes") or []) if n["id"] != note_id])
        return {"ok": True}

    # ---------------------------------------------------------------- launch planner
    @router.post("/books/{book_id}/launch/plan")
    async def make_plan(book_id: str, payload: PlanIn, user: dict = Depends(get_current_user)):
        """Sets the launch date and fills in the starting plan (keeps any task the author already has)."""
        await _require_member(user)
        b = await _book(book_id, user)
        launch = date.fromisoformat(_valid_day(payload.launch_date))
        tasks = list(b.get("launch_tasks") or [])
        have = {t["label"] for t in tasks}
        for offset, group, label in C.LAUNCH_TEMPLATE:
            if label not in have:
                tasks.append({"id": uuid.uuid4().hex[:12], "label": label, "group": group,
                              "due": (launch + timedelta(days=offset)).isoformat(), "done": False})
        await _save(b, launch_date=launch.isoformat(), launch_tasks=tasks[:C.MAX_LAUNCH_TASKS])
        return await get_book(book_id, user)

    @router.post("/books/{book_id}/launch/tasks")
    async def add_task(book_id: str, payload: TaskIn, user: dict = Depends(get_current_user)):
        await _require_member(user)
        b = await _book(book_id, user)
        if not (payload.label or "").strip():
            raise HTTPException(400, "Give the task a name")
        tasks = list(b.get("launch_tasks") or [])
        if len(tasks) >= C.MAX_LAUNCH_TASKS:
            raise HTTPException(400, "That's the most tasks one launch plan can hold.")
        tasks.append({"id": uuid.uuid4().hex[:12], "label": _clean(payload.label), "group": _clean(payload.group, 40) or "plan",
                      "due": _valid_day(payload.due), "done": bool(payload.done)})
        await _save(b, launch_tasks=tasks)
        return await get_book(book_id, user)

    @router.patch("/books/{book_id}/launch/tasks/{task_id}")
    async def edit_task(book_id: str, task_id: str, payload: TaskIn, user: dict = Depends(get_current_user)):
        await _require_member(user)
        b = await _book(book_id, user)
        tasks = list(b.get("launch_tasks") or [])
        for t in tasks:
            if t["id"] == task_id:
                if payload.label is not None and payload.label.strip():
                    t["label"] = _clean(payload.label)
                if payload.due is not None:
                    t["due"] = _valid_day(payload.due)
                if payload.done is not None:
                    t["done"] = payload.done
                await _save(b, launch_tasks=tasks)
                return await get_book(book_id, user)
        raise HTTPException(404, "Task not found")

    @router.delete("/books/{book_id}/launch/tasks/{task_id}")
    async def delete_task(book_id: str, task_id: str, user: dict = Depends(get_current_user)):
        b = await _book(book_id, user)
        await _save(b, launch_tasks=[t for t in (b.get("launch_tasks") or []) if t["id"] != task_id])
        return await get_book(book_id, user)

    # ---------------------------------------------------------------- author's business ledger
    @router.post("/books/{book_id}/ledger")
    async def add_ledger(book_id: str, payload: LedgerIn, user: dict = Depends(get_current_user)):
        await _require_member(user)
        if payload.kind not in ("expense", "marketing", "sale"):
            raise HTTPException(400, "Pick expense, marketing or sale")
        b = await _book(book_id, user)
        ledger = list(b.get("ledger") or [])
        if len(ledger) >= C.MAX_LEDGER:
            raise HTTPException(400, "That's the most entries one book can hold.")
        ledger.append({"id": uuid.uuid4().hex[:12], "kind": payload.kind, "label": _clean(payload.label),
                       "amount_cents": payload.amount_cents, "copies": payload.copies or 0,
                       "day": _valid_day(payload.day) or datetime.now(timezone.utc).date().isoformat()})
        await _save(b, ledger=ledger)
        return await get_book(book_id, user)

    @router.delete("/books/{book_id}/ledger/{entry_id}")
    async def delete_ledger(book_id: str, entry_id: str, user: dict = Depends(get_current_user)):
        b = await _book(book_id, user)
        await _save(b, ledger=[e for e in (b.get("ledger") or []) if e["id"] != entry_id])
        return await get_book(book_id, user)

    # ---------------------------------------------------------------- files with version history
    @router.post("/books/{book_id}/files")
    async def upload(book_id: str, folder: str = Form(...), file: UploadFile = File(...),
                     group_id: Optional[str] = Form(None), note: Optional[str] = Form(None),
                     user: dict = Depends(get_current_user)):
        await _require_member(user)
        b = await _book(book_id, user)
        if folder not in C.FOLDER_KEYS:
            raise HTTPException(400, "Unknown folder")
        name = _safe_name(file.filename)
        ext = Path(name).suffix.lower()
        if ext not in C.ALLOWED_EXTS:
            raise HTTPException(400, f"That kind of file ({ext or 'no extension'}) can't be stored here.")
        limit = C.MAX_FILE_MB * 1024 * 1024
        used = await _used_bytes(user["id"])
        quota = C.QUOTA_MB * 1024 * 1024
        fd, tmp_name = tempfile.mkstemp(prefix="studio_", dir=str(data_dir))
        os.close(fd)
        tmp = Path(tmp_name)
        size = 0
        try:
            with open(tmp, "wb") as fh:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"Files can be up to {C.MAX_FILE_MB} MB.")
                    if used + size > quota:
                        raise HTTPException(413, "Your Author Studio storage is full. Delete old versions to make room.")
                    fh.write(chunk)
            if not storage.has_room(size, C.MIN_FREE_DISK_MB):
                raise HTTPException(507, "Storage is busy right now. Please try again later.")
            # Same file again = next version. A group_id says it outright; otherwise the same name in the same folder.
            prev = None
            if group_id:
                prev = await db.studio_files.find_one({"group_id": group_id, "book_id": str(b["_id"])}, sort=[("version", -1)])
                if not prev:
                    raise HTTPException(404, "File not found")
                folder = prev["folder"]
            else:
                prev = await db.studio_files.find_one({"book_id": str(b["_id"]), "folder": folder, "name": name},
                                                      sort=[("version", -1)])
            gid = prev["group_id"] if prev else uuid.uuid4().hex[:16]
            version = (prev["version"] + 1) if prev else 1
            display = prev["name"] if prev and group_id else name
            key = f"{user['id']}/{b['_id']}/{gid}/v{version}_{uuid.uuid4().hex[:6]}{ext}"
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            storage.put_file(key, tmp, ctype)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        doc = {"user_id": user["id"], "book_id": str(b["_id"]), "group_id": gid, "folder": folder, "name": display,
               "original_filename": name, "version": version, "size": size, "content_type": ctype,
               "storage_key": key, "note": _clean(note, 300), "created_at": _now()}
        res = await db.studio_files.insert_one(doc)
        doc["_id"] = res.inserted_id
        await _save(b)
        return _file_out(doc)

    async def _owned_file(file_id: str, user: dict) -> dict:
        f = await db.studio_files.find_one({"_id": _oid(file_id), "user_id": user["id"]})
        if not f:
            raise HTTPException(404, "File not found")
        return f

    @router.get("/files/{file_id}/download")
    async def download(file_id: str, user: dict = Depends(get_current_user)):
        f = await _owned_file(file_id, user)            # your own files are always yours to download
        stem, ext = Path(f["name"]).stem, Path(f["name"]).suffix
        filename = f"{stem} (v{f['version']}){ext}"
        url = storage.download_url(f["storage_key"], filename)
        if url:
            return RedirectResponse(url, status_code=302)
        p = storage.local_path(f["storage_key"])
        if not p:
            raise HTTPException(404, "This file is missing. Please contact support.")
        return FileResponse(str(p), filename=filename, media_type=f.get("content_type") or "application/octet-stream")

    @router.get("/files/{file_id}/link")
    async def download_link(file_id: str, user: dict = Depends(get_current_user)):
        """For the website: a short-lived direct link when files live in Cloudflare R2, else null (then the
        site downloads through /download with the member's login)."""
        f = await _owned_file(file_id, user)
        stem, ext = Path(f["name"]).stem, Path(f["name"]).suffix
        return {"url": storage.download_url(f["storage_key"], f"{stem} (v{f['version']}){ext}"),
                "filename": f"{stem} (v{f['version']}){ext}"}

    @router.post("/files/{file_id}/restore")
    async def restore(file_id: str, user: dict = Depends(get_current_user)):
        """Go back to an earlier draft: it becomes the newest version. Nothing is deleted, so this can be undone."""
        await _require_member(user)
        f = await _owned_file(file_id, user)
        latest = await db.studio_files.find_one({"group_id": f["group_id"], "book_id": f["book_id"]}, sort=[("version", -1)])
        doc = {k: f[k] for k in ("user_id", "book_id", "group_id", "folder", "name", "original_filename", "size",
                                 "content_type", "storage_key")}
        doc.update(version=latest["version"] + 1, note=f"Restored from version {f['version']}",
                   restored_from=f["version"], shares_key_of=str(f.get("shares_key_of") or f["_id"]), created_at=_now())
        res = await db.studio_files.insert_one(doc)
        doc["_id"] = res.inserted_id
        return _file_out(doc)

    @router.delete("/files/{file_id}")
    async def delete_version(file_id: str, user: dict = Depends(get_current_user)):
        f = await _owned_file(file_id, user)
        others = [o async for o in db.studio_files.find({"storage_key": f["storage_key"], "user_id": user["id"]})
                  if o["_id"] != f["_id"]]
        if not others:
            storage.delete(f["storage_key"])
        elif not f.get("shares_key_of"):
            # This version owned the stored file and a restored copy still uses it: hand ownership on.
            heir = others[0]
            await db.studio_files.update_one({"_id": heir["_id"]}, {"$set": {"shares_key_of": None}})
            for o in others[1:]:
                await db.studio_files.update_one({"_id": o["_id"]}, {"$set": {"shares_key_of": str(heir["_id"])}})
        await db.studio_files.delete_one({"_id": f["_id"]})
        return {"ok": True}

    # ---------------------------------------------------------------- members-only Library
    @router.get("/library")
    async def library_list(user: dict = Depends(get_current_user)):
        await _require_member(user)
        return {"articles": [L.card(a) for a in L.published()], "categories": L.CATEGORIES}

    @router.get("/library/{slug}")
    async def library_article(slug: str, user: dict = Depends(get_current_user)):
        await _require_member(user)
        for a in L.published():
            if a["slug"] == slug:
                return {**L.card(a), "body": a["body"], "video_url": a.get("video_url")}
        raise HTTPException(404, "Article not found")

    return router
