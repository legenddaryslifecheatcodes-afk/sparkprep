from dotenv import load_dotenv
from pathlib import Path
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import io
import re
import copy
import uuid
import asyncio
import time
import logging
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Literal

import bcrypt
import jwt
import stripe
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
try:
    from motor.motor_asyncio import AsyncIOMotorClient
except Exception:  # pragma: no cover - fallback for local/dev environments
    AsyncIOMotorClient = None
from pydantic import BaseModel, EmailStr, Field
from bson import ObjectId

from print_specs import (
    TRIM_SIZES, PAPER_TYPES, BINDING_TYPES, PLATFORMS, COLOR_PROFILES, DEFAULT_COLOR_PROFILE,
    calculate_spine_width, calculate_spine_width_for_platform, calculate_full_cover_dimensions,
    resolve_binding_spec, PLATFORM_UNSUPPORTED_BINDINGS, paper_ppi, fmt_in, spine_source, even_page_count,
    BLACK_AND_WHITE_PAPERS,
)
from file_processor import (
    analyze_file, compute_effective_dpi, convert_to_cmyk,
    build_print_ready_pdf, build_interior_pdf_x1a, run_compliance_checks, assemble_cover_pieces,
    check_total_ink_coverage, check_final_pdf_ink_coverage, autofix_cover_safe_margin, autofix_interior_safety_margins,
    autofix_spine_text_margin,
    TAC_THRESHOLD_BY_PLATFORM, TAC_THRESHOLD_DEFAULT,
)
from audit_engine import deep_audit, audit_summary
from template_interpreter_adapter import interpret_publisher_template
from pdfx_validator import (
    run_pdf_structure_audit, check_interior_safety_margins, check_cover_safety_margins, check_rgb_color, ocr_status,
    SPINE_SAFETY_WIDE_IN, SPINE_SAFETY_NARROW_IN, SPINE_WIDTH_TIER_THRESHOLD_IN,
)
from ghostscript_engine import convert_to_pdfx1a, find_ghostscript
from report_export import generate_audit_brief_pdf, generate_preflight_report_pdf
from docx_reader import extract_manuscript_text, extract_embedded_images
from failure_log import log_failure
from barcode_engine import normalize_isbn, generate_barcode_png_bytes
from manuscript_composer import compose_manuscript_pdf, list_templates as list_manuscript_templates, TEMPLATES as MANUSCRIPT_TEMPLATES
from beta_engine import (
    generate_pass_code, new_pass_doc, new_feedback_doc, DEFAULT_CHECKLIST_FEATURES,
)
from series_engine import check_series_consistency
from ai_cover_engine import build_cover_prompt, generate_cover_image, AICoverError
from image_upscale_engine import upscale_to_size
import autofix_agents
import book_pass
from cover_template_engine import COVER_TEMPLATES, render_cover_template, list_cover_templates

class MemoryCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=-1):
        # `d.get(field) or ""` would also catch a real value of 0/False/0.0 as
        # "missing" (falsy), mixing str and numeric keys and crashing sorted().
        # Only a truly absent key should fall back to "".
        self._docs = sorted(self._docs, key=lambda d: d[field] if d.get(field) is not None else "",
                            reverse=direction != 1)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        self._iterator = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            # Shallow-copy on the way out -- real Mongo/motor always hands
            # back a freshly-deserialized dict per query, so callers that
            # mutate what they get back (e.g. `doc.pop("_id")`) only ever
            # touch their own copy. Without this, mutating a cursor result
            # here would corrupt the actual stored document, the same bug
            # fixed in find_one() below.
            return dict(next(self._iterator))
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class MemoryCollection:
    def __init__(self):
        self._docs = []

    async def create_index(self, *args, **kwargs):
        return None

    async def find_one(self, filter=None, sort=None):
        docs = self._docs
        if sort:  # real Mongo honours sort=[(field, direction)]; mirror it so local runs match
            for field, direction in reversed(sort):
                # See MemoryCursor.sort()'s note above -- same falsy-zero pitfall.
                docs = sorted(docs, key=lambda d, field=field: d[field] if d.get(field) is not None else "",
                             reverse=direction != 1)
        for doc in docs:
            if all(doc.get(key) == value for key, value in (filter or {}).items()):
                # Shallow-copy before returning -- see the note on
                # MemoryCursor.__anext__ above. Without this, code like
                # get_current_user()'s `user.pop("_id")` mutates the actual
                # stored document in place: the first successful auth check
                # for a user permanently strips its _id, and every request
                # after that silently fails with "User not found" -- this
                # was a real, reproduced bug in the in-memory dev fallback
                # (not present against real MongoDB, which never shares
                # object identity between a query result and its storage).
                return dict(doc)
        return None

    async def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", ObjectId())
        self._docs.append(doc)
        return type("InsertResult", (), {"inserted_id": doc["_id"]})()

    async def update_one(self, filter=None, update=None):
        matched = 0
        modified = 0
        for doc in self._docs:
            if all(doc.get(key) == value for key, value in (filter or {}).items()):
                matched += 1
                if update:
                    for key, value in update.get("$set", {}).items():
                        doc[key] = value
                    for key, value in update.get("$inc", {}).items():
                        doc[key] = doc.get(key, 0) + value
                modified += 1
        return type("UpdateResult", (), {"matched_count": matched, "modified_count": modified})()

    async def update_many(self, filter=None, update=None):
        return await self.update_one(filter, update)  # this shim's update_one already applies to every match

    async def delete_one(self, filter=None):
        before = len(self._docs)
        self._docs = [doc for doc in self._docs if not all(doc.get(key) == value for key, value in (filter or {}).items())]
        return type("DeleteResult", (), {"deleted_count": before - len(self._docs)})()

    async def delete_many(self, filter=None):
        return await self.delete_one(filter)  # this shim's delete_one already removes every match

    async def insert_many(self, docs):
        inserted_ids = []
        for doc in docs:
            doc = dict(doc)
            doc.setdefault("_id", ObjectId())
            inserted_ids.append(doc["_id"])
            self._docs.append(doc)
        return type("InsertManyResult", (), {"inserted_ids": inserted_ids})()

    async def count_documents(self, filter=None):
        return sum(1 for doc in self._docs if all(doc.get(key) == value for key, value in (filter or {}).items()))

    def find(self, filter=None):
        docs = [doc for doc in self._docs if all(doc.get(key) == value for key, value in (filter or {}).items())]
        return MemoryCursor(docs)


class MemoryDatabase:
    def __init__(self):
        self.users = MemoryCollection()
        self.projects = MemoryCollection()
        self.payment_transactions = MemoryCollection()
        self.beta_passes = MemoryCollection()
        self.beta_feedback = MemoryCollection()
        self.audits = MemoryCollection()
        self.exports = MemoryCollection()
        self._dynamic = {}

    def __getattr__(self, name):
        # Real Mongo databases hand back a collection for any attribute
        # name on first access -- this mirrors that so call sites (e.g.
        # db.interior_checks, db.teams) don't have to be pre-declared
        # above, matching real-Mongo behavior instead of AttributeError
        # crashing the in-memory dev fallback the first time a new
        # collection is used.
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._dynamic:
            self._dynamic[name] = MemoryCollection()
        return self._dynamic[name]


# ---- Setup ----
mongo_url = os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URL") or "mongodb://localhost:27017"
db_name = os.environ.get("DB_NAME") or "sparkprep"
logger = logging.getLogger("sparkprep")
if AsyncIOMotorClient is None:
    client = None
    db = MemoryDatabase()
    logger.warning("motor is not available; using an in-memory fallback database for local development")
else:
    try:
        client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=1000)
        # The local environment may not have MongoDB running, so prefer a safe in-memory fallback
        # instead of failing startup on first contact.
        db = client[db_name]
        if os.environ.get("USE_MEMORY_DB", "1") == "1":
            raise RuntimeError("Local development fallback enabled")
    except Exception as exc:
        client = None
        db = MemoryDatabase()
        logger.warning("Database client initialization failed: %s; using an in-memory fallback", exc)


# DATA_DIR points at a mounted persistent volume in production (Railway's
# container filesystem is wiped on every deploy/restart otherwise, which is
# why previously uploaded files kept disappearing). Falls back to the repo
# folder for local dev, where that's not an issue.
DATA_DIR = Path(os.environ.get("DATA_DIR") or ROOT_DIR)
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)

JWT_ALGORITHM = "HS256"
stripe.api_key = os.environ.get("STRIPE_API_KEY") or "sk_test_not_configured"

app = FastAPI(title="SparkPrep API")
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("sparkprep")

# ---- Interior check scope ----
# Basic Interior Check (page 1 only) is what every free/subscription audit
# and autofix gets by default. The paid Advanced Interior Check (up to
# ADVANCED_INTERIOR_MAX_PAGES, defined below near its checkout endpoint) is
# the only thing allowed to scan beyond page 1 -- these must never be mixed.
BASIC_CHECK_MAX_PAGES = 1

# ---- Hard ceiling on any single request's processing time ----
# Cloudflare sits in front of this backend (confirmed: Render's own edge is
# Cloudflare, independent of whatever the frontend uses) and cuts an idle
# connection at ~100s with an opaque 524 -- the client gets no useful error
# and, worse, has no way to know whether the backend actually finished the
# work or not, since nothing here tracks jobs asynchronously. Also found
# live: a single slow/heavy request (AI Upscale's old neural model) could
# run 280+ seconds and get OOM-killed by the host, taking the whole API
# down for every user, not just the one who triggered it. REQUEST_TIMEOUT_S
# is set well under Cloudflare's ceiling so a slow request fails fast with a
# real, friendly error instead of hanging into a 524.
# Launch-week guard rail, currently LIFTED on the owner's instruction ("correct,
# not fast"): with SPARKPREP_TIME_LIMITS unset/off, the ceiling below is only a
# 30-minute backstop against a wedged job. Set SPARKPREP_TIME_LIMITS=on to
# restore the original 45s behaviour everywhere (this and the verified
# auto-fix pipeline share the one switch).
REQUEST_TIMEOUT_S = 45.0 if autofix_agents.common.time_limits_on() else autofix_agents.common.NO_LIMIT_CEILING_S


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # A file upload's duration is the user's connection speed, not our
        # processing time -- a 100MB cover on a slow home uplink can
        # legitimately take over a minute just to arrive. The processing
        # that happens *after* the bytes land is bounded separately by
        # run_with_timeout inside each upload handler, so multipart uploads
        # are exempt from this outer clock.
        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            return await call_next(request)
        try:
            return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=503,
                content={"detail": "This is taking longer than expected and was stopped automatically. Try again, or try a smaller/simpler file."},
            )


async def run_with_timeout(fn, *args, **kwargs):
    """Runs a synchronous, potentially-slow function in a worker thread and
    enforces REQUEST_TIMEOUT_S on it. Plain `asyncio.wait_for` around a
    request handler doesn't actually interrupt synchronous CPU-bound code --
    it only reports the timeout once that code finally returns control to
    the event loop, which is too late to matter. Dispatching to a thread via
    asyncio.to_thread lets the event loop keep servicing the timeout timer
    while the real work runs, so the HTTP response comes back on time even
    though the orphaned thread (Python can't force-kill a thread) keeps
    running to completion in the background."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=REQUEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(
            504,
            f"This step is taking longer than {int(REQUEST_TIMEOUT_S)} seconds and was stopped automatically. "
            "Try again, or try a smaller/simpler file.",
        )


def _cover_file_meta(p: dict) -> Optional[dict]:
    """Resolve a project's cover file regardless of whether it was uploaded
    through the slot-based endpoint (slots.full_wrap/front_cover, the only
    path the current editor UI uses) or the legacy single-file field (older
    projects, or anything that predates slots)."""
    slots = p.get("slots") or {}
    if slots.get("full_wrap"):
        return slots["full_wrap"]
    if slots.get("front_cover"):
        return slots["front_cover"]
    if p.get("uploaded_file"):
        return {"stored_filename": p["uploaded_file"], **(p.get("file_metadata") or {})}
    return None


def _interior_file_meta(p: dict) -> Optional[dict]:
    """Resolve a project's interior file the same way -- several endpoints
    used to check only the legacy uploaded_file field, which a slot upload
    never sets, so they 404'd ("no file uploaded") for every interior/
    combined project uploaded through the real UI even after a real upload."""
    slots = p.get("slots") or {}
    if slots.get("interior"):
        return slots["interior"]
    if p.get("uploaded_file"):
        return {"stored_filename": p["uploaded_file"], **(p.get("file_metadata") or {})}
    return None


# Both build_interior_pdf_x1a() and build_print_ready_pdf() unconditionally
# (re)stamp GTS_PDFXVersion/OutputIntents/XMP at export time, regardless of
# whether the file already has them -- so a "fail" on these two checks
# doesn't mean SparkPrep's own export will produce a non-compliant file,
# only that the file doesn't have it *yet*, as uploaded.
EXPORT_TIME_FIXED_IDS = {"pdfx1a_not_declared", "pdfx1a_missing_output_intent"}


def _annotate_export_time_fixes(findings: list) -> list:
    """Shown as a plain 'fail' with no context, these two checks read as a
    real blocker -- exactly the confusion a real user hit, pointing at this
    exact finding right after being told their file was export-ready. This
    is applied only where the audit is shown specifically to someone about
    to export through SparkPrep (the Advanced Interior Check); a general
    PDF audit for a file someone intends to fix and use elsewhere is right
    to call this a real, unresolved fail, so the underlying check itself
    is left alone."""
    for f in findings:
        if f.get("id") in EXPORT_TIME_FIXED_IDS:
            f["why_it_fails"] = (
                (f.get("why_it_fails") or "").rstrip()
                + " Note: SparkPrep's own Export button fixes this automatically, every time, regardless of the "
                "file's current state -- this only matters if you use this file somewhere other than SparkPrep's export."
            )
    return findings

# Flat per-book export cap, independent of (layered on top of) the
# tier-based monthly account limits below -- prevents a single book from
# burning an entire month's export allowance on repeated re-exports of
# itself instead of the account actually shipping multiple books.
EXPORTS_PER_BOOK = 5

# ---- Subscription tiers ----
TIERS = {
    "free": {
        "name": "Free",
        "books_per_month": 0,
        "monthly_exports": 0,
        "max_file_mb": 25,
        "price_cents": 0,
        "team_seats": 1,
        "batch_enabled": False,
        "white_label": False,
        "features": [
            "Preview & compliance report",
            "No exports — upgrade to export",
            "Uploads up to 25 MB",
            "KDP + IngramSpark templates",
        ],
    },
    "author": {
        "name": "Author",
        "books_per_month": 1,
        "monthly_exports": 15,
        "max_file_mb": 100,
        "price_cents": 1999,
        "price_cents_annual": 19999,
        "team_seats": 1,
        "batch_enabled": False,
        "white_label": False,
        "features": [
            "1 full book / month (cover + spine + back + interior)",
            "15 print-ready exports / month",
            "Uploads up to 100 MB",
            "All distributor templates",
            "AI Blurb Writer",
        ],
    },
    "creator_pro": {
        "name": "Creator Pro",
        "books_per_month": 3,
        "monthly_exports": 45,
        "max_file_mb": 250,
        "price_cents": 3999,
        "price_cents_annual": 39999,
        "team_seats": 1,
        "batch_enabled": False,
        "white_label": False,
        "features": [
            "3 full books / month",
            "45 exports / month",
            "Uploads up to 250 MB",
            "Priority AI blurb + 3D mockup",
            "All distributor templates",
            "Email support",
        ],
    },
    "publisher": {
        "name": "Publisher",
        "books_per_month": 7,
        "monthly_exports": 100,
        "max_file_mb": 500,
        "price_cents": 6999,
        "price_cents_annual": 69999,
        "team_seats": 3,
        "batch_enabled": True,
        "white_label": False,
        "features": [
            "7 full books / month",
            "100 exports / month",
            "Team seats (up to 3)",
            "Uploads up to 500 MB",
            "Bulk audit + batch export",
            "Priority support",
        ],
    },
    "studio": {
        "name": "Studio",
        "books_per_month": 30,
        "monthly_exports": 300,
        "max_file_mb": 1024,
        "price_cents": 19999,
        "price_cents_annual": 199999,
        "team_seats": 10,
        "batch_enabled": True,
        "white_label": True,
        "features": [
            "30 full books / month",
            "300 exports / month",
            "Team seats (up to 10)",
            "Uploads up to 1 GB",
            "Advanced color profiles + white-label",
            "Dedicated account manager",
        ],
    },
}

# 99-Day Audit Season launch (Sept 23 → Dec 31) -- the same dates the 99-Day Special's audit prices use.
AUDIT_SEASON_START = book_pass.SPECIAL_START
AUDIT_SEASON_END = book_pass.SPECIAL_END


def audit_season_status():
    now = datetime.now(timezone.utc)
    if now < AUDIT_SEASON_START:
        days_until = (AUDIT_SEASON_START - now).days
        return {"phase": "pre_launch", "start": AUDIT_SEASON_START.isoformat(), "end": AUDIT_SEASON_END.isoformat(), "days_until": days_until, "days_remaining": 99}
    if now <= AUDIT_SEASON_END:
        days_remaining = (AUDIT_SEASON_END - now).days
        return {"phase": "active", "start": AUDIT_SEASON_START.isoformat(), "end": AUDIT_SEASON_END.isoformat(), "days_until": 0, "days_remaining": days_remaining}
    return {"phase": "closed", "start": AUDIT_SEASON_START.isoformat(), "end": AUDIT_SEASON_END.isoformat(), "days_until": 0, "days_remaining": 0}

# ---- Auth helpers ----
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def get_jwt_secret() -> str:
    return os.environ["JWT_SECRET"]

def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id, "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
        "type": "access",
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        user["id"] = str(user.pop("_id"))
        user.pop("password_hash", None)
        if book_pass.book_pass_on():
            # Book model: a customer who holds a book (unused credit or an open window) gets the plan-locked
            # features unlocked; everyone else is "free". Admins and beta testers keep what they have.
            exempt = bool(user.get("beta_active")) or is_admin_user(user)
            user["tier"] = await book_pass.entitlements.effective_tier(db, user, exempt=exempt)
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def set_auth_cookie(response: Response, token: str):
    response.set_cookie(
        key="access_token", value=token, httponly=True, secure=True,
        samesite="none", max_age=604800, path="/",
    )


ADMIN_EMAIL = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()


def is_admin_user(user: dict) -> bool:
    return bool(ADMIN_EMAIL) and user.get("email", "").lower() == ADMIN_EMAIL


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def enrich_user(user: dict) -> dict:
    """Add computed flags for client consumption (idempotent)."""
    user["is_admin"] = is_admin_user(user)
    user["beta_active"] = bool(user.get("beta_active", False))
    user["beta_pass_code"] = user.get("beta_pass_code")
    user["is_team_member"] = bool(user.get("team_owner_id"))
    return user


# ---- Team seats (Publisher: 3, Studio: 10 -- see TIERS[*]["team_seats"]) ----
TEAM_INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no O/0/I/1/L -- avoids miscopied invite codes


def generate_team_invite_code() -> str:
    return "TEAM-" + "".join(secrets.choice(TEAM_INVITE_ALPHABET) for _ in range(8))


async def get_billing_user(user: dict) -> dict:
    """Resolves the account whose subscription tier, usage counters and
    white-label settings govern this request. A team member (a user with
    team_owner_id set, via /team/join) shares their org owner's plan and
    usage pool instead of having their own free-tier limits -- one paid
    seat-holding subscription covers the whole team, the same model as
    most seat-based SaaS billing. Solo accounts are their own billing user.
    """
    owner_id = user.get("team_owner_id")
    if not owner_id:
        return user
    owner = await db.users.find_one({"_id": ObjectId(owner_id)})
    if not owner:
        return user
    owner = dict(owner)
    owner["id"] = str(owner.pop("_id"))
    if book_pass.book_pass_on():
        owner["tier"] = await book_pass.entitlements.effective_tier(
            db, owner, exempt=bool(owner.get("beta_active")) or is_admin_user(owner))
    return owner


# ---- Models ----
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: Optional[str] = None

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class ProjectCreate(BaseModel):
    name: str
    platform: str = "kdp"  # kdp, ingramspark, barnes_noble, lulu
    trim_size: str = "6x9"
    paper_type: str = "white_50lb"
    binding: str = "paperback"
    page_count: int = 200
    project_type: str = "cover"  # cover, interior, combined
    series_name: Optional[str] = None

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    platform: Optional[str] = None
    trim_size: Optional[str] = None
    paper_type: Optional[str] = None
    binding: Optional[str] = None
    page_count: Optional[int] = None
    project_type: Optional[str] = None
    series_name: Optional[str] = None
    # Hardcover (case laminate or dust jacket) spine width isn't a public
    # formula -- IngramSpark computes it from their own internal stepped
    # table keyed to exact page count + paper weight, not a page/PPI ratio,
    # so SparkPrep's formula-based estimate can be meaningfully wrong for
    # any hardcover binding. This lets the user paste the exact number from
    # IngramSpark's own Spine and Weight Calculator and have SparkPrep build
    # the real cover around that instead of guessing. None/omitted falls
    # back to the formula estimate (correct for paperback, an estimate only
    # for hardcover). Set to 0 or null explicitly to clear it.
    spine_width_override: Optional[float] = None
    # When the uploaded/designed cover art already has its own barcode built
    # in (common for a professionally designed jacket), skip SparkPrep's
    # auto-overlay at export instead of stamping a second barcode on top.
    cover_has_barcode: Optional[bool] = None
    # "full": one combined file (a plain wrap for paperback/hardcover_case, or a
    # 5-panel wrap-with-flaps for hardcover_jacket -- whichever the Binding field
    # already says). "separate": front/spine/back uploaded as three individual
    # files. Independent of Binding -- this only decides which upload screen the
    # customer sees, never the cover's actual shape. None/unset = not chosen yet.
    cover_upload_mode: Optional[Literal["full", "separate"]] = None
    # Set once by the frontend the first time the whole project (every part it
    # needs -- cover, interior, or both) has passed every check. Marks that the
    # "your book is ready" celebration has already played, so it's a one-time
    # milestone moment, not something that replays on every later visit.
    book_ready_celebrated_at: Optional[str] = None

class CheckoutIn(BaseModel):
    tier: str  # pro | studio
    origin_url: str


class BlurbIn(BaseModel):
    title: str
    genre: Optional[str] = None
    page_count: Optional[int] = None
    themes: Optional[str] = None
    audience: Optional[str] = None


class TemplatePDFAnalyze(BaseModel):
    file_id: str  # returned from a prior upload


class SlotUploadMeta(BaseModel):
    slot: str  # front_cover | back_cover | spine | interior | full_wrap


class ManualAdjustments(BaseModel):
    spine_offset: Optional[float] = None       # inches, +/- to shift spine text
    bleed_extra: Optional[float] = None        # extra bleed to add (inches)
    trim_offset_x: Optional[float] = None      # trim alignment shift x (inches)
    trim_offset_y: Optional[float] = None      # trim alignment shift y (inches)
    image_scale: Optional[float] = None        # 0.5 - 2.0
    target_dpi: Optional[int] = None           # 200 - 600
    color_profile: Optional[str] = None        # "US Web Coated SWOP v2" | "GRACoL" | "FOGRA39"


# ---- Auth Routes ----
@api_router.post("/auth/register")
async def register(payload: RegisterIn, response: Response):
    email = payload.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    now = datetime.now(timezone.utc)
    doc = {
        "email": email,
        "password_hash": hash_password(payload.password),
        "name": payload.name or email.split("@")[0],
        "tier": "free",
        "stripe_customer_id": None,
        "subscription_status": None,
        "exports_this_month": 0,
        "books_this_month": 0,
        "billing_period_start": now.isoformat(),
        "created_at": now.isoformat(),
    }
    result = await db.users.insert_one(doc)
    uid = str(result.inserted_id)
    token = create_access_token(uid, email)
    set_auth_cookie(response, token)
    doc.pop("password_hash")
    doc["id"] = uid
    doc.pop("_id", None)
    return {"user": enrich_user(doc), "token": token}


# A cool-down, never a lockout (owner's rule): after LOGIN_MAX_FAILS wrong passwords for one account within
# LOGIN_WINDOW_S, that account's login waits until the oldest of those tries is LOGIN_WINDOW_S old. That makes
# guessing by a program useless while the real owner is never locked out and never needs a phone or codes.
LOGIN_MAX_FAILS = 10
LOGIN_WINDOW_S = 15 * 60


async def _recent_login_fails(email: str) -> list:
    doc = await db.login_guard.find_one({"email": email}) or {}
    cutoff = datetime.now(timezone.utc).timestamp() - LOGIN_WINDOW_S
    return [t for t in (doc.get("fails") or []) if t > cutoff]


@api_router.post("/auth/login")
async def login(payload: LoginIn, response: Response):
    email = payload.email.lower()
    fails = await _recent_login_fails(email)
    if len(fails) >= LOGIN_MAX_FAILS:
        wait_min = max(1, round((min(fails) + LOGIN_WINDOW_S - datetime.now(timezone.utc).timestamp()) / 60))
        raise HTTPException(status_code=429, detail=(
            f"Too many wrong passwords for this account. To keep it safe, please wait about {wait_min} minute"
            f"{'' if wait_min == 1 else 's'} and try again. Nothing is locked -- your password works as soon as the wait is over."))
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user["password_hash"]):
        fails.append(datetime.now(timezone.utc).timestamp())
        if await db.login_guard.find_one({"email": email}):
            await db.login_guard.update_one({"email": email}, {"$set": {"fails": fails}})
        else:
            await db.login_guard.insert_one({"email": email, "fails": fails})
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if fails:
        await db.login_guard.update_one({"email": email}, {"$set": {"fails": []}})
    uid = str(user["_id"])
    token = create_access_token(uid, email)
    set_auth_cookie(response, token)
    user["id"] = uid
    user.pop("_id", None)
    user.pop("password_hash", None)
    return {"user": enrich_user(user), "token": token}


@api_router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    return {"ok": True}


@api_router.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {"user": enrich_user(user)}


# ---- Team seats ----
class TeamJoinIn(BaseModel):
    code: str


class TeamBrandingIn(BaseModel):
    brand_name: Optional[str] = None  # None/"" clears white-label branding


@api_router.get("/team/status")
async def team_status(user: dict = Depends(get_current_user)):
    tier = user.get("tier", "free")
    seats = TIERS.get(tier, TIERS["free"])["team_seats"]
    if user.get("team_owner_id"):
        owner = await db.users.find_one({"_id": ObjectId(user["team_owner_id"])})
        return {
            "role": "member",
            "owner_email": owner.get("email") if owner else None,
        }
    member_count = await db.users.count_documents({"team_owner_id": user["id"]})
    pending = [inv async for inv in db.team_invites.find({"owner_id": user["id"], "status": "pending"})]
    return {
        "role": "owner",
        "seats_total": seats,
        "seats_used": 1 + member_count,  # the owner occupies one seat
        "pending_invites": [{"code": i["code"], "created_at": i["created_at"]} for i in pending],
        "white_label_brand_name": user.get("white_label_brand_name") if TIERS.get(tier, {}).get("white_label") else None,
    }


@api_router.post("/team/invite")
async def create_team_invite(user: dict = Depends(get_current_user)):
    if user.get("team_owner_id"):
        raise HTTPException(400, "You're a member of another team — leave it first (POST /team/leave) before inviting others.")
    tier = user.get("tier", "free")
    seats = TIERS.get(tier, TIERS["free"])["team_seats"]
    if seats <= 1:
        raise HTTPException(402, _soon_msg(f"Team seats aren't included on the {TIERS.get(tier, {}).get('name', tier)} plan. Upgrade to Publisher (3 seats) or Studio (10 seats).", "Team seats"))
    member_count = await db.users.count_documents({"team_owner_id": user["id"]})
    pending_count = await db.team_invites.count_documents({"owner_id": user["id"], "status": "pending"})
    if 1 + member_count + pending_count >= seats:
        raise HTTPException(402, f"All {seats} seats on your plan are in use or already invited.")
    code = generate_team_invite_code()
    await db.team_invites.insert_one({
        "code": code,
        "owner_id": user["id"],
        "owner_email": user["email"],
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"invite_code": code}


@api_router.post("/team/join")
async def join_team(payload: TeamJoinIn, user: dict = Depends(get_current_user)):
    if user.get("team_owner_id"):
        raise HTTPException(400, "You're already a member of a team. Leave it first (POST /team/leave).")
    if await db.users.count_documents({"team_owner_id": user["id"]}) > 0:
        raise HTTPException(400, "You own a team with members — you can't also join someone else's team.")
    code = payload.code.strip().upper()
    invite = await db.team_invites.find_one({"code": code, "status": "pending"})
    if not invite:
        raise HTTPException(404, "Invalid or already-used invite code.")
    if invite["owner_id"] == user["id"]:
        raise HTTPException(400, "You can't join your own team.")
    owner = await db.users.find_one({"_id": ObjectId(invite["owner_id"])})
    if not owner:
        raise HTTPException(404, "The team owner's account no longer exists.")
    seats = TIERS.get(owner.get("tier", "free"), TIERS["free"])["team_seats"]
    member_count = await db.users.count_documents({"team_owner_id": invite["owner_id"]})
    if 1 + member_count >= seats:
        raise HTTPException(402, "That team's seats are already full.")
    await db.users.update_one({"_id": ObjectId(user["id"])}, {"$set": {"team_owner_id": invite["owner_id"]}})
    await db.team_invites.update_one({"_id": invite["_id"]}, {"$set": {"status": "consumed", "consumed_by": user["id"], "consumed_at": datetime.now(timezone.utc).isoformat()}})
    return {"ok": True, "owner_email": owner.get("email")}


@api_router.post("/team/leave")
async def leave_team(user: dict = Depends(get_current_user)):
    if not user.get("team_owner_id"):
        raise HTTPException(400, "You're not a member of any team.")
    await db.users.update_one({"_id": ObjectId(user["id"])}, {"$set": {"team_owner_id": None}})
    return {"ok": True}


@api_router.get("/team/members")
async def list_team_members(user: dict = Depends(get_current_user)):
    if user.get("team_owner_id"):
        raise HTTPException(403, "Only the team owner can view the member list.")
    members = [m async for m in db.users.find({"team_owner_id": user["id"]})]
    return {"members": [{"id": str(m["_id"]), "email": m["email"], "name": m.get("name")} for m in members]}


@api_router.delete("/team/members/{member_id}")
async def remove_team_member(member_id: str, user: dict = Depends(get_current_user)):
    if user.get("team_owner_id"):
        raise HTTPException(403, "Only the team owner can remove members.")
    result = await db.users.update_one({"_id": ObjectId(member_id), "team_owner_id": user["id"]}, {"$set": {"team_owner_id": None}})
    if result.matched_count == 0:
        raise HTTPException(404, "That member isn't on your team.")
    return {"ok": True}


@api_router.patch("/team/branding")
async def set_team_branding(payload: TeamBrandingIn, user: dict = Depends(get_current_user)):
    """White-label: Studio tier only. The brand name replaces 'SparkPrep' in
    exported PDF metadata and audit report PDFs for this account and its
    team members' work."""
    if user.get("team_owner_id"):
        raise HTTPException(403, "Only the team/billing owner can change branding.")
    tier = user.get("tier", "free")
    if not TIERS.get(tier, TIERS["free"]).get("white_label"):
        raise HTTPException(402, _soon_msg("White-label branding is a Studio plan feature.", "White-label branding"))
    name = (payload.brand_name or "").strip()[:60]
    await db.users.update_one({"_id": ObjectId(user["id"])}, {"$set": {"white_label_brand_name": name or None}})
    return {"ok": True, "white_label_brand_name": name or None}


# ---- Specs ----
@api_router.get("/specs")
async def get_specs():
    out = {
        "platforms": PLATFORMS,
        "trim_sizes": TRIM_SIZES,
        "paper_types": PAPER_TYPES,
        "binding_types": BINDING_TYPES,
    }
    # The SparkPrep Assistant reads prices from here -- under the book model it must see the real prices,
    # never the retired monthly plans.
    if book_pass.book_pass_on():
        out["pricing"] = book_pass.public_pricing()
    else:
        out["tiers"] = TIERS
    return out


def _paid_msg(legacy: str, feature: str) -> str:
    """Upgrade message for a paid feature, in whichever pricing model is live."""
    return f"{feature} is included with a book — {book_pass.book_offer_text()}." if book_pass.book_pass_on() else legacy


def _soon_msg(legacy: str, feature: str) -> str:
    """Team / white-label / bulk tools aren't sold under the book model yet."""
    return f"{feature} — coming soon." if book_pass.book_pass_on() else legacy


@api_router.post("/specs/spine")
async def spine_calc(payload: dict):
    page_count = int(payload.get("page_count", 0))
    paper = payload.get("paper_type", "white_50lb")
    paper_info = PAPER_TYPES.get(paper, PAPER_TYPES["white_50lb"])
    trim_key = payload.get("trim_size", "6x9")
    trim = TRIM_SIZES.get(trim_key, TRIM_SIZES["6x9"])
    binding = payload.get("binding", "paperback")
    plat = payload.get("platform", "kdp")
    binding_unsupported = binding in PLATFORM_UNSUPPORTED_BINDINGS.get(plat, set())
    # Cover bleed is a property of the (platform, binding) pair -- case
    # laminate/jacket bleed varies by distributor, not just by binding.
    bleed = resolve_binding_spec(binding, plat)["bleed"]
    spine_w, spine_is_estimate = calculate_spine_width_for_platform(page_count, paper_ppi(paper_info, plat), plat, binding)
    spine_is_estimate = not spine_is_estimate
    if payload.get("spine_width_override"):
        spine_w = float(payload["spine_width_override"])
        spine_is_estimate = False
    full = calculate_full_cover_dimensions(trim["w"], trim["h"], spine_w, bleed, binding, plat)
    return {
        "spine_width": spine_w,
        "spine_display": fmt_in(spine_w),
        "spine_is_estimate": spine_is_estimate,
        # where the number comes from, so the Editor can say so honestly (see print_specs.spine_source)
        "spine_source": "your_number" if payload.get("spine_width_override") else spine_source(plat, binding, paper_ppi(paper_info, plat)),
        "page_count_used": even_page_count(page_count, plat),
        "binding_unsupported_on_platform": binding_unsupported,
        "full_cover": full,
        "trim": trim,
        "paper_ppi": paper_ppi(paper_info, plat),
        "bleed": bleed,
        "spine_text_allowed": page_count >= PLATFORMS.get(plat, PLATFORMS["kdp"])["spine_text_min_pages"],
    }


# ---- Projects ----
def project_to_dict(p: dict) -> dict:
    p = dict(p)
    p["id"] = str(p.pop("_id"))
    p.pop("user_id_obj", None)
    return p


# ---- The owner's funnel (book-pass model): free demo -> $1.99 results -> the product ----
# 1. Free: the REAL scan runs, but only whether issues were found (and how many) leaves the server.
# 2. $1.99 audit: the full results for this project. A problem detector only -- it never repairs.
# 3. The product (a book on this project): Repair Bay, every fix, final review, export.
# The lock is enforced here, on the server; hiding results in the browser alone would leave them
# readable in the network responses.

async def _owns_product(p: dict, user: dict) -> bool:
    """May use Repair Bay / fixes / final review / export on this project (same rule as export)."""
    if not book_pass.book_pass_on() or is_admin_user(user):
        return True
    billing_user = await get_billing_user(user)
    if user.get("beta_active") or billing_user.get("beta_active"):
        return True
    if p.get("promo_access") in ("full_access", "interior_only_access"):
        return True
    return bool(await book_pass.entitlements.project_window(db, billing_user["id"], str(p["_id"])))


async def _require_product(p: dict, user: dict, what: str = "Repair Bay"):
    if await _owns_product(p, user):
        return
    billing_user = await get_billing_user(user)
    has_credit = await book_pass.entitlements.has_paid_access(db, billing_user["id"])
    raise HTTPException(402, {"code": "book_required", "has_credit": has_credit, "msg": (
        f"Start this book to use {what} (you have a book ready to use)." if has_credit
        else f"{what} is part of the SparkPrep book — {book_pass.book_offer_text()}.")})


async def _results_unlocked(p: dict, user: dict) -> bool:
    if await _owns_product(p, user):
        return True
    billing_user = await get_billing_user(user)
    if await db.book_credits.find_one({"user_id": billing_user["id"], "project_id": str(p["_id"])}):
        return True                                          # bought the book for it; its window has since ended
    aid = p.get("results_audit_id")
    return bool(aid and (await db.audits.find_one({"audit_id": aid}) or {}).get("paid"))


def _check_kind(check_id: str) -> str:
    """What kind of check ran, without revealing its result (labels like "Color Space (RGB)" would)."""
    c = (check_id or "").lower()
    for keys, name in ((("size", "spine_width"), "Page & cover size"), (("dpi",), "Resolution"), (("color",), "Color space"),
                       (("transparen",), "Transparency"), (("ink", "tac"), "Ink coverage"), (("bleed",), "Bleed"),
                       (("pdfx",), "PDF/X-1a print standard"), (("margin", "safety", "spine", "center"), "Safe margins")):
        if any(k in c for k in keys):
            return name
    return "Print readiness"


# File facts that ARE a check's answer (color_mode "RGB" is the color-space result, dpi the resolution one).
_RESULT_REVEALING_FILE_FACTS = ("color_mode", "has_transparency", "dpi_x", "dpi_y")


def _strip_file_facts(meta: Optional[dict]) -> Optional[dict]:
    return {k: v for k, v in meta.items() if k not in _RESULT_REVEALING_FILE_FACTS} if meta else meta


def _lock_results(pd: dict) -> dict:
    """The free demo view of a project: every result detail removed, only the honest count kept.
    Works on its own deep copy -- blanking results on a shared object would wipe the real stored scan."""
    pd = copy.deepcopy(pd)
    slots = pd.get("slots") or {}
    checks = [c for s in slots.values() for c in (s.get("compliance") or [])] or list(pd.get("compliance") or [])
    kinds = []
    for c in checks:
        k = _check_kind(c.get("id"))
        if k not in kinds:
            kinds.append(k)
    if any(s.get("is_pdf") and s.get("audit_issue_count") is not None for s in slots.values()):
        kinds.append("Fonts & PDF structure")
    # The count is the audit's own (see _slot_audit_issue_count), so it always matches what the paid audit shows;
    # files uploaded before that existed fall back to the Editor's checks.
    issues = (sum(s["audit_issue_count"] if s.get("audit_issue_count") is not None
                  else sum(1 for c in (s.get("compliance") or []) if c.get("status") != "pass") for s in slots.values())
              if slots else sum(1 for c in checks if c.get("status") != "pass"))
    for key, s in list(slots.items()):
        slots[key] = {**_strip_file_facts(s), "compliance": []}
    pd["compliance"] = []
    pd["file_metadata"] = _strip_file_facts(pd.get("file_metadata"))
    for key in ("repair_log", "final_review"):
        pd.pop(key, None)
    pd["results_locked"] = True
    pd["results_summary"] = {"scanned": bool(checks), "issues": issues, "checks_run": kinds}
    return pd


_EXPORT_FIXES_INK_NOTE = (" SparkPrep fixes this automatically when it builds your print files -- nothing for you to do. "
                          "The export then measures every page to prove it.")


def _export_fixes_ink(p: dict, slot: str, meta: dict) -> bool:
    """Whether export itself brings this file's ink under the limit (owner, 2026-10-05: "why can't we fix this
    for the users"): a cover PDF is redrawn and an RGB cover converted with the ink limit applied; a black & white
    book's PDF interior is exported as true grayscale (never over 100%); an RGB image interior is converted
    with the limit. A file that's already CMYK artwork keeps its exact ink at export, so it isn't covered here."""
    mode = (meta.get("color_mode") or "").upper()
    if slot == "interior":
        if mode == "PDF":
            return p.get("paper_type", "white_50lb") in BLACK_AND_WHITE_PAPERS
        return mode not in ("", "CMYK")
    return mode == "PDF" or mode not in ("", "CMYK")


def _mark_export_fixes(p: dict, slot: str, meta: dict, compliance: list) -> list:
    if not compliance or not _export_fixes_ink(p, slot, meta or {}):
        return compliance
    out = []
    for c in compliance:
        if c.get("id") == "total_ink_coverage" and c.get("status") != "pass" and not c.get("fixed_on_export"):
            c = {**c, "fixed_on_export": True, "message": (c.get("message") or "") + _EXPORT_FIXES_INK_NOTE}
        out.append(c)
    return out


def _mark_project_export_fixes(p: dict, pd: dict) -> dict:
    slots = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (pd.get("slots") or {}).items()}
    pd["slots"] = slots                           # copies: never touch the stored document
    for slot, meta in slots.items():
        if isinstance(meta, dict) and meta.get("compliance"):
            meta["compliance"] = _mark_export_fixes(p, slot, meta, meta["compliance"])
    if pd.get("compliance"):                      # the top-level copy of the cover's (full_wrap / legacy) checks
        cover_meta = slots.get("full_wrap") or pd.get("file_metadata") or {}
        pd["compliance"] = _mark_export_fixes(p, "full_wrap", cover_meta, pd["compliance"])
    return pd


async def _present_project(p: dict, user: dict) -> dict:
    pd = project_to_dict(p)
    if await _results_unlocked(p, user):
        pd = _mark_project_export_fixes(p, pd)
        pd["results_locked"] = False
        pd["owns_product"] = await _owns_product(p, user)
        aid = p.get("results_audit_id")
        a = await db.audits.find_one({"audit_id": aid}) if aid and not pd["owns_product"] else None
        if a and a.get("paid"):
            pd["results_audit_level"] = a.get("level") or "standard"
            pd["results_can_upgrade"] = _audit_can_upgrade(a)
        return pd
    pd["owns_product"] = False
    return _lock_results(pd)


async def _present_upload(p: dict, user: dict, body: dict) -> dict:
    """A {file_metadata, compliance, ...} response, locked for a free demo project."""
    if await _results_unlocked(p, user):
        return body
    checks = body.get("compliance") or []
    audit_count = (body.get("file_metadata") or {}).get("audit_issue_count")
    return {**body, "compliance": [], "file_metadata": _strip_file_facts(body.get("file_metadata")), "results_locked": True,
            "results_summary": {"scanned": True,
                                "issues": audit_count if audit_count is not None else sum(1 for c in checks if c.get("status") != "pass"),
                                "checks_run": list(dict.fromkeys(_check_kind(c.get("id")) for c in checks))}}


@api_router.get("/projects")
async def list_projects(user: dict = Depends(get_current_user)):
    cursor = db.projects.find({"user_id": user["id"]}).sort("updated_at", -1)
    items = [await _present_project(p, user) async for p in cursor]
    return {"projects": items}


@api_router.post("/projects")
async def create_project(payload: ProjectCreate, user: dict = Depends(get_current_user)):
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "user_id": user["id"],
        "name": payload.name,
        "platform": payload.platform,
        "trim_size": payload.trim_size,
        "paper_type": payload.paper_type,
        "binding": payload.binding,
        "page_count": payload.page_count,
        "project_type": payload.project_type,
        "series_name": (payload.series_name or "").strip() or None,
        "uploaded_file": None,
        "file_metadata": None,
        "compliance": [],
        "exports_used": 0,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.projects.insert_one(doc)
    doc["_id"] = result.inserted_id
    return await _present_project(doc, user)


@api_router.get("/projects/{project_id}")
async def get_project(project_id: str, user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    return await _present_project(p, user)


@api_router.patch("/projects/{project_id}")
async def update_project(project_id: str, payload: ProjectUpdate, user: dict = Depends(get_current_user)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    before = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not before:
        raise HTTPException(404, "Project not found")
    await db.projects.update_one({"_id": ObjectId(project_id), "user_id": user["id"]}, {"$set": updates})
    p = await db.projects.find_one({"_id": ObjectId(project_id)})
    if any(k in updates and updates[k] != before.get(k) for k in _GEOMETRY_FIELDS):
        # The covers were checked against the old size -- re-check them so the Editor never shows a stale verdict.
        rescan = await run_with_timeout(_rescan_cover_slots, p)
        if rescan:
            await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": rescan})
            p = await db.projects.find_one({"_id": ObjectId(project_id)})
    return await _present_project(p, user)


@api_router.delete("/projects/{project_id}")
async def delete_project(project_id: str, user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    await run_with_timeout(_delete_project_files, project_id, p)
    await db.projects.delete_one({"_id": ObjectId(project_id)})
    return {"ok": True}


# ---- Series consistency checker ----
@api_router.get("/series")
async def list_series(user: dict = Depends(get_current_user)):
    """Every distinct series_name this user has used, with book counts --
    lets the UI offer a picker instead of the user retyping a series name."""
    cursor = db.projects.find({"user_id": user["id"]})
    counts: dict = {}
    async for p in cursor:
        name = (p.get("series_name") or "").strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    return {"series": [{"name": k, "book_count": v} for k, v in sorted(counts.items())]}


@api_router.get("/series/{series_name}/consistency")
async def series_consistency(series_name: str, user: dict = Depends(get_current_user)):
    cursor = db.projects.find({"user_id": user["id"], "series_name": series_name})
    projects = [project_to_dict(p) async for p in cursor]
    if not projects:
        raise HTTPException(404, f"No books found in series '{series_name}'")
    findings = check_series_consistency(projects, {
        "trim_sizes": TRIM_SIZES, "platforms": PLATFORMS, "paper_types": PAPER_TYPES,
    })
    return {
        "series_name": series_name,
        "book_count": len(projects),
        "books": [{"id": p["id"], "name": p.get("name")} for p in projects],
        "findings": findings,
    }


# ---- File Upload ----
@api_router.post("/projects/{project_id}/upload")
async def upload_file(project_id: str, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")

    billing_user = await get_billing_user(user)
    tier = billing_user.get("tier", "free")
    max_mb = TIERS.get(tier, TIERS["free"])["max_file_mb"]
    if user.get("beta_active") or billing_user.get("beta_active"):
        max_mb = 1024  # beta = full 1GB uploads

    ext = Path(file.filename).suffix.lower()
    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(allowed))}")

    file_id = f"{project_id}_{uuid.uuid4().hex[:8]}{ext}"
    file_path = UPLOAD_DIR / file_id
    size = 0
    with open(file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_mb * 1024 * 1024:
                f.close()
                os.remove(file_path)
                raise HTTPException(413, f"File exceeds {max_mb}MB limit for {tier} tier")
            f.write(chunk)

    try:
        metadata = analyze_file(str(file_path))
        metadata["original_filename"] = file.filename
        metadata["stored_filename"] = file_id

        # Run compliance checks
        trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
        plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
        compliance = run_compliance_checks(metadata, trim["w"], trim["h"], plat["bleed"], p["platform"])
    except Exception as e:
        await log_failure(db, "upload_analyze", e, project_id=project_id, user_id=user["id"],
                           context={"filename": file.filename, "ext": ext})
        raise HTTPException(500, f"Couldn't analyze this file: {e}. It may be corrupted or an unsupported variant of {ext}.")

    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {
            "uploaded_file": file_id,
            "file_metadata": metadata,
            "compliance": compliance,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return await _present_upload(p, user, {"file_metadata": metadata, "compliance": compliance, "file_id": file_id})


_WEB_SAFE_PREVIEW_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _render_web_preview(fp: Path) -> Response:
    """Renders any uploaded cover/interior file as a browser-safe RGB image
    -- a PNG of page 1 for a PDF, otherwise a JPEG.

    Serving a stored file's raw bytes (what this used to do unconditionally)
    is fine for a plain PNG/JPEG, but browsers can't decode a raw PDF at all,
    and neither browsers nor WebGL textures can decode CMYK -- exactly what
    Auto-Fix's own CMYK conversion always saves as a .tif. That combination
    meant the 3D cover mockup (a WebGL texture, not an <img> tag) would load
    a CMYK TIFF with no error at all and just render the whole book solid
    black, because the texture "loaded" a file whose pixel data the GPU has
    no way to interpret.
    """
    if fp.suffix.lower() == ".pdf":
        import fitz
        with fitz.open(str(fp)) as doc:
            pix = doc[0].get_pixmap(dpi=150)
            png_bytes = pix.tobytes("png")
        return Response(content=png_bytes, media_type="image/png")

    if fp.suffix.lower() in _WEB_SAFE_PREVIEW_EXTS:
        try:
            from PIL import Image
            with Image.open(fp) as img:
                if img.mode in ("RGB", "RGBA", "P", "L"):
                    return FileResponse(str(fp))
        except Exception:
            pass  # mislabeled/corrupt -- fall through and try to re-encode it below

    # CMYK (Auto-Fix's TIFF output), or anything else a browser/WebGL can't
    # decode natively -- convert to a clean sRGB JPEG.
    from PIL import Image
    with Image.open(fp) as img:
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
    return Response(content=buf.getvalue(), media_type="image/jpeg")


@api_router.get("/projects/{project_id}/preview")
async def preview_file(project_id: str, request: Request):
    # Public-ish read (require token via query or cookie)
    token = request.cookies.get("access_token") or request.query_params.get("token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        user_id = payload["sub"]
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user_id})
    if not p or not p.get("uploaded_file"):
        raise HTTPException(404, "No file")
    file_path = UPLOAD_DIR / p["uploaded_file"]
    if not file_path.exists():
        raise HTTPException(404, "File missing")
    try:
        return _render_web_preview(file_path)
    except Exception as e:
        await log_failure(db, "preview_render", e, project_id=project_id, user_id=user_id,
                           context={"filename": p["uploaded_file"]})
        raise HTTPException(500, f"Couldn't render a preview of this file: {e}")


@api_router.post("/projects/{project_id}/autofix")
async def autofix(project_id: str, slot: str = None, user: dict = Depends(get_current_user)):
    """Run all auto-fixes (convert to CMYK, upscale, flatten transparency,
    declare PDF/X-1a) and re-check the result in one pass, so the response
    reflects what's actually true now rather than a promise -- this is the
    fix-then-rescan step the guided workflow relies on for every slot.

    `slot` selects which uploaded file to fix (e.g. "interior" for a
    combined project's interior PDF); omitting it preserves the original
    behavior of fixing the legacy uploaded_file/file_metadata pair (the
    cover / full_wrap file), so existing callers don't need to change."""
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    await _require_product(p, user, "Auto-Fix")

    if slot:
        if slot not in ALLOWED_SLOTS:
            raise HTTPException(400, f"Unknown slot: {slot}")
        slot_data = (p.get("slots") or {}).get(slot)
        if not slot_data or not slot_data.get("stored_filename"):
            raise HTTPException(404, f"No uploaded file in slot '{slot}'")
        stored_filename = slot_data["stored_filename"]
        current_metadata = slot_data
    else:
        if not p.get("uploaded_file"):
            raise HTTPException(404, "No uploaded file")
        stored_filename = p["uploaded_file"]
        current_metadata = p.get("file_metadata", {})

    file_path = UPLOAD_DIR / stored_filename
    # The customer's actual uploaded source, preserved forever once set --
    # never deleted by a repair pass below, only by the customer explicitly
    # replacing (slot_upload) or deleting (slot_delete) this slot. If this
    # is the first repair ever run on this slot, current_metadata has no
    # original_stored_filename yet, so the file we're about to fix IS the
    # original -- every os.remove() below is guarded against removing
    # whichever path this resolves to.
    original_stored_filename = current_metadata.get("original_stored_filename") or stored_filename
    original_path = UPLOAD_DIR / original_stored_filename

    def _remove_if_not_original(path: Path):
        if Path(path) != original_path:
            try:
                os.remove(path)
            except OSError:
                pass

    ghostscript_result = None
    interior_margin_fix = None
    if current_metadata.get("is_pdf"):
        # Interior manuscripts get a geometry pass FIRST, before the
        # Ghostscript/PDF-X pass below -- a wrong page size or a too-tight
        # safety margin is the #1 reason distributors reject an interior
        # (IngramSpark's own rejection reason is literally "content extends
        # outside the safety area" / "content is not centered"), and unlike
        # font/ICC issues this one WAS actually fixable, just never wired up:
        # scaling+recentering a real text-layer PDF via a content-stream
        # transform is lossless (no rasterizing, no re-flowed text), so
        # there's no reason to leave it as manual-fix-only guidance the way
        # pdfx_validator's one_click_fix=False currently claims.
        if slot == "interior":
            trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
            plat_name = PLATFORMS.get(p["platform"], {}).get("name", "your distributor")
            margin_findings = await run_with_timeout(
                check_interior_safety_margins,
                str(file_path), plat_name, trim["w"], trim["h"], max_pages=BASIC_CHECK_MAX_PAGES,
            )
            needs_geometry_fix = any(
                f["id"] in ("interior_page_size_mismatch", "interior_safety_margin") for f in margin_findings
            )
            if needs_geometry_fix:
                geom_fixed_name = f"{project_id}_interior_marginfixed_{uuid.uuid4().hex[:6]}.pdf"
                geom_fixed_path = UPLOAD_DIR / geom_fixed_name
                try:
                    interior_margin_fix = await run_with_timeout(
                        autofix_interior_safety_margins,
                        str(file_path), str(geom_fixed_path), trim["w"], trim["h"],
                    )
                    interior_margin_fix["attempted"] = True
                    interior_margin_fix["succeeded"] = True
                    _remove_if_not_original(file_path)
                    file_path = geom_fixed_path
                    stored_filename = geom_fixed_name
                except HTTPException:
                    raise
                except Exception as e:
                    interior_margin_fix = {"attempted": True, "succeeded": False, "reason": str(e)}
                    await log_failure(db, "autofix_interior_margin", e, project_id=project_id, user_id=user["id"])

        # Check what actually needs fixing before touching the file --
        # only PDF/X-1a declaration, live transparency, and layers are
        # things Ghostscript's -dPDFX pipeline can genuinely repair (it
        # flattens transparency and forces CMYK/PDF-X metadata in the same
        # pass). Font embedding and missing ICC profiles aren't safely
        # auto-fixable this way, so those are left as manual guidance.
        structure_findings = await run_with_timeout(run_pdf_structure_audit, str(file_path), PLATFORMS.get(p["platform"], {}).get("name", "your distributor"), max_pages=BASIC_CHECK_MAX_PAGES)
        fixable_ids = {"pdfx1a_not_declared", "live_transparency_detected", "layers_detected", "pdfx1a_missing_output_intent"}
        needs_gs_fix = any(f["id"] in fixable_ids for f in structure_findings)

        if needs_gs_fix:
            if not find_ghostscript():
                ghostscript_result = {
                    "attempted": True,
                    "succeeded": False,
                    "reason": "Ghostscript isn't installed on this server, so live transparency/layers/PDF-X1a declaration couldn't be auto-repaired. These issues still need a manual fix (see fix_steps below) until Ghostscript is set up.",
                }
                metadata = await run_with_timeout(analyze_file, str(file_path))
            else:
                fixed_name = f"{project_id}_{slot or 'cover'}_gsfixed_{uuid.uuid4().hex[:6]}.pdf"
                fixed_path = UPLOAD_DIR / fixed_name
                try:
                    await run_with_timeout(convert_to_pdfx1a, str(file_path), str(fixed_path), title=p.get("name", "SparkPrep Export"))
                    _remove_if_not_original(file_path)
                    file_path = fixed_path
                    metadata = await run_with_timeout(analyze_file, str(fixed_path))
                    metadata["original_filename"] = current_metadata.get("original_filename")
                    metadata["stored_filename"] = fixed_name
                    metadata["autofixed"] = True
                    # Re-run the structural audit against the fixed file so the
                    # response reflects what's actually true now, not a promise.
                    after_findings = await run_with_timeout(run_pdf_structure_audit, str(fixed_path), PLATFORMS.get(p["platform"], {}).get("name", "your distributor"), max_pages=BASIC_CHECK_MAX_PAGES)
                    still_broken_findings = [f for f in after_findings if f["id"] in fixable_ids]
                    still_broken = [f["id"] for f in still_broken_findings]
                    # pdfx1a_not_declared/pdfx1a_missing_output_intent get stamped
                    # unconditionally at export time regardless of whether THIS
                    # pre-export Ghostscript attempt succeeds -- both
                    # build_interior_pdf_x1a() and build_print_ready_pdf() always
                    # (re)write GTS_PDFXVersion/OutputIntents/XMP via
                    # _declare_pdfx1a(), independent of this step. So a leftover
                    # failure on ONLY those two doesn't actually block export --
                    # it was being reported as "needs a manual fix" and shown
                    # right next to "you're ready for Final Review" in the same
                    # breath, which is exactly as contradictory as it sounds.
                    # live_transparency_detected/layers_detected are NOT redone at
                    # export, so those genuinely are still broken if unresolved here.
                    ALWAYS_FIXED_AT_EXPORT = {"pdfx1a_not_declared", "pdfx1a_missing_output_intent"}
                    genuinely_unresolved = [f for f in still_broken_findings if f["id"] not in ALWAYS_FIXED_AT_EXPORT]
                    ghostscript_result = {
                        "attempted": True,
                        "succeeded": not genuinely_unresolved,
                        "fixed_issues": [f["id"] for f in structure_findings if f["id"] in fixable_ids],
                        "still_present": still_broken,
                        "reason": (
                            "Ghostscript fixed some issues automatically, but couldn't resolve: "
                            + "; ".join(f["title"] for f in genuinely_unresolved)
                            + ". This needs the source file re-exported from the original design tool with the correct settings -- see the fix steps on that check below."
                        ) if genuinely_unresolved else None,
                    }
                except RuntimeError as e:
                    ghostscript_result = {"attempted": True, "succeeded": False, "reason": str(e)}
                    metadata = await run_with_timeout(analyze_file, str(file_path))
                    await log_failure(db, "autofix_ghostscript", e, project_id=project_id, user_id=user["id"],
                                       context={"fixable_ids": [f["id"] for f in structure_findings if f["id"] in fixable_ids]})
        else:
            metadata = await run_with_timeout(analyze_file, str(file_path))
    else:
        plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
        # Cover text sitting too close to the trim edge can be pulled back
        # into the safe margin automatically by scaling the whole flat
        # cover slightly inward before the CMYK conversion below, so both
        # fixes land in the same Auto-Fix pass -- see
        # autofix_cover_safe_margin's docstring for why this only applies
        # to image covers (PDFs use the Ghostscript path above instead).
        margin_source_path = file_path
        effective_slot_for_margins = slot or "full_wrap"
        final_w, final_h = _target_inches_for_slot(p, effective_slot_for_margins)
        margin_geom_kwargs = {}
        if effective_slot_for_margins == "full_wrap":
            geom = _full_wrap_geometry(p)
            margin_geom_kwargs = {"spine_x_in": geom["spine_x"], "spine_w_in": geom["spine_width"],
                                   "page_count": p.get("page_count"), "binding": p.get("binding", "paperback")}
        margin_bleed = _cover_bleed_for_slot(p, effective_slot_for_margins) or 0.0
        margin_findings = await run_with_timeout(
            check_cover_safety_margins,
            str(file_path), False, final_w, final_h, plat.get("name", "your distributor"),
            bleed_in=margin_bleed, **margin_geom_kwargs,
        )
        outer_margin_finding = next((f for f in margin_findings if f["id"] == "cover_safety_margin"), None)
        if outer_margin_finding:
            margin_fixed_name = f"{project_id}_{slot or 'cover'}_marginfixed_{uuid.uuid4().hex[:6]}{Path(file_path).suffix}"
            margin_fixed_path = UPLOAD_DIR / margin_fixed_name
            try:
                await run_with_timeout(
                    autofix_cover_safe_margin,
                    str(file_path), str(margin_fixed_path),
                    # the fixer works in distances from the file's edge: the trim line is margin_bleed in
                    outer_margin_finding["pinpoint"]["margin_in"] + margin_bleed, final_w, final_h,
                    target_margin_in=0.27 + margin_bleed,
                )
                margin_source_path = margin_fixed_path
            except HTTPException:
                raise
            except Exception as e:
                await log_failure(db, "autofix_cover_margin", e, project_id=project_id, user_id=user["id"],
                                   context={"slot": slot})

        # Spine text sitting too close to (or across) the spine's own fold lines gets its
        # own pass, confined to just the spine band -- see autofix_spine_text_margin's
        # docstring for why this is separate from the whole-cover fix above. Applied on top
        # of margin_source_path (the outer-margin-fixed file, if that ran) since the outer
        # fix keeps the canvas the same size, so the spine's own inch geometry is still valid.
        spine_margin_finding = next((f for f in margin_findings if f["id"] == "cover_spine_text_margin"), None)
        if spine_margin_finding and margin_geom_kwargs.get("spine_w_in"):
            target_margin_in = SPINE_SAFETY_WIDE_IN if margin_geom_kwargs["spine_w_in"] >= SPINE_WIDTH_TIER_THRESHOLD_IN else SPINE_SAFETY_NARROW_IN
            spine_fixed_name = f"{project_id}_{slot or 'cover'}_spinefixed_{uuid.uuid4().hex[:6]}{Path(margin_source_path).suffix}"
            spine_fixed_path = UPLOAD_DIR / spine_fixed_name
            try:
                await run_with_timeout(
                    autofix_spine_text_margin,
                    str(margin_source_path), str(spine_fixed_path),
                    spine_margin_finding["pinpoint"]["clearance_in"], final_w, final_h,
                    margin_geom_kwargs["spine_x_in"], margin_geom_kwargs["spine_w_in"], target_margin_in,
                )
                if margin_source_path != file_path:
                    _remove_if_not_original(margin_source_path)
                margin_source_path = spine_fixed_path
            except HTTPException:
                raise
            except Exception as e:
                await log_failure(db, "autofix_spine_margin", e, project_id=project_id, user_id=user["id"],
                                   context={"slot": slot})

        # Convert to CMYK TIFF, clamped to this platform's total-ink-coverage limit
        fixed_name = f"{project_id}_{slot or 'cover'}_fixed_{uuid.uuid4().hex[:6]}.tif"
        fixed_path = UPLOAD_DIR / fixed_name
        tac_limit = TAC_THRESHOLD_BY_PLATFORM.get(p["platform"], TAC_THRESHOLD_DEFAULT)
        try:
            await run_with_timeout(convert_to_cmyk, str(margin_source_path), str(fixed_path), 300, tac_limit=tac_limit)
        except HTTPException:
            raise
        except Exception as e:
            await log_failure(db, "autofix_cmyk", e, project_id=project_id, user_id=user["id"],
                               context={"file_ext": Path(file_path).suffix.lower(), "slot": slot})
            raise HTTPException(500, f"CMYK conversion failed: {e}")
        _remove_if_not_original(file_path)
        if margin_source_path != file_path:
            _remove_if_not_original(margin_source_path)
        # The compliance re-check below (and the response's file_metadata)
        # need to look at the actual fixed file, not the original this
        # branch just deleted -- without this, the re-check after autofix
        # was silently running against a missing file, and every
        # file-dependent check (ink coverage, cover-text OCR, DPI) would
        # come back empty regardless of whether the fix actually worked.
        file_path = fixed_path
        metadata = await run_with_timeout(analyze_file, str(fixed_path))
        metadata["original_filename"] = current_metadata.get("original_filename")
        metadata["stored_filename"] = fixed_name
        metadata["autofixed"] = True

    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    # No explicit slot means the legacy full-cover path (see _replace_slot's
    # legacy branch, which now keeps slots.full_wrap in sync for exactly
    # this reason) -- treat it as full_wrap for sizing purposes too.
    effective_slot = slot or "full_wrap"
    final_w, final_h = _target_inches_for_slot(p, effective_slot)
    spine_kwargs = {}
    if effective_slot == "full_wrap":
        geom = _full_wrap_geometry(p)
        spine_kwargs = {"spine_x_in": geom["spine_x"], "spine_w_in": geom["spine_width"],
                         "page_count": p.get("page_count"), "binding": p.get("binding", "paperback")}
    compliance = await run_with_timeout(
        run_compliance_checks,
        metadata, trim["w"], trim["h"], plat["bleed"], p["platform"],
        file_path=str(file_path), slot=slot, platform_name=plat.get("name"), max_pages=BASIC_CHECK_MAX_PAGES,
        final_w=final_w, final_h=final_h, **spine_kwargs,
    )
    # Branches above that didn't actually replace the file (no fix needed, or
    # Ghostscript unavailable) don't set stored_filename on the fresh
    # metadata -- fall back to the file we started with so it doesn't go
    # missing from the project record.
    metadata.setdefault("stored_filename", stored_filename)
    # Carry the preserved original forward into the saved record -- so the
    # *next* autofix call (or _replace_slot, e.g. a later AI Upscale) still
    # knows which file on disk must never be deleted, even though
    # stored_filename itself has now moved on to this fix's output.
    metadata["original_stored_filename"] = original_stored_filename

    if slot:
        metadata["slot"] = slot
        slots = p.get("slots") or {}
        slots[slot] = {**metadata, "compliance": compliance}
        update = {"slots": slots, "updated_at": datetime.now(timezone.utc).isoformat()}
        if slot == "full_wrap":
            update["uploaded_file"] = metadata["stored_filename"]
            update["file_metadata"] = metadata
            update["compliance"] = compliance
    else:
        update = {
            "uploaded_file": metadata["stored_filename"],
            "file_metadata": metadata, "compliance": compliance,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # This legacy (no-slot) path is really just fixing the full_wrap
        # cover under its old name -- if the project also has a slots.full_wrap
        # entry (any slot-based upload, e.g. a cover-template render, sets
        # both), it must be kept in sync too. Without this, autofix deletes
        # the old file and updates only uploaded_file/file_metadata, leaving
        # slots.full_wrap pointing at a file that no longer exists -- so the
        # very next slot-based call (AI Upscale, AI Cover, re-autofix by
        # slot) 404s on "No uploaded file in slot 'full_wrap'" even though
        # the user just successfully fixed that exact cover.
        existing_slots = p.get("slots") or {}
        if existing_slots.get("full_wrap"):
            existing_slots["full_wrap"] = {**metadata, "compliance": compliance}
            update["slots"] = existing_slots
    await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": update})
    return {"slot": slot, "file_metadata": metadata, "compliance": compliance, "ghostscript_fix": ghostscript_result, "interior_margin_fix": interior_margin_fix, "check_type": "basic"}


# ---- Verified auto-fix (four-agent pipeline) ----
# The repair engine above (autofix) is unchanged. This is the outside layer
# around it: Agent 1 confirms the problem is real, Agent 2 runs autofix(),
# Agent 3 independently verifies the result, Agent 4 audits the whole trail
# -- and the customer watches all of it live. See backend/autofix_agents/.
_BG_TASKS: set = set()
SUPERVISOR_MODEL = os.environ.get("ANTHROPIC_SUPERVISOR_MODEL", "claude-haiku-4-5-20251001")


def _resolve_slot_target(p: dict, slot: Optional[str]) -> tuple:
    """(stored_filename, current_metadata) for the file autofix would act on --
    same resolution as autofix()'s own opening block."""
    if slot:
        if slot not in ALLOWED_SLOTS:
            raise HTTPException(400, f"Unknown slot: {slot}")
        slot_data = (p.get("slots") or {}).get(slot)
        if not slot_data or not slot_data.get("stored_filename"):
            raise HTTPException(404, f"No uploaded file in slot '{slot}'")
        return slot_data["stored_filename"], slot_data
    if not p.get("uploaded_file"):
        raise HTTPException(404, "No uploaded file")
    return p["uploaded_file"], p.get("file_metadata", {})


def _scan_slot_sync(p: dict, slot: Optional[str], stored_filename: str) -> dict:
    """A fresh, from-scratch scan of one stored file -- the same checks (with
    the same parameters) autofix()'s own final re-check and the upload scan
    use. Blocking; callers run it through a worker thread with a time limit."""
    file_path = UPLOAD_DIR / stored_filename
    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    metadata = analyze_file(str(file_path))
    effective_slot = slot or "full_wrap"
    final_w, final_h = _target_inches_for_slot(p, effective_slot)
    spine_kwargs = {}
    if effective_slot == "full_wrap":
        geom = _full_wrap_geometry(p)
        spine_kwargs = {"spine_x_in": geom["spine_x"], "spine_w_in": geom["spine_width"],
                         "page_count": p.get("page_count"), "binding": p.get("binding", "paperback")}
    compliance = run_compliance_checks(
        metadata, trim["w"], trim["h"], plat["bleed"], p["platform"],
        file_path=str(file_path), slot=slot, platform_name=plat.get("name"), max_pages=BASIC_CHECK_MAX_PAGES,
        final_w=final_w, final_h=final_h, **spine_kwargs,
    )
    structure = []
    if metadata.get("is_pdf"):
        structure = run_pdf_structure_audit(str(file_path), plat.get("name", "your distributor"), max_pages=BASIC_CHECK_MAX_PAGES)
    return {"metadata": metadata, "compliance": compliance, "structure": structure}


@api_router.post("/projects/{project_id}/autofix/verified")
async def autofix_verified(project_id: str, slot: str = None, stream: bool = True, user: dict = Depends(get_current_user)):
    """Auto-fix with independent verification and live progress.

    stream=true (default) returns newline-delimited JSON events as each agent
    works, ending with a `result` event; stream=false runs the same pipeline
    and returns the final result plus the full audit trail as one JSON body.
    Either way the fix is only kept if Agent 3 and Agent 4 approve it --
    otherwise the file and project record are rolled back."""
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    await _require_product(p, user)
    _resolve_slot_target(p, slot)  # fail fast (404/400) exactly like autofix()
    if not autofix_agents.try_claim(project_id, slot):
        raise HTTPException(409, "An auto-fix is already running for this file - hang tight.")

    async def get_project():
        return await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})

    async def save_fields(fields: dict, unset: list):
        upd = {}
        if fields:
            upd["$set"] = fields
        if unset:
            upd["$unset"] = {k: "" for k in unset}
        if upd:
            await db.projects.update_one({"_id": ObjectId(project_id)}, upd)

    async def log(stage, exc, context):
        await log_failure(db, stage, exc, project_id=project_id, user_id=user["id"], context=context)

    ai_review = None
    if ANTHROPIC_API_KEY:
        async def ai_review(summary):
            return await autofix_agents.anthropic_review(summary, api_key=ANTHROPIC_API_KEY, model=SUPERVISOR_MODEL)

    deps = autofix_agents.Deps(
        project_id=project_id, slot=slot, user_id=user["id"], upload_dir=UPLOAD_DIR,
        get_project=get_project, save_fields=save_fields, resolve_target=_resolve_slot_target,
        scan_fn=_scan_slot_sync,
        repair_fn=lambda: autofix(project_id=project_id, slot=slot, user=user),
        ai_review=ai_review, log=log,
        on_unsolved=lambda issues: _record_unsolved_case(project_id, user["id"], "repair_bay", [slot] if slot else [],
                                                         [{"title": i.get("label") or i.get("id"), "why": i.get("message", ""),
                                                           "id": i.get("id")} for i in issues]),
        budget_s=(85.0 if stream else REQUEST_TIMEOUT_S - 5.0) if autofix_agents.common.time_limits_on() else REQUEST_TIMEOUT_S,
    )
    queue: asyncio.Queue = asyncio.Queue()
    # Detached from the HTTP response on purpose: if the customer closes the
    # tab mid-run, the pipeline still finishes -- including its rollback --
    # instead of being cancelled half-way with a repair applied but unverified.
    task = asyncio.create_task(autofix_agents.run(deps, queue.put_nowait))
    _BG_TASKS.add(task)

    def _done(t):
        _BG_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("verified autofix crashed: %r", t.exception())
            queue.put_nowait({"type": "result", "agent": 0, "message": "Something went wrong.", "data": {
                "pipeline": {"status": "error", "message": "Something unexpected went wrong. Please try again."}}})
    task.add_done_callback(_done)

    if not stream:
        return await task

    async def events():
        while True:
            ev = await queue.get()
            yield _json.dumps(ev, default=str) + "\n"
            if ev.get("type") == "result":
                return

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@api_router.post("/projects/{project_id}/autofix/confirm")
async def autofix_confirm(project_id: str, slot: str = None, user: dict = Depends(get_current_user)):
    """The customer's "press Enter for system confirmation" step: re-runs the
    original scan from scratch on the file as it is saved right now, stores
    that fresh result as the project's current compliance, and returns it --
    so the number the customer ends on is a brand-new measurement, not a
    carried-over claim."""
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    await _require_product(p, user)
    stored_filename, current = _resolve_slot_target(p, slot)
    scan = await run_with_timeout(_scan_slot_sync, p, slot, stored_filename)
    compliance = scan["compliance"]
    metadata = {**{k: v for k, v in current.items() if k != "compliance"}, **scan["metadata"]}
    now = datetime.now(timezone.utc).isoformat()
    if slot:
        slots = p.get("slots") or {}
        slots[slot] = {**metadata, "compliance": compliance}
        update = {"slots": slots, "updated_at": now}
        if slot == "full_wrap":
            update.update({"uploaded_file": metadata.get("stored_filename", stored_filename),
                           "file_metadata": metadata, "compliance": compliance})
    else:
        update = {"file_metadata": metadata, "compliance": compliance, "updated_at": now}
        if (p.get("slots") or {}).get("full_wrap"):
            slots = p["slots"]
            slots["full_wrap"] = {**metadata, "compliance": compliance}
            update["slots"] = slots
    await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": update})
    return {"slot": slot, "file_metadata": metadata, "compliance": compliance, "check_type": "basic"}


@api_router.post("/projects/{project_id}/final-review")
async def final_review(project_id: str, user: dict = Depends(get_current_user)):
    """One last combined check across every file the project actually
    needs (cover for cover/combined projects, interior for interior/combined
    projects) before export -- the guided workflow's last step. Re-runs
    compliance fresh against whatever's currently uploaded rather than
    trusting stale results from an earlier upload/autofix call, and reduces
    everything to a single stoplight verdict:
      - red: at least one section has a hard failure -- must not export yet
      - yellow: no failures, but some warnings remain -- exportable but worth reviewing
      - green: every required section passed clean
    """
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    await _require_product(p, user, "Final Review")

    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    project_type = p.get("project_type", "cover")
    needs_cover = project_type in ("cover", "combined")
    needs_interior = project_type in ("interior", "combined")

    sections = {}
    if needs_cover:
        cover_meta = (p.get("slots") or {}).get("full_wrap") or p.get("file_metadata")
        if not cover_meta:
            sections["cover"] = {"uploaded": False, "compliance": []}
        else:
            cover_path = UPLOAD_DIR / cover_meta["stored_filename"] if cover_meta.get("stored_filename") else None
            final_w, final_h = _target_inches_for_slot(p, "full_wrap")
            geom = _full_wrap_geometry(p)
            sections["cover"] = {"uploaded": True, "compliance": await run_with_timeout(
                run_compliance_checks,
                cover_meta, trim["w"], trim["h"], plat["bleed"], p["platform"],
                file_path=str(cover_path) if cover_path else None, slot="full_wrap",
                platform_name=plat.get("name"), final_w=final_w, final_h=final_h,
                spine_x_in=geom["spine_x"], spine_w_in=geom["spine_width"],
                page_count=p.get("page_count"), binding=p.get("binding", "paperback"),
                expected_isbn=p.get("isbn"),
            )}
    if needs_interior:
        interior_meta = (p.get("slots") or {}).get("interior")
        if not interior_meta:
            sections["interior"] = {"uploaded": False, "compliance": []}
        else:
            interior_path = UPLOAD_DIR / interior_meta["stored_filename"] if interior_meta.get("stored_filename") else None
            sections["interior"] = {"uploaded": True, "compliance": await run_with_timeout(
                run_compliance_checks,
                interior_meta, trim["w"], trim["h"], plat["bleed"], p["platform"],
                file_path=str(interior_path) if interior_path else None, slot="interior",
                platform_name=plat.get("name"), max_pages=BASIC_CHECK_MAX_PAGES,
            )}

    if sections.get("cover", {}).get("uploaded"):
        sections["cover"]["compliance"] = _mark_export_fixes(p, "full_wrap", cover_meta, sections["cover"]["compliance"])
    if sections.get("interior", {}).get("uploaded"):
        sections["interior"]["compliance"] = _mark_export_fixes(p, "interior", interior_meta, sections["interior"]["compliance"])
    all_checks = [c for s in sections.values() for c in s["compliance"]]
    any_missing = any(not s["uploaded"] for s in sections.values())
    any_fail = any(c["status"] == "fail" for c in all_checks)
    any_warning = any(c["status"] == "warning" and not c.get("fixed_on_export") for c in all_checks)

    if any_missing:
        status, message = "red", "Not every required file has been uploaded yet."
    elif any_fail:
        status, message = "red", "One or more sections still have a failing check -- fix those before exporting."
    elif any_warning:
        status, message = "yellow", "No failures, but some warnings remain -- you can export, but review them first."
    else:
        status, message = "green", "Every check passed. Ready to export."

    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"last_final_review": {"status": status, "message": message, "checked_at": datetime.now(timezone.utc).isoformat()}}},
    )
    return {"status": status, "message": message, "sections": sections}


# ---- Export ----
SUPPORT_EMAIL = "legenddaryslifecheatcodes@gmail.com"
# The server's disk is 1 GB and a book's exports run 10-40 MB each; with unlimited exports per book, keeping
# every one would fill it. Downloads are opened the moment an export finishes and nothing lists old ones.
KEEP_EXPORTS_PER_PROJECT = 3
_EXPORT_FILE_RE = re.compile(r"^[0-9a-f]{24}_(?:cover|interior|export)_([0-9a-z]+)\.(?:pdf|zip)$")


def _prune_old_exports(project_id: str, keep: int) -> int:
    """Keep this project's `keep` most recent exports (each export = its cover/interior PDFs and/or zip)."""
    groups: dict = {}
    for f in EXPORT_DIR.glob(f"{project_id}_*"):
        m = _EXPORT_FILE_RE.match(f.name)
        if m:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            g = groups.setdefault(m.group(1), {"files": [], "newest": 0.0})
            g["files"].append(f)
            g["newest"] = max(g["newest"], mtime)
    removed = 0
    for g in sorted(groups.values(), key=lambda g: g["newest"], reverse=True)[keep:]:
        for f in g["files"]:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _delete_project_files(project_id: str, p: dict) -> int:
    """Every file on disk that belongs to this project: slot files (current, original, raw manuscript), the legacy
    single upload, and anything else named with the project's id (repairs, intermediates, exports)."""
    names = set()
    for meta in list((p.get("slots") or {}).values()) + [p.get("file_metadata") or {}]:
        for key in ("stored_filename", "original_stored_filename", "raw_manuscript_stored_filename"):
            if meta.get(key):
                names.add(Path(meta[key]).name)                   # .name: never follow a path out of the folder
    if p.get("uploaded_file"):
        names.add(Path(p["uploaded_file"]).name)
    paths = {UPLOAD_DIR / n for n in names}
    for folder in (UPLOAD_DIR, EXPORT_DIR):
        paths.update(folder.glob(f"{project_id}_*"))
    removed = 0
    for path in paths:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


async def _enforce_same_book(window: dict, p: dict, user_id: str) -> None:
    """One book pass = one book (and its revisions). The first export records what the book says; later
    exports in the same window must be that book. See book_pass/fingerprint.py. Never blocks on a failure of
    its own -- an honest customer must not be stopped because fingerprinting hiccuped."""
    try:
        current = await run_with_timeout(book_pass.fingerprint.project_fingerprint, p, UPLOAD_DIR)
    except Exception as e:  # noqa: BLE001
        await log_failure(db, "book_fingerprint", e, project_id=str(p["_id"]), user_id=user_id)
        return
    stored = window.get("fingerprint")
    if not stored:
        await db.book_credits.update_one({"credit_id": window["credit_id"]}, {"$set": {"fingerprint": current}})
        return
    verdict, details = book_pass.fingerprint.compare(stored, current)
    if verdict != "same":
        await db.book_flags.insert_one({
            "credit_id": window["credit_id"], "user_id": user_id, "project_id": str(p["_id"]),
            "project_name": p.get("name"), "verdict": verdict, "details": details,
            "blocked": verdict == "different", "reviewed": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    if verdict == "different":
        started = (window.get("activated_at") or "")[:10]
        raise HTTPException(409, {"code": "different_book", "msg": (
            f"These files look like a different book from the one you started{' on ' + started if started else ''}. "
            "Each book pass covers one book, including all its revisions — start a new book to export this one. "
            f"If this really is the same book, email {SUPPORT_EMAIL} and we'll sort it out right away.")})
    merged = book_pass.fingerprint.merge(stored, current)
    if merged != stored:
        await db.book_credits.update_one({"credit_id": window["credit_id"]}, {"$set": {"fingerprint": merged}})


async def _export_project_core(project_id: str, user: dict) -> dict:
    """Core export logic, shared by the single-project export endpoint and
    batch_export(). acting `user` owns the project; billing (tier, usage
    counters, white-label branding) resolves through get_billing_user() so
    team members correctly draw against their org owner's shared pool."""
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")

    # This used to check only the legacy p["uploaded_file"] field, which is
    # NEVER set by a slot upload (nothing mirrors slots.interior into it,
    # unlike slots.full_wrap) -- so exporting any interior-only or combined
    # project always 404'd here regardless of what was actually uploaded.
    project_type = p.get("project_type", "cover")
    needs_cover = project_type in ("cover", "combined")
    needs_interior = project_type in ("interior", "combined")

    cover_data = _cover_file_meta(p) if needs_cover else None
    if needs_cover and not cover_data:
        raise HTTPException(404, "No cover file uploaded to export")
    interior_data = _interior_file_meta(p) if needs_interior else None
    if needs_interior and not interior_data:
        raise HTTPException(404, "No interior file uploaded to export")
    # Dust-jacket books need a third file: the plain case underneath the
    # jacket. Real IngramSpark submission for one of these literally will
    # not process until all three files (case, jacket, interior) are filled.
    # A cover uploaded as separate front / spine / back pieces is assembled into one wrap at export.
    piece_slots = p.get("slots") or {}
    from_pieces = needs_cover and not piece_slots.get("full_wrap") and any(
        piece_slots.get(k) for k in ("front_cover", "spine", "back_cover"))
    if from_pieces:
        if p.get("binding", "paperback") != "paperback":
            raise HTTPException(400, "Hardcover covers need one combined file — the hinge and wrap areas (and a jacket's "
                                     "flaps) aren't part of separate front and back pieces. Upload your cover as one combined file.")
        missing = [name for k, name in (("front_cover", "front"), ("spine", "spine"), ("back_cover", "back")) if not piece_slots.get(k)]
        if missing:
            raise HTTPException(404, f"Upload your cover's {' and '.join(missing)} too — separate pieces need all three.")
    needs_case = needs_cover and p.get("binding") == "hardcover_jacket"
    case_data = (p.get("slots") or {}).get("case_wrap") if needs_case else None
    if needs_case and not case_data:
        raise HTTPException(404, "No case cover uploaded -- a Hardcover with Dust Jacket book needs a separate plain case file too, in addition to the jacket.")
    # Never build a hardcover cover around a guessed spine.
    if needs_cover and _project_needs_spine_number(p):
        raise HTTPException(400, _spine_needed_message(PLATFORMS.get(p["platform"], PLATFORMS["kdp"])["name"]))

    # Mandatory book title -- required before any export so exports are
    # traceable to a real book, not left on a never-renamed placeholder.
    title = (p.get("name") or "").strip()
    if not title or title.lower() == "untitled book":
        raise HTTPException(400, "Please set a title for this book before exporting.")

    # Block export while any slot still has an unresolved compliance
    # failure -- a "successful" export of a file that fails real distributor
    # specs just pushes the same rejection downstream instead of catching it
    # here, which was happening because nothing in this function ever
    # checked compliance status before generating the PDF.
    slots = p.get("slots") or {}
    all_compliance = []
    if slots:
        for slot_data in slots.values():
            all_compliance.extend(slot_data.get("compliance") or [])
    else:
        all_compliance = p.get("compliance") or []

    failing = [c for c in all_compliance if c.get("status") == "fail"]
    if failing:
        labels = ", ".join(c.get("label") or c.get("id") or "unknown issue" for c in failing)
        raise HTTPException(
            400,
            f"Can't export yet -- unresolved compliance failures: {labels}. Fix these first, then export again.",
        )

    billing_user = await get_billing_user(user)

    # Check usage limits (bypassed entirely for active beta testers, or for
    # this specific project if a "full_access"/"interior_only_access" promo
    # code was redeemed on it -- those are per-project, not account-wide,
    # unlike the beta flag).
    tier = billing_user.get("tier", "free")
    tier_info = TIERS.get(tier, TIERS["free"])
    export_limit = tier_info["monthly_exports"]
    used = billing_user.get("exports_this_month", 0)
    promo_bypass = p.get("promo_access") in ("full_access", "interior_only_access")
    beta_bypass = bool(user.get("beta_active") or billing_user.get("beta_active") or promo_bypass)
    book_model = book_pass.book_pass_on() and not is_admin_user(user)
    if book_model and not beta_bypass:
        # Book model: exports are unlimited while THIS book's 7-day window is open, and only then.
        try:
            window = await book_pass.entitlements.require_active_book(db, billing_user["id"], project_id)
        except book_pass.BookRequired as e:
            raise HTTPException(402, {"code": "book_required", "has_credit": e.has_credit, "msg": (
                "Start this book to export (you have a book ready to use)." if e.has_credit
                else f"To export, get a book — {book_pass.book_offer_text()}.")})
        await _enforce_same_book(window, p, billing_user["id"])
    if book_model:
        pass                                            # the legacy monthly/per-book/plan limits below do not apply
    elif not beta_bypass and used >= export_limit:
        raise HTTPException(402, f"Monthly export limit reached ({used}/{export_limit}). Upgrade to continue.")

    # Per-book export cap -- flat 5 exports per book, independent of the
    # account's monthly allowance above.
    book_exports_used = p.get("exports_used", 0)
    if not book_model and not beta_bypass and book_exports_used >= EXPORTS_PER_BOOK:
        raise HTTPException(402, f"You have reached the {EXPORTS_PER_BOOK}-export limit for this book.")

    # Book counter — this project counts as a "book" the first time it's exported this period
    books_used = billing_user.get("books_this_month", 0)
    books_limit = tier_info["books_per_month"]
    is_new_book = not p.get("first_exported_at")
    if is_new_book and not beta_bypass and not book_model:
        if books_limit <= 0:
            raise HTTPException(
                402,
                f"Your {tier_info['name']} plan doesn't include full book exports. Upgrade to Author or higher.",
            )
        if books_used >= books_limit:
            raise HTTPException(
                402,
                f"Monthly book allowance reached ({books_used}/{books_limit} books). Upgrade or wait until next cycle.",
            )

    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    paper = PAPER_TYPES.get(p["paper_type"], PAPER_TYPES["white_50lb"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    platform_key = p.get("platform", "kdp")
    binding = p.get("binding", "paperback")
    # Cover bleed and hardcover spine are both properties of the (platform,
    # binding) pair -- e.g. IngramSpark/KDP case-laminate wrap bleed vs
    # Lulu's flat 0.125" for every binding -- not just the binding alone.
    # Interior bleed stays platform-level since that genuinely doesn't vary
    # by binding.
    cover_bleed = resolve_binding_spec(binding, platform_key)["bleed"]
    spine_w = calculate_spine_width_for_platform(p.get("page_count", 0), paper_ppi(paper, platform_key), platform_key, binding)[0] if needs_cover else 0
    if needs_cover and p.get("spine_width_override"):
        spine_w = float(p["spine_width_override"])
    export_id = uuid.uuid4().hex[:6]

    color_profile = (p.get("adjustments") or {}).get("color_profile") or DEFAULT_COLOR_PROFILE
    # White-label: only takes effect if the *billing* account's tier actually
    # includes it (Studio) -- a team member exporting under a Publisher
    # owner's plan still gets "SparkPrep", not a stale/downgraded brand name.
    producer_name = "SparkPrep"
    if TIERS.get(tier, {}).get("white_label") and billing_user.get("white_label_brand_name"):
        producer_name = billing_user["white_label_brand_name"]

    cover_result = None
    interior_result = None
    case_result = None

    if needs_cover:
        cover_path = UPLOAD_DIR / cover_data["stored_filename"]
        cover_export_path = EXPORT_DIR / f"{project_id}_cover_{export_id}.pdf"
        # If a valid ISBN is set on the project, generate the barcode PNG for the back cover --
        # unless the uploaded cover art already has its own barcode built in, in which case
        # overlaying another one would stamp a second barcode on top of it.
        barcode_png = None
        if p.get("isbn") and not p.get("cover_has_barcode"):
            try:
                barcode_png = generate_barcode_png_bytes(p["isbn"])
            except Exception:
                barcode_png = None
        assembled_path = None
        try:
            if from_pieces:
                assembled_path = EXPORT_DIR / f"{project_id}_assembled_{export_id}.img"
                await run_with_timeout(
                    assemble_cover_pieces,
                    *(str(UPLOAD_DIR / piece_slots[k]["stored_filename"]) for k in ("back_cover", "spine", "front_cover")),
                    str(assembled_path), trim_w=trim["w"], trim_h=trim["h"], bleed=cover_bleed, spine_w=spine_w,
                )
                cover_path = assembled_path
            cover_result = await run_with_timeout(
                build_print_ready_pdf,
                str(cover_path), str(cover_export_path),
                trim_w=trim["w"], trim_h=trim["h"],
                bleed=cover_bleed, spine_w=spine_w,
                is_cover=True, title=p["name"],
                author=(user.get("name") or ""),
                barcode_png_bytes=barcode_png,
                color_profile=color_profile,
                producer_name=producer_name,
                binding=binding,
                platform=platform_key,
            )
        except HTTPException:
            raise
        except Exception as e:
            await log_failure(db, "export_build_pdf", e, project_id=project_id, user_id=user["id"],
                               context={"project_type": project_type, "part": "cover", "platform": p["platform"],
                                        "from_pieces": from_pieces})
            raise HTTPException(500, f"Export failed while building the cover PDF: {e}")
        finally:
            if assembled_path:
                try: os.remove(assembled_path)
                except OSError: pass

    if needs_interior:
        interior_path = UPLOAD_DIR / interior_data["stored_filename"]
        interior_ext = Path(interior_path).suffix.lower()
        interior_export_path = EXPORT_DIR / f"{project_id}_interior_{export_id}.pdf"
        try:
            if interior_ext == ".pdf":
                # Multi-page interior branch: source is a PDF → preserve vector text + fonts, tag PDF/X-1a
                interior_result = await run_with_timeout(
                    build_interior_pdf_x1a,
                    str(interior_path), str(interior_export_path),
                    trim_w=trim["w"], trim_h=trim["h"],
                    bleed=plat["bleed"],
                    title=p["name"],
                    author=(user.get("name") or ""),
                    color_profile=color_profile,
                    producer_name=producer_name,
                    grayscale=p.get("paper_type", "white_50lb") in BLACK_AND_WHITE_PAPERS,
                )
            else:
                # Image-source interior (e.g. a single scanned page) → rasterized single-page flow
                interior_result = await run_with_timeout(
                    build_print_ready_pdf,
                    str(interior_path), str(interior_export_path),
                    trim_w=trim["w"], trim_h=trim["h"],
                    bleed=plat["bleed"], spine_w=0,
                    is_cover=False, title=p["name"],
                    author=(user.get("name") or ""),
                    color_profile=color_profile,
                    producer_name=producer_name,
                )
        except HTTPException:
            raise
        except Exception as e:
            await log_failure(db, "export_build_pdf", e, project_id=project_id, user_id=user["id"],
                               context={"project_type": project_type, "part": "interior", "platform": p["platform"], "file_ext": interior_ext})
            raise HTTPException(500, f"Export failed while building the interior PDF: {e}")

    if needs_case:
        case_path = UPLOAD_DIR / case_data["stored_filename"]
        case_export_path = EXPORT_DIR / f"{project_id}_case_{export_id}.pdf"
        case_bleed = resolve_binding_spec("hardcover_case", platform_key)["bleed"]
        case_spine_w = calculate_spine_width_for_platform(p.get("page_count", 0), paper_ppi(paper, platform_key), platform_key, "hardcover_case")[0]
        if p.get("spine_width_override"):
            case_spine_w = float(p["spine_width_override"])
        try:
            case_result = await run_with_timeout(
                build_print_ready_pdf,
                str(case_path), str(case_export_path),
                trim_w=trim["w"], trim_h=trim["h"],
                bleed=case_bleed, spine_w=case_spine_w,
                is_cover=True, title=p["name"],
                author=(user.get("name") or ""),
                barcode_png_bytes=barcode_png,  # IngramSpark's Case Laminate template carries a barcode block on the case back too
                color_profile=color_profile,
                producer_name=producer_name,
                binding="hardcover_case",
                platform=platform_key,
            )
        except HTTPException:
            raise
        except Exception as e:
            await log_failure(db, "export_build_pdf", e, project_id=project_id, user_id=user["id"],
                               context={"project_type": project_type, "part": "case_wrap", "platform": p["platform"]})
            raise HTTPException(500, f"Export failed while building the case PDF: {e}")

    # Distributors require the cover, interior, and (for a dust-jacket book)
    # the plain case as separate file uploads, never merged into one PDF --
    # so whenever more than one of these was actually built, they're zipped
    # together into a single download rather than only ever returning one
    # file. A cover-only or interior-only project (the common case) still
    # produces one plain PDF, not a zip -- no reason to force that on the
    # simple path.
    parts = [
        (EXPORT_DIR / f"{project_id}_cover_{export_id}.pdf", f"{title}_cover.pdf", "cover", cover_result),
        (EXPORT_DIR / f"{project_id}_case_{export_id}.pdf", f"{title}_case.pdf", "case", case_result),
        (EXPORT_DIR / f"{project_id}_interior_{export_id}.pdf", f"{title}_interior.pdf", "interior", interior_result),
    ]
    parts = [part for part in parts if part[3] is not None]
    if len(parts) > 1:
        import zipfile
        export_name = f"{project_id}_export_{export_id}.zip"
        export_path = EXPORT_DIR / export_name
        with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src_path, arcname, _key, _res in parts:
                zf.write(src_path, arcname=arcname)
        result = {"bundled": True, **{key: res for _src, _arc, key, res in parts}}
    else:
        src_path, _arcname, _key, res = parts[0]
        export_name = src_path.name
        export_path = src_path
        result = res

    # Every finished book gets a report (owner's rule). Its seal is earned, never assumed: SparkPrep
    # re-checks the FINAL files themselves -- every interior page up to 300 -- and only when everything
    # is clear does the book get "The SparkPrep Certified Complete Publisher Preflight Report".
    # Anything else gets the plainer "SparkPrep Preflight Report", which says what's still open.
    import zipfile
    cert = await run_with_timeout(
        _certify_final_files, p, parts, plat, platform_key, trim, all_compliance, spine_w, export_id)
    report_path = EXPORT_DIR / f"{project_id}_report_{export_id}.pdf"
    generate_preflight_report_pdf(
        cert=cert, repair_log=p.get("repair_log") or [],
        project_meta={"title": title, "platform": plat.get("name", platform_key),
                      "trim_size": trim.get("label", p["trim_size"]),
                      "binding": BINDING_TYPES.get(binding, {}).get("label", binding),
                      "paper": PAPER_TYPES.get(p["paper_type"], {}).get("label", p["paper_type"]),
                      "page_count": p.get("page_count"), "spine_width": spine_w if needs_cover else None},
        output_path=str(report_path),
    )
    report_arcname = f"{title}_{'SparkPrep_Certified_Report' if cert['certified'] else 'SparkPrep_Preflight_Report'}.pdf"
    if export_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(export_path, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.write(report_path, arcname=report_arcname)
    else:
        bundled_name = f"{project_id}_export_{export_id}.zip"
        bundled_path = EXPORT_DIR / bundled_name
        with zipfile.ZipFile(bundled_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(export_path, arcname=f"{title}{export_path.suffix}")
            zf.write(report_path, arcname=report_arcname)
        export_path.unlink()
        export_name, export_path = bundled_name, bundled_path
        result = {"bundled": True, **result}
    report_path.unlink()
    result = {**result, "certified": cert["certified"], "certificate_id": cert.get("certificate_id"),
              "report_name": cert["report_name"], "found_and_fixed_report": bool(p.get("repair_log")),
              "still_open": [o["title"] for o in cert["open"]]}
    if not cert["certified"]:
        await _record_unsolved_case(project_id, user["id"], "export", [k for k, v in (p.get("slots") or {}).items() if v],
                                    cert["open"])
    if cert["certified"]:
        # Kept so a certificate can be looked up later (the seal is only worth something if it can be verified).
        await db.certificates.insert_one({
            "certificate_id": cert["certificate_id"], "project_id": project_id, "user_id": user["id"],
            "title": title, "platform": plat.get("name", platform_key), "trim_size": p["trim_size"],
            "binding": binding, "files": cert["files"], "checked_at": cert["checked_at"],
        })
    _prune_old_exports(project_id, keep=KEEP_EXPORTS_PER_PROJECT)

    # Increment usage -- always against the billing account, not necessarily
    # the acting user, so a team's shared pool is debited correctly.
    inc_fields = {"exports_this_month": 1}
    if is_new_book:
        inc_fields["books_this_month"] = 1
    project_update = {"$inc": {"exports_used": 1}}
    if is_new_book:
        project_update["$set"] = {"first_exported_at": datetime.now(timezone.utc).isoformat()}
    await db.projects.update_one({"_id": ObjectId(project_id)}, project_update)
    await db.users.update_one(
        {"_id": ObjectId(billing_user["id"])},
        {"$inc": inc_fields},
    )
    new_used = used + 1
    new_books_used = books_used + (1 if is_new_book else 0)
    new_book_exports_used = book_exports_used + 1
    # Record export
    await db.exports.insert_one({
        "project_id": project_id,
        "user_id": user["id"],
        "billing_user_id": billing_user["id"],
        "export_name": export_name,
        "result": result,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "export_name": export_name,
        "download_url": f"/api/projects/{project_id}/download/{export_name}",
        "files": _export_files(project_id, export_name, export_path, title, binding, plat.get("name", platform_key)),
        "exports_this_month": new_used,
        "exports_limit": export_limit,
        "books_this_month": new_books_used,
        "books_limit": books_limit,
        "counted_as_new_book": is_new_book,
        "book_exports_used": new_book_exports_used,
        "book_exports_limit": EXPORTS_PER_BOOK,
        **result,
    }


@api_router.post("/projects/{project_id}/export")
async def export_project(project_id: str, user: dict = Depends(get_current_user)):
    return await _export_project_core(project_id, user)


# Export as a background job. One export (building the print files + checking every page for SparkPrep
# Certified) can take longer than the ~100 s the network in front of Render allows a single request -- a long book
# then got cut off and the customer received nothing. The page starts a job, checks on it every few seconds, and
# downloads when it's done. Jobs live in memory (one server instance); a restart just means "export again".
_EXPORT_JOBS: dict = {}
_EXPORT_JOB_TTL_S = 2 * 60 * 60


@api_router.post("/projects/{project_id}/export-jobs")
async def start_export_job(project_id: str, user: dict = Depends(get_current_user)):
    now = time.time()
    for jid in [j for j, v in _EXPORT_JOBS.items() if now - v["started"] > _EXPORT_JOB_TTL_S]:
        _EXPORT_JOBS.pop(jid, None)
    running = next((j for j, v in _EXPORT_JOBS.items()
                    if v["project_id"] == project_id and v["user_id"] == user["id"] and v["status"] == "running"), None)
    if running:                                   # a double click joins the export already under way
        return {"job_id": running}
    job_id = uuid.uuid4().hex
    job = {"project_id": project_id, "user_id": user["id"], "status": "running", "started": now}
    _EXPORT_JOBS[job_id] = job

    async def run():
        try:
            job["result"] = await _export_project_core(project_id, user)
            job["status"] = "done"
        except HTTPException as e:
            job.update(status="error", status_code=e.status_code, detail=e.detail)
        except Exception as e:                    # noqa: BLE001
            await log_failure(db, "export_job", e, project_id=project_id, user_id=user["id"])
            job.update(status="error", status_code=500, detail=f"Export failed: {e}")

    task = asyncio.create_task(run())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return {"job_id": job_id}


@api_router.get("/export-jobs/{job_id}")
async def export_job_status(job_id: str, user: dict = Depends(get_current_user)):
    job = _EXPORT_JOBS.get(job_id)
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "That export isn't running any more -- please press Export again.")
    out = {"status": job["status"], "seconds": round(time.time() - job["started"])}
    if job["status"] == "done":
        out["result"] = job["result"]
    elif job["status"] == "error":
        out.update(status_code=job["status_code"], detail=job["detail"])
    return out


# Each finished file is its own download (owner, 2026-10-05: "I can't find the downloaded files… I need them to be
# easy to find because SparkPrep isn't just for me"). A ZIP hid them -- a distributor's upload box can't see inside
# one. The ZIP stays (one bundle, same storage); each file inside it is also served alone under a plain name.
def _export_file_kind(member: str):
    if member.endswith("Report.pdf"):
        return "report"
    for kind in ("interior", "case", "cover"):
        if member.endswith(f"_{kind}.pdf"):
            return kind
    return None


def _export_files(project_id: str, export_name: str, export_path, title: str, binding: str, platform_name: str) -> list:
    import zipfile
    cover_name = {"hardcover_jacket": "Dust Jacket", "hardcover_case": "Case Cover"}.get(binding, "Cover")
    info = {
        "interior": ("Interior", f"Upload where {platform_name} asks for your interior (text) file."),
        "cover": (cover_name, f"Upload where {platform_name} asks for your cover file."),
        "case": ("Case Cover", f"The hard cover under the jacket. Upload where {platform_name} asks for your case cover file."),
        "report": ("SparkPrep Report", "Your SparkPrep report. Keep it for your records -- it isn't uploaded anywhere."),
    }
    order = {"interior": 0, "cover": 1, "case": 2, "report": 3}
    try:
        with zipfile.ZipFile(export_path) as zf:
            members = zf.namelist()
    except Exception:
        return []
    safe_title = re.sub(r'[\\/:*?"<>|]', "", title).strip() or "My book"
    files = []
    for i, member in enumerate(members):
        kind = _export_file_kind(member)
        if not kind:
            continue
        label, where = info[kind]
        if kind == "report" and "Certified" in member:
            label = "SparkPrep Certified Report"
        files.append({"kind": kind, "label": label, "where": where, "filename": f"{safe_title} - {label}.pdf",
                      "download_url": f"/api/projects/{project_id}/download/{export_name}/{i}"})
    return sorted(files, key=lambda f: order[f["kind"]])


@api_router.get("/projects/{project_id}/download/{export_name}/{member_index}")
async def download_export_file(project_id: str, export_name: str, member_index: int, request: Request):
    """One file out of an export, under its plain name -- same sign-in and ownership checks as the whole bundle."""
    import urllib.parse
    import zipfile
    fp = await _owned_export_path(project_id, export_name, request)
    if fp.suffix.lower() != ".zip":
        raise HTTPException(404, "Export not found")
    with zipfile.ZipFile(fp) as zf:
        members = zf.namelist()
        if member_index < 0 or member_index >= len(members) or not _export_file_kind(members[member_index]):
            raise HTTPException(404, "File not found")
        data = zf.read(members[member_index])
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(members[member_index])})


async def _owned_export_path(project_id: str, export_name: str, request: Request):
    token = request.cookies.get("access_token") or request.query_params.get("token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        user_id = payload["sub"]
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    if not export_name.startswith(project_id) or "/" in export_name or "\\" in export_name or ".." in export_name:
        raise HTTPException(403, "Forbidden")
    project = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user_id})
    if not project:
        raise HTTPException(403, "Forbidden")
    fp = EXPORT_DIR / export_name
    if not fp.exists():
        raise HTTPException(404, "Export not found")
    return fp


@api_router.get("/projects/{project_id}/download/{export_name}")
async def download_export(project_id: str, export_name: str, request: Request):
    token = request.cookies.get("access_token") or request.query_params.get("token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        user_id = payload["sub"]
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    if not export_name.startswith(project_id):
        raise HTTPException(403, "Forbidden")
    # Verify the authenticated user actually owns this project -- without
    # this, any logged-in user who knew or guessed another user's
    # project_id + export filename could download their file, since the
    # startswith check above only confirms filename shape, not ownership.
    project = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user_id})
    if not project:
        raise HTTPException(403, "Forbidden")
    fp = EXPORT_DIR / export_name
    if not fp.exists():
        raise HTTPException(404, "Export not found")
    # A combined (cover + interior) project's export is a .zip bundling both
    # PDFs -- everything else is still a single .pdf, same as before.
    media_type = "application/zip" if fp.suffix.lower() == ".zip" else "application/pdf"
    return FileResponse(str(fp), media_type=media_type, filename=f"sparkprep_{export_name}")


# ---- Batch audit + batch export (Publisher/Studio -- see TIERS[*]["batch_enabled"]) ----
BATCH_MAX_PROJECTS = 25


class BatchIn(BaseModel):
    project_ids: List[str] = Field(min_length=1, max_length=BATCH_MAX_PROJECTS)


async def _require_batch_enabled(user: dict) -> dict:
    billing_user = await get_billing_user(user)
    tier = billing_user.get("tier", "free")
    if not TIERS.get(tier, TIERS["free"]).get("batch_enabled"):
        raise HTTPException(402, _soon_msg("Bulk audit + batch export is a Publisher/Studio plan feature.", "Bulk audit and batch export"))
    return billing_user


@api_router.post("/projects/batch-audit")
async def batch_audit(payload: BatchIn, user: dict = Depends(get_current_user)):
    """Runs the same compliance + PDF structure checks as a normal upload,
    across many of the caller's projects in one call -- the 'bulk audit'
    half of the Publisher/Studio 'Bulk audit + batch export' feature."""
    await _require_batch_enabled(user)
    results = []
    for pid in payload.project_ids:
        try:
            p = await db.projects.find_one({"_id": ObjectId(pid), "user_id": user["id"]})
        except Exception:
            p = None
        if not p:
            results.append({"project_id": pid, "ok": False, "error": "Project not found"})
            continue
        # Checks whichever of cover/interior this project type actually has,
        # via slots first and the legacy uploaded_file field as a fallback --
        # this used to only ever look at uploaded_file, which a slot upload
        # never sets, so every interior-only or combined project batch-audited
        # always came back "No uploaded file" regardless of what was uploaded.
        project_type = p.get("project_type", "cover")
        trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
        plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
        compliance = []
        structure = []
        if project_type in ("cover", "combined"):
            cover_meta = _cover_file_meta(p)
            if cover_meta:
                # _cover_file_meta resolves full_wrap, then front_cover, then
                # the legacy field (itself always a full-wrap upload) -- mirror
                # that same precedence here so the size check matches whichever
                # slot actually got used.
                cover_slot = "full_wrap" if (p.get("slots") or {}).get("full_wrap") or not (p.get("slots") or {}).get("front_cover") else "front_cover"
                cover_path = UPLOAD_DIR / cover_meta["stored_filename"]
                final_w, final_h = _target_inches_for_slot(p, cover_slot)
                spine_kwargs = {}
                if cover_slot == "full_wrap":
                    geom = _full_wrap_geometry(p)
                    spine_kwargs = {"spine_x_in": geom["spine_x"], "spine_w_in": geom["spine_width"],
                                     "page_count": p.get("page_count"), "binding": p.get("binding", "paperback")}
                compliance += run_compliance_checks(
                    cover_meta, trim["w"], trim["h"], plat["bleed"], p["platform"],
                    file_path=str(cover_path) if cover_path.exists() else None, slot=cover_slot,
                    platform_name=plat.get("name"), final_w=final_w, final_h=final_h, **spine_kwargs,
                )
                if cover_meta.get("is_pdf") and cover_path.exists():
                    structure += run_pdf_structure_audit(str(cover_path), plat.get("name", "your distributor"), max_pages=BASIC_CHECK_MAX_PAGES)
        if project_type in ("interior", "combined"):
            interior_meta = _interior_file_meta(p)
            if interior_meta:
                interior_path = UPLOAD_DIR / interior_meta["stored_filename"]
                compliance += run_compliance_checks(
                    interior_meta, trim["w"], trim["h"], plat["bleed"], p["platform"],
                    file_path=str(interior_path), slot="interior", platform_name=plat.get("name"), max_pages=BASIC_CHECK_MAX_PAGES,
                )
                if interior_meta.get("is_pdf") and interior_path.exists():
                    structure += run_pdf_structure_audit(str(interior_path), plat.get("name", "your distributor"), max_pages=BASIC_CHECK_MAX_PAGES)
        if not compliance and not structure:
            results.append({"project_id": pid, "ok": False, "error": "No uploaded file"})
            continue
        fails = sum(1 for c in compliance if c.get("status") == "fail") + sum(1 for f in structure if f.get("severity") == "fail")
        warnings = sum(1 for c in compliance if c.get("status") == "warning") + sum(1 for f in structure if f.get("severity") == "warning")
        unlocked = await _results_unlocked(p, user)
        results.append({
            "project_id": pid, "ok": True, "name": p.get("name"),
            "critical_failures": fails, "warnings": warnings, "results_locked": not unlocked,
            "compliance": compliance if unlocked else [], "structure_findings": structure if unlocked else [],
        })
    return {"results": results}


@api_router.post("/projects/batch-export")
async def batch_export(payload: BatchIn, user: dict = Depends(get_current_user)):
    """Exports many of the caller's projects in one call and hands back a
    single zip -- the 'batch export' half of the Publisher/Studio feature.
    Each project's own per-book/monthly limits still apply; a project that
    fails its own export (limit reached, no file, etc.) is reported in
    `results` rather than aborting the whole batch."""
    await _require_batch_enabled(user)
    if len(payload.project_ids) < 2:
        raise HTTPException(400, "Batch export needs at least 2 projects — export a single book from its editor instead.")

    results = []
    exported_names = []
    for pid in payload.project_ids:
        try:
            r = await _export_project_core(pid, user)
            results.append({"project_id": pid, "ok": True, **r})
            exported_names.append(r["export_name"])
        except HTTPException as e:
            results.append({"project_id": pid, "ok": False, "error": e.detail})

    if not exported_names:
        return {"results": results, "zip_name": None, "download_url": None}

    import zipfile
    zip_name = f"batch_{user['id']}_{uuid.uuid4().hex[:8]}.zip"
    zip_path = EXPORT_DIR / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in exported_names:
            fp = EXPORT_DIR / name
            if fp.exists():
                zf.write(fp, arcname=name)

    return {
        "results": results,
        "zip_name": zip_name,
        "download_url": f"/api/exports/batch/{zip_name}",
    }


@api_router.get("/exports/batch/{zip_name}")
async def download_batch_export(zip_name: str, request: Request):
    token = request.cookies.get("access_token") or request.query_params.get("token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        user_id = payload["sub"]
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    # Batch zip filenames are namespaced with the owning user's id
    # (batch_{user_id}_{random}.zip) -- this is the ownership check,
    # same pattern as the project_id prefix check on single-file downloads.
    if not zip_name.startswith(f"batch_{user_id}_"):
        raise HTTPException(403, "Forbidden")
    fp = EXPORT_DIR / zip_name
    if not fp.exists():
        raise HTTPException(404, "Batch export not found")
    return FileResponse(str(fp), media_type="application/zip", filename=f"sparkprep_{zip_name}")


# ---- Advanced Interior Check (one-time paid add-on, distinct from the
# $0.99 anonymous Print Failure Audit) ----
ADVANCED_INTERIOR_PRICE_CENTS = {
    "free": 4999,
    "author": 3999,
    "creator_pro": 3499,
    "publisher": 2999,
    "studio": 2999,
}
ADVANCED_INTERIOR_MAX_PAGES = 300
# Customer-facing limit: how many times the customer can SUBMIT the
# Advanced workflow (scan -> repair -> recheck) for one purchase. Distinct
# from ADVANCED_MAX_INTERNAL_ITERATIONS below, which bounds how many
# repair/recheck passes happen INSIDE one submitted run -- those internal
# passes are invisible to this counter.
ADVANCED_MAX_RUNS = 3
ADVANCED_MAX_INTERNAL_ITERATIONS = 3
# Findings the Advanced repair loop actually knows how to fix, split by
# which repair function handles them -- same underlying fixers autofix()
# uses for the Basic check, just applied here across up to 300 pages
# instead of 1, and looped instead of a single pass.
ADVANCED_GEOMETRY_FIXABLE_IDS = {"interior_page_size_mismatch", "interior_safety_margin"}
ADVANCED_GHOSTSCRIPT_FIXABLE_IDS = {"pdfx1a_not_declared", "live_transparency_detected", "layers_detected", "pdfx1a_missing_output_intent"}
# Ghostscript's -dPDFX pass doesn't reliably stamp /GTS_PDFXVersion in
# practice (confirmed directly: converting a real file through
# convert_to_pdfx1a() and rechecking it still reports pdfx1a_not_declared)
# -- but this doesn't actually matter, because build_interior_pdf_x1a()/
# build_print_ready_pdf() unconditionally (re)stamp both of these at
# export time regardless of what happened here (see autofix()'s identical
# ALWAYS_FIXED_AT_EXPORT set). Without this exemption the loop burned all
# ADVANCED_MAX_INTERNAL_ITERATIONS passes re-attempting a "fix" that Ghostscript
# was never going to make stick, and then told the paying customer these
# were unresolved problems they needed to act on, when they're not.
ADVANCED_ALWAYS_FIXED_AT_EXPORT = {"pdfx1a_not_declared", "pdfx1a_missing_output_intent"}


async def _advanced_interior_repair_loop(
    project_id: str, p: dict, file_path: Path, original_path: Path,
    trim_w: float, trim_h: float, platform_name: str, user_id: str,
) -> dict:
    """Bounded internal repair loop for ONE submitted Advanced Interior
    Check run: scan (up to ADVANCED_INTERIOR_MAX_PAGES pages) -> apply
    whatever's safely fixable -> rescan -> repeat, until nothing fixable
    remains or ADVANCED_MAX_INTERNAL_ITERATIONS is hit. This entire loop,
    however many passes it takes, counts as exactly one submitted run --
    the customer-facing ADVANCED_MAX_RUNS limit is enforced by the caller,
    not in here.

    Never deletes original_path (same guarantee as autofix() and
    _replace_slot() -- see their original_stored_filename handling); an
    intermediate file from an earlier iteration in THIS loop that a later
    iteration supersedes is disposable and does get cleaned up, exactly
    like autofix()'s own multi-step repair chain within a single call.
    """
    fixed_log = []
    iterations_used = 0
    current_path = Path(file_path)

    for iteration in range(1, ADVANCED_MAX_INTERNAL_ITERATIONS + 1):
        findings = run_pdf_structure_audit(str(current_path), platform_name, max_pages=ADVANCED_INTERIOR_MAX_PAGES)
        findings += check_interior_safety_margins(str(current_path), platform_name, trim_w, trim_h, max_pages=ADVANCED_INTERIOR_MAX_PAGES)

        geometry_findings = [f for f in findings if f["id"] in ADVANCED_GEOMETRY_FIXABLE_IDS]
        ghostscript_findings = [f for f in findings if f["id"] in ADVANCED_GHOSTSCRIPT_FIXABLE_IDS]
        # Only live_transparency_detected/layers_detected genuinely need
        # (and benefit from) another Ghostscript pass -- see
        # ADVANCED_ALWAYS_FIXED_AT_EXPORT above for why the other two
        # ghostscript-fixable ids don't count toward "is there more to do".
        genuinely_needs_gs = [f for f in ghostscript_findings if f["id"] not in ADVANCED_ALWAYS_FIXED_AT_EXPORT]
        if not geometry_findings and not genuinely_needs_gs:
            break  # nothing left that this loop knows how to fix (or that export doesn't fix anyway)

        iterations_used = iteration
        made_progress = False

        if geometry_findings:
            try:
                geom_fixed_path = UPLOAD_DIR / f"{project_id}_interior_adv_geom_{uuid.uuid4().hex[:6]}.pdf"
                autofix_interior_safety_margins(str(current_path), str(geom_fixed_path), trim_w, trim_h)
                if current_path != Path(original_path):
                    try: os.remove(current_path)
                    except OSError: pass
                current_path = geom_fixed_path
                fixed_log.append({
                    "iteration": iteration, "ids": [f["id"] for f in geometry_findings],
                    "action": "Resized and recentered interior pages onto the ordered trim size.",
                })
                made_progress = True
            except Exception as e:
                await log_failure(db, "advanced_interior_geometry_fix", e, project_id=project_id, user_id=user_id)

        if genuinely_needs_gs and find_ghostscript():
            try:
                gs_fixed_path = UPLOAD_DIR / f"{project_id}_interior_adv_gs_{uuid.uuid4().hex[:6]}.pdf"
                convert_to_pdfx1a(str(current_path), str(gs_fixed_path), title=p.get("name", "SparkPrep Export"))
                if current_path != Path(original_path):
                    try: os.remove(current_path)
                    except OSError: pass
                current_path = gs_fixed_path
                fixed_log.append({
                    "iteration": iteration, "ids": [f["id"] for f in ghostscript_findings],
                    "action": "Declared PDF/X-1a and flattened live transparency/layers.",
                })
                made_progress = True
            except Exception as e:
                await log_failure(db, "advanced_interior_ghostscript_fix", e, project_id=project_id, user_id=user_id)

        if not made_progress:
            break  # every repair attempt this pass failed -- stop rather than spin

    final_findings = run_pdf_structure_audit(str(current_path), platform_name, max_pages=ADVANCED_INTERIOR_MAX_PAGES)
    final_findings += check_interior_safety_margins(str(current_path), platform_name, trim_w, trim_h, max_pages=ADVANCED_INTERIOR_MAX_PAGES)
    final_findings = _annotate_export_time_fixes(final_findings)
    # Excluded outright, not just annotated: these two don't actually block
    # or survive export (see ADVANCED_ALWAYS_FIXED_AT_EXPORT above), so
    # showing them to a customer who just paid for "tell me exactly what I
    # need to fix myself" as something needing their attention would be
    # actively wrong, not just unhelpful.
    unresolved = [
        f for f in final_findings
        if f["severity"] in ("fail", "warning") and f["id"] not in ADVANCED_ALWAYS_FIXED_AT_EXPORT
    ]

    return {
        "final_path": current_path,
        "iterations_used": iterations_used,
        "fixed": fixed_log,
        "unresolved": unresolved,
    }


class InteriorCheckCheckoutIn(BaseModel):
    origin_url: str


@api_router.post("/projects/{project_id}/interior-check/checkout")
async def interior_check_checkout(project_id: str, payload: InteriorCheckCheckoutIn, user: dict = Depends(get_current_user)):
    if book_pass.book_pass_on():
        raise HTTPException(410, "The SparkPrep Standard Check of every page is included with every book. Start your book to use it.")
    """One-time purchase for a full structural interior check, up to
    ADVANCED_INTERIOR_MAX_PAGES pages. Priced by the buyer's current
    subscription tier. This is a one-time purchase, not a lifetime license --
    it unlocks full findings for this project's current interior file only,
    and is intentionally separate from the $0.99 anonymous audit flow.
    """
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    # "combined" is allowed too -- the editor already shows this upsell card
    # on combined projects (it has an interior section same as interior-only),
    # but this endpoint used to hard-reject them with a 400 the moment
    # someone actually clicked buy.
    if p.get("project_type") not in ("interior", "combined"):
        raise HTTPException(400, "Advanced Interior Check only applies to projects with an interior")
    # Used to check only the legacy uploaded_file field, which a slot upload
    # never sets -- this 404'd for every interior file uploaded through the
    # real editor UI (slots.interior), even though one was clearly there.
    if not _interior_file_meta(p):
        raise HTTPException(404, "No interior file uploaded yet")
    if (p.get("page_count") or 0) > ADVANCED_INTERIOR_MAX_PAGES:
        raise HTTPException(400, f"Advanced Interior Check supports up to {ADVANCED_INTERIOR_MAX_PAGES} pages")

    billing_user = await get_billing_user(user)
    tier = billing_user.get("tier", "free")
    price_cents = ADVANCED_INTERIOR_PRICE_CENTS.get(tier, ADVANCED_INTERIOR_PRICE_CENTS["free"])
    if not stripe.api_key or stripe.api_key in ("sk_test_not_configured", ""):
        raise HTTPException(503, "Payments not configured — Stripe key missing")
    origin = payload.origin_url.rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "SparkPrep Advanced Interior Check",
                        "description": f"Full structural interior check, up to {ADVANCED_INTERIOR_MAX_PAGES} pages. One-time purchase, not a lifetime license.",
                    },
                    "unit_amount": price_cents,
                },
                "quantity": 1,
            }],
            success_url=f"{origin}/editor/{project_id}?interior_check_session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/editor/{project_id}",
            metadata={"project_id": project_id, "user_id": user["id"], "purpose": "advanced_interior_check"},
        )
    except stripe.error.StripeError as e:
        raise HTTPException(500, f"Stripe error: {e}")

    await db.interior_checks.insert_one({
        "project_id": project_id,
        "user_id": user["id"],
        "session_id": session.id,
        "price_cents": price_cents,
        "tier_at_purchase": tier,
        "paid": False,
        "runs_used": 0,
        "last_run": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    await db.payment_transactions.insert_one({
        "session_id": session.id,
        "project_id": project_id,
        "user_id": user["id"],
        "amount": price_cents,
        "currency": "usd",
        "status": "initiated",
        "payment_status": "pending",
        "product": "advanced_interior_check",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"checkout_url": session.url, "session_id": session.id}


def _interior_check_stale(check: dict, interior_meta: Optional[dict]) -> bool:
    """True only when the customer has uploaded a genuinely NEW interior
    (a different original_stored_filename lineage) since the last submitted
    run -- NOT simply because the run itself changed the file (repairing it
    is supposed to change stored_filename; that's not staleness, that's the
    record of what the run did)."""
    last_run = (check or {}).get("last_run")
    if not last_run or not interior_meta:
        return False
    checked_original = (last_run.get("file_checked") or {}).get("original_stored_filename")
    current_original = interior_meta.get("original_stored_filename")
    return bool(checked_original and current_original and checked_original != current_original)


@api_router.get("/projects/{project_id}/interior-check/status")
async def interior_check_status(project_id: str, user: dict = Depends(get_current_user)):
    """Lets the UI show where things stand on a page reload/revisit -- returns
    the STORED result of the last submitted run, never re-scans live (a page
    reload must never itself consume one of the customer's 3 runs, and the
    result shown must be traceable to a specific file, not silently
    recomputed against whatever the file happens to be right now)."""
    check = await db.interior_checks.find_one(
        {"project_id": project_id, "user_id": user["id"], "paid": True},
        sort=[("created_at", -1)],
    )
    if not check and book_pass.book_pass_on() and await book_pass.entitlements.project_window(db, user["id"], project_id):
        return {"paid": True, "check_type": "advanced", "runs_used": 0, "max_runs": ADVANCED_MAX_RUNS,
                "last_run": None, "is_stale": False, "included_with_book": True}
    if not check:
        return {"paid": False}
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    interior_meta = _interior_file_meta(p) if p else None
    return {
        "paid": True, "check_type": "advanced",
        "runs_used": check.get("runs_used", 0), "max_runs": ADVANCED_MAX_RUNS,
        "last_run": check.get("last_run"),
        "is_stale": _interior_check_stale(check, interior_meta),
    }


@api_router.get("/projects/{project_id}/interior-check/verify")
async def interior_check_verify(project_id: str, session_id: str, user: dict = Depends(get_current_user)):
    """Confirms a just-completed Stripe purchase and returns the same
    stored-state shape as /status -- paying does NOT itself run the check;
    the customer explicitly submits their first run afterward via
    POST .../interior-check/run."""
    check = await db.interior_checks.find_one({"project_id": project_id, "session_id": session_id, "user_id": user["id"]})
    if not check:
        raise HTTPException(404, "Interior check purchase not found")
    if not check.get("paid"):
        try:
            session = stripe.checkout.Session.retrieve(session_id)
        except stripe.error.StripeError as e:
            raise HTTPException(500, f"Stripe error: {e}")
        if session.payment_status == "paid":
            await db.interior_checks.update_one({"_id": check["_id"]}, {"$set": {"paid": True}})
            check["paid"] = True
    if not check.get("paid"):
        return {"paid": False}
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    interior_meta = _interior_file_meta(p) if p else None
    return {
        "paid": True, "check_type": "advanced",
        "runs_used": check.get("runs_used", 0), "max_runs": ADVANCED_MAX_RUNS,
        "last_run": check.get("last_run"),
        "is_stale": _interior_check_stale(check, interior_meta),
    }


@api_router.post("/projects/{project_id}/interior-check/run")
async def interior_check_run(project_id: str, user: dict = Depends(get_current_user)):
    """Submits ONE Advanced Interior Check run: scans up to
    ADVANCED_INTERIOR_MAX_PAGES pages, repairs whatever SparkPrep can
    safely fix via a bounded internal loop, rechecks, and returns exactly
    what got fixed vs. what still needs the customer's own action --
    with the exact page, the problem, the requirement, and plain-English
    fix steps for anything left over (the same finding shape every other
    check in this app already produces). Counts one against the
    purchase's ADVANCED_MAX_RUNS submitted-run limit; the internal
    repair/recheck passes inside this one call do not."""
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    if p.get("project_type") not in ("interior", "combined"):
        raise HTTPException(400, "The SparkPrep Standard Check is for a book's interior -- this project doesn't have one.")
    interior_meta = _interior_file_meta(p)
    if not interior_meta or not interior_meta.get("stored_filename"):
        raise HTTPException(404, "No interior file uploaded yet")
    if not interior_meta.get("is_pdf"):
        raise HTTPException(400, "The SparkPrep Standard Check needs a PDF interior.")

    if book_pass.book_pass_on() and not is_admin_user(user):
        window = await book_pass.entitlements.project_window(db, user["id"], project_id)
        if not window:
            raise HTTPException(402, {"code": "book_required", "msg": "Start this book to use the SparkPrep Standard Check -- it's included."})
        if not await db.interior_checks.find_one({"project_id": project_id, "user_id": user["id"], "paid": True}):
            await db.interior_checks.insert_one({
                "project_id": project_id, "user_id": user["id"], "session_id": f"book:{window['credit_id']}",
                "price_cents": 0, "tier_at_purchase": "book", "paid": True, "runs_used": 0, "last_run": None,
                "created_at": datetime.now(timezone.utc).isoformat()})
    check = await db.interior_checks.find_one(
        {"project_id": project_id, "user_id": user["id"], "paid": True},
        sort=[("created_at", -1)],
    )
    if not check:
        raise HTTPException(402, "Purchase the Advanced Interior Check for this book first.")
    runs_used = check.get("runs_used", 0)
    if runs_used >= ADVANCED_MAX_RUNS:
        raise HTTPException(400, f"You've used all {ADVANCED_MAX_RUNS} SparkPrep Standard Check runs for this book. (Your export still checks every page automatically.)")

    file_path = UPLOAD_DIR / interior_meta["stored_filename"]
    if not file_path.exists():
        raise HTTPException(404, "Interior file missing on disk")
    original_stored_filename = interior_meta.get("original_stored_filename") or interior_meta["stored_filename"]
    original_path = UPLOAD_DIR / original_stored_filename

    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])

    started_at = datetime.now(timezone.utc).isoformat()
    result = await _advanced_interior_repair_loop(
        project_id, p, file_path, original_path,
        trim["w"], trim["h"], plat.get("name", "your distributor"), user["id"],
    )
    finished_at = datetime.now(timezone.utc).isoformat()

    final_path = Path(result["final_path"])
    final_stored_filename = final_path.name
    metadata = analyze_file(str(final_path))
    metadata["original_filename"] = interior_meta.get("original_filename")
    metadata["stored_filename"] = final_stored_filename
    metadata["slot"] = "interior"
    # This slot's regular (Basic-depth) compliance stays computed the usual
    # way -- Advanced's own 300-page findings live in last_run below, kept
    # deliberately separate so Basic/Final Review/Export don't silently
    # start reflecting a different check's scope than they always have.
    metadata["original_stored_filename"] = original_stored_filename
    compliance = run_compliance_checks(
        metadata, trim["w"], trim["h"], plat["bleed"], p["platform"],
        file_path=str(final_path), slot="interior", platform_name=plat.get("name"),
        max_pages=BASIC_CHECK_MAX_PAGES,
    )
    slots = p.get("slots") or {}
    slots["interior"] = {**metadata, "compliance": compliance}
    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"slots": slots, "updated_at": finished_at}},
    )

    run_number = runs_used + 1
    total_pages = metadata.get("pdf_pages") or 0
    last_run = {
        "run_number": run_number,
        "started_at": started_at,
        "finished_at": finished_at,
        "file_checked": {
            "stored_filename": final_stored_filename,
            "original_stored_filename": original_stored_filename,
            "total_pages": total_pages,
            "pages_checked": min(total_pages, ADVANCED_INTERIOR_MAX_PAGES),
        },
        "iterations_used": result["iterations_used"],
        "fixed": result["fixed"],
        "unresolved": result["unresolved"],
        "status": "clean" if not result["unresolved"] else "issues_remain",
    }
    await db.interior_checks.update_one(
        {"_id": check["_id"]},
        {"$set": {"last_run": last_run, "runs_used": run_number}},
    )

    return {
        "paid": True, "check_type": "advanced",
        "runs_used": run_number, "max_runs": ADVANCED_MAX_RUNS,
        "last_run": last_run, "is_stale": False,
    }


# ---- Promo codes (free giveaway access, no Stripe involved -- these never
# charge anything, so there's nothing for a payment processor to do) ----
# "full_access" and "interior_only_access" mark a specific project so every
# paid-tier gate that's actually scoped to a project (export limits, the
# Word/text-to-interior conversion, AI Cover, AI Upscale) treats it as
# unlocked, regardless of the account's real plan. AI Blurb isn't included
# in that list because it has no project_id at all in its request -- there's
# nothing to attach a per-project unlock to without a larger change to that
# endpoint. "advanced_interior_check" instead just writes a paid=True
# interior_checks record directly, so the existing status/verify endpoints
# pick it up with no further changes -- identical to a real Stripe purchase
# from their point of view.
PROMO_CODE_TYPES = {"full_access", "interior_only_access", "advanced_interior_check"}


class RedeemPromoIn(BaseModel):
    code: str


@api_router.post("/projects/{project_id}/redeem-promo")
async def redeem_promo_code(project_id: str, payload: RedeemPromoIn, user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")

    code = payload.code.strip().upper()
    promo = await db.promo_codes.find_one({"code": code})
    if not promo:
        raise HTTPException(404, "That code isn't valid.")
    if promo.get("used"):
        raise HTTPException(400, "That code has already been used.")

    promo_type = promo["type"]
    if promo_type == "interior_only_access" and p.get("project_type") != "interior":
        raise HTTPException(400, "This code only works on an interior-only project.")
    if promo_type == "advanced_interior_check":
        if p.get("project_type") not in ("interior", "combined"):
            raise HTTPException(400, "This code only applies to a project with an interior.")
        if not _interior_file_meta(p):
            raise HTTPException(404, "Upload your interior file first, then redeem this code.")
        if (p.get("page_count") or 0) > ADVANCED_INTERIOR_MAX_PAGES:
            raise HTTPException(400, f"Advanced Interior Check supports up to {ADVANCED_INTERIOR_MAX_PAGES} pages")

    await db.promo_codes.update_one(
        {"_id": promo["_id"]},
        {"$set": {
            "used": True, "used_by_user_id": user["id"], "used_on_project_id": project_id,
            "used_at": datetime.now(timezone.utc).isoformat(),
        }},
    )

    if promo_type == "advanced_interior_check":
        await db.interior_checks.insert_one({
            "project_id": project_id, "user_id": user["id"], "session_id": f"promo_{code}",
            "price_cents": 0, "tier_at_purchase": "promo", "paid": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"ok": True, "unlocked": "Advanced Interior Check — refresh to see full results."}

    await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": {"promo_access": promo_type}})
    label = "Full Access" if promo_type == "full_access" else "Interior-Only Access"
    return {"ok": True, "unlocked": f"{label} for this book — paid-tier features are now unlocked here."}


# ---- Stripe Payments ----
@api_router.post("/payments/checkout")
async def create_checkout(payload: CheckoutIn, user: dict = Depends(get_current_user)):
    if book_pass.book_pass_on():
        raise HTTPException(410, "Plans have changed. See the new pricing page.")
    tier = payload.tier
    if tier not in ("author", "creator_pro", "publisher", "studio"):
        raise HTTPException(400, "Invalid tier")
    tier_info = TIERS[tier]
    origin = payload.origin_url.rstrip("/")
    if not stripe.api_key or stripe.api_key in ("sk_test_not_configured", ""):
        raise HTTPException(
            status_code=503,
            detail="Payments not configured yet. Add a valid STRIPE_API_KEY to backend/.env to enable checkout.",
        )
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"SparkPrep {tier_info['name']} Plan"},
                    "unit_amount": tier_info["price_cents"],
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            success_url=f"{origin}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/payment/cancel",
            customer_email=user["email"],
            metadata={"user_id": user["id"], "tier": tier},
        )
    except stripe.error.StripeError as e:
        raise HTTPException(500, f"Stripe error: {str(e)}")

    await db.payment_transactions.insert_one({
        "session_id": session.id,
        "user_id": user["id"],
        "tier": tier,
        "amount": tier_info["price_cents"],
        "currency": "usd",
        "status": "initiated",
        "payment_status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"checkout_url": session.url, "session_id": session.id}


@api_router.get("/health")
async def health_check():
    # "status" stays "ok" for the host's health probe; "ocr" makes a silently
    # disabled cover text-margin check visible (see pdfx_validator.ocr_status).
    # "disk" closes a real blind spot: the persistent disk at DATA_DIR (every uploaded/exported
    # file lives there) has no visibility anywhere else -- Render's own metrics don't expose disk
    # usage, so a slowly-filling disk could go unnoticed until it's suddenly full and every upload
    # starts failing. Cheap (a single statvfs-style syscall), safe to compute on every health check.
    disk = None
    try:
        total, used, free = shutil.disk_usage(DATA_DIR)
        disk = {"total_gb": round(total / 1e9, 2), "used_gb": round(used / 1e9, 2), "free_gb": round(free / 1e9, 2),
                "used_pct": round(used / total * 100, 1)}
    except OSError:
        pass
    return {"status": "ok", "ocr": ocr_status(), "disk": disk}


@api_router.get("/season")
async def get_season():
    return audit_season_status()


@api_router.get("/payments/status/{session_id}")
async def payment_status(session_id: str):
    record = await db.payment_transactions.find_one({"session_id": session_id})
    if not record:
        raise HTTPException(404, "Transaction not found")
    if record.get("payment_status") != "paid" and record.get("product") in book_pass.NEW_PRODUCTS:
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            if s.payment_status == "paid" or s.status == "complete":
                await db.payment_transactions.update_one(          # safe to repeat: crediting below is idempotent
                    {"session_id": session_id},
                    {"$set": {"status": "completed", "payment_status": "paid",
                              "updated_at": datetime.now(timezone.utc).isoformat()}})
                record = await db.payment_transactions.find_one({"session_id": session_id})
                await book_pass.purchases.on_session_paid(db, stripe, record, s)
        except stripe.error.StripeError:
            pass
    elif record.get("payment_status") != "paid":
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            if s.payment_status == "paid" or s.status == "complete":
                await db.payment_transactions.update_one(
                    {"session_id": session_id, "payment_status": {"$ne": "paid"}},
                    {"$set": {
                        "status": "completed", "payment_status": "paid",
                        "stripe_subscription_id": s.subscription,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }},
                )
                # Upgrade user tier
                if record.get("user_id"):
                    await db.users.update_one(
                        {"_id": ObjectId(record["user_id"])},
                        {"$set": {
                            "tier": record["tier"],
                            "subscription_status": "active",
                            "stripe_subscription_id": s.subscription,
                        }},
                    )
                record = await db.payment_transactions.find_one({"session_id": session_id})
        except stripe.error.StripeError:
            pass
    return {
        "session_id": record["session_id"],
        "status": record["status"],
        "payment_status": record["payment_status"],
        "tier": record.get("tier"),
    }


@api_router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), secret)
        else:
            import json as _json
            event = _json.loads(payload)
    except Exception as e:
        raise HTTPException(400, f"Invalid webhook: {e}")

    t = event["type"] if isinstance(event, dict) else event.type
    obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
    obj_get = (lambda k: obj[k] if isinstance(obj, dict) else getattr(obj, k, None))

    now_iso = datetime.now(timezone.utc).isoformat()

    if t == "checkout.session.completed":
        session_id = obj_get("id")
        record = await db.payment_transactions.find_one({"session_id": session_id})
        if record and record.get("payment_status") != "paid":
            await db.payment_transactions.update_one(
                {"session_id": session_id},
                {"$set": {"status": "completed", "payment_status": "paid", "updated_at": now_iso}},
            )
            # Route based on product
            if record.get("product") in book_pass.NEW_PRODUCTS:
                await book_pass.purchases.on_session_paid(db, stripe, record, obj)
            elif record.get("product") == "audit_099" and record.get("audit_id"):
                await _mark_audit_paid(record["audit_id"], record.get("level"))
            elif record.get("product") == "advanced_interior_check":
                # Unlock from Stripe's own confirmation, not only when the customer's
                # browser returns to the success page (interior_check_verify) -- someone
                # who pays and closes the tab must not be charged and left locked out.
                await db.interior_checks.update_one(
                    {"session_id": session_id},
                    {"$set": {"paid": True, "paid_at": now_iso}},
                )
            elif record.get("user_id") and record.get("tier"):
                await db.users.update_one(
                    {"_id": ObjectId(record["user_id"])},
                    {"$set": {
                        "tier": record["tier"],
                        "subscription_status": "active",
                        "stripe_subscription_id": obj_get("subscription"),
                    }},
                )
    elif t == "invoice.paid":
        await book_pass.purchases.on_invoice_paid(db, stripe, obj)
    elif t == "checkout.session.expired":
        await book_pass.purchases.on_session_expired(db, obj)
    elif t == "customer.subscription.deleted":
        await book_pass.purchases.on_subscription_deleted(db, obj_get("id"))
        # Downgrade the user to free when their subscription cancels
        sub_id = obj_get("id")
        user_doc = await db.users.find_one({"stripe_subscription_id": sub_id})
        if user_doc:
            await db.users.update_one(
                {"_id": user_doc["_id"]},
                {"$set": {"tier": "free", "subscription_status": "canceled", "updated_at": now_iso}},
            )
    elif t == "invoice.payment_failed":
        sub_id = obj_get("subscription")
        if sub_id:
            await db.users.update_one(
                {"stripe_subscription_id": sub_id},
                {"$set": {"subscription_status": "past_due", "updated_at": now_iso}},
            )

    return {"status": "ok", "event_type": t}


# ---- Health ----
@api_router.get("/")
async def root():
    return {"service": "SparkPrep", "status": "ok"}


# ---- ISBN + Barcode ----
class ISBNIn(BaseModel):
    isbn: str


@api_router.post("/isbn/validate")
async def isbn_validate(payload: ISBNIn):
    info = normalize_isbn(payload.isbn)
    return info


@api_router.get("/isbn/barcode.png")
async def isbn_barcode_png(isbn: str):
    info = normalize_isbn(isbn)
    if not info["valid"]:
        raise HTTPException(400, info.get("error", "Invalid ISBN"))
    try:
        png = generate_barcode_png_bytes(info["isbn"])
    except Exception as e:
        raise HTTPException(500, f"Barcode generation failed: {e}")
    from fastapi.responses import Response
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


class ProjectISBN(BaseModel):
    isbn: str


@api_router.patch("/projects/{project_id}/isbn")
async def set_project_isbn(project_id: str, payload: ProjectISBN, user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    info = normalize_isbn(payload.isbn)
    if not info["valid"]:
        raise HTTPException(400, info.get("error", "Invalid ISBN"))
    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"isbn": info["isbn"], "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"isbn": info["isbn"]}


# ---- Manuscript templates ----
@api_router.get("/manuscript/templates")
async def manuscript_templates():
    return {"templates": list_manuscript_templates()}


@api_router.post("/manuscript/upload-docx")
async def manuscript_upload_docx(file: UploadFile = File(...), project_id: Optional[str] = Form(None), user: dict = Depends(get_current_user)):
    """Extracts text and chapter structure from an uploaded .docx manuscript
    so it can be composed into a print-ready interior -- this is the real
    'regular people write in Word' path, distinct from manually typing/
    pasting into the source_text field below.
    """
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(400, "Only .docx files are supported. (Older .doc files must be re-saved as .docx first -- Word does this automatically via File > Save As.)")
    try:
        contents = await file.read()
        result = extract_manuscript_text(io.BytesIO(contents))
        images = extract_embedded_images(contents)
    except Exception as e:
        raise HTTPException(400, f"Could not read this .docx file: {e}")

    saved_images = []
    image_dir = UPLOAD_DIR / (project_id or user["id"]) / "manuscript_images"
    if images:
        image_dir.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(images):
        stored_name = f"{uuid.uuid4().hex[:8]}_{img['filename']}"
        (image_dir / stored_name).write_bytes(img["data"])
        saved_images.append({
            "filename": img["filename"],
            "stored_name": stored_name,
            "content_type": img["content_type"],
            "size_bytes": len(img["data"]),
        })

    if project_id and saved_images:
        await db.projects.update_one(
            {"_id": ObjectId(project_id)},
            {"$set": {"manuscript_extracted_images": saved_images, "updated_at": datetime.now(timezone.utc).isoformat()}},
        )

    result["extracted_images"] = saved_images
    return result


class ComposeIn(BaseModel):
    template: str
    title: str
    author: Optional[str] = ""
    source_text: str
    trim_size: str = "6x9"
    platform: str = "kdp"


@api_router.post("/manuscript/compose")
async def manuscript_compose(payload: ComposeIn, user: dict = Depends(get_current_user)):
    # Free tier gets audit/testing only -- composing a full interior PDF is a
    # production deliverable, so it must not be free (matches the same rule
    # already enforced on /export).
    if user.get("tier", "free") == "free" and not user.get("beta_active"):
        raise HTTPException(402, _paid_msg("Interior composition isn't included in the Free plan. Upgrade to Author or higher.", "Interior composition"))
    if payload.template not in MANUSCRIPT_TEMPLATES:
        raise HTTPException(400, "Unknown template")
    if payload.trim_size not in TRIM_SIZES:
        raise HTTPException(400, "Unknown trim size")
    if payload.platform not in PLATFORMS:
        raise HTTPException(400, "Unknown platform")
    trim = TRIM_SIZES[payload.trim_size]
    out_name = f"manuscript_{user['id']}_{uuid.uuid4().hex[:6]}.pdf"
    out_path = UPLOAD_DIR / out_name
    try:
        meta = compose_manuscript_pdf(
            str(out_path), payload.template, payload.title,
            payload.author or "", payload.source_text,
            trim["w"], trim["h"], payload.platform,
        )
    except Exception as e:
        raise HTTPException(500, f"Compose failed: {e}")
    return {
        "file_id": out_name,
        "preview_url": f"/api/manuscript/preview/{out_name}",
        **meta,
    }


@api_router.get("/manuscript/preview/{file_id}")
async def manuscript_preview(file_id: str, request: Request):
    token = request.cookies.get("access_token") or request.query_params.get("token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    fp = UPLOAD_DIR / file_id
    if not fp.exists() or not file_id.startswith("manuscript_"):
        raise HTTPException(404, "Manuscript not found")
    return FileResponse(str(fp), media_type="application/pdf")


# ---- AI Blurb Writer ----
# Uses the Anthropic API directly (Claude Sonnet), matching the "Claude
# Sonnet" copy already shown in the frontend's Blurb dialog and Editor
# tooltip. Requires ANTHROPIC_API_KEY in backend/.env -- returns 503 with
# a clear setup message rather than a generic failure if it's missing,
# same pattern as the Stripe checkout routes above.
import json as _json

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")  # AI Cover Generation + AI Upscale (OpenAI Image API)
ANTHROPIC_BLURB_MODEL = os.environ.get("ANTHROPIC_BLURB_MODEL", "claude-sonnet-4-5-20250929")


@api_router.post("/ai/blurb")
async def generate_blurb(payload: BlurbIn, user: dict = Depends(get_current_user)):
    # Matches the pricing page, which lists "AI Blurb Writer" as an Author-plan-and-up
    # feature (not included in Free) -- same gate shape as /projects/{id}/ai-cover below.
    billing_user = await get_billing_user(user)
    if billing_user.get("tier", "free") == "free":
        raise HTTPException(402, _paid_msg("AI Blurb Writer requires the Author plan or higher.", "AI Blurb Writer"))
    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "AI Blurb Writer isn't configured yet — add ANTHROPIC_API_KEY to backend/.env")

    brief = [f"Title: {payload.title}"]
    if payload.genre:
        brief.append(f"Genre: {payload.genre}")
    if payload.page_count:
        brief.append(f"Approx. page count: {payload.page_count}")
    if payload.themes:
        brief.append(f"Themes/hooks: {payload.themes}")
    if payload.audience:
        brief.append(f"Target audience: {payload.audience}")

    prompt = (
        "You write back-cover book blurbs for self-published authors preparing a print-ready book.\n\n"
        f"{chr(10).join(brief)}\n\n"
        "Write:\n"
        "1. One short punchy tagline (under 12 words).\n"
        "2. Three back-cover blurb variations (80-140 words each), each in a distinct tone "
        "(e.g. literary/atmospheric, commercial/hooky, and warm/personal — pick tones that fit the genre given).\n\n"
        "Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly this shape:\n"
        '{"tagline": "...", "variations": [{"tone": "...", "copy": "..."}, {"tone": "...", "copy": "..."}, {"tone": "...", "copy": "..."}]}'
    )

    try:
        import httpx
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_BLURB_MODEL,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code != 200:
            logger.warning("Anthropic blurb request failed: %s %s", resp.status_code, resp.text[:500])
            raise HTTPException(502, "AI Blurb Writer's model provider returned an error. Try again shortly.")
        data = resp.json()
        raw_text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text").strip()
        # Models sometimes wrap JSON in ```json fences despite instructions -- strip them defensively.
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.lower().startswith("json"):
                raw_text = raw_text[4:]
        parsed = _json.loads(raw_text)
    except HTTPException:
        raise
    except (_json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Could not parse AI blurb response: %s", e)
        raise HTTPException(502, "AI Blurb Writer produced an unexpected response. Try again.")
    except Exception as e:
        logger.warning("AI blurb request errored: %s", e)
        raise HTTPException(502, "Couldn't reach the AI Blurb Writer's model provider. Try again shortly.")

    return {
        "tagline": parsed.get("tagline", ""),
        "variations": parsed.get("variations", []),
    }


# ---- Publisher Template Upload ----
@api_router.post("/projects/{project_id}/template-upload")
async def upload_publisher_template(project_id: str, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """Accept an IngramSpark/KDP/etc. publisher template file (PDF preferred).
    Analyzes it to auto-detect trim dimensions and stores it as the project's blueprint reference.
    """
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")

    ext = Path(file.filename).suffix.lower()
    if ext not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        raise HTTPException(400, "Publisher templates must be PDF or image files")

    file_id = f"{project_id}_tmpl_{uuid.uuid4().hex[:8]}{ext}"
    file_path = UPLOAD_DIR / file_id
    with open(file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    try:
        metadata = analyze_file(str(file_path))
    except Exception as e:
        await log_failure(db, "template_upload_analyze", e, project_id=project_id, user_id=user["id"],
                           context={"filename": file.filename, "ext": ext})
        raise HTTPException(500, f"Couldn't analyze this template file: {e}")
    metadata["original_filename"] = file.filename
    metadata["stored_filename"] = file_id
    metadata["source"] = "publisher_template"

    # Real trim/spine/bleed/safe-zone detection, read from the template's
    # own text and vector graphics -- not a fixed-bleed guess off the page
    # size. Only PDFs carry the text/graphics evidence this needs; image
    # templates fall back to detected_trim = None (unresolved), same as
    # before, rather than a guess.
    detected_trim = None
    detected_spec = None
    if metadata.get("is_pdf"):
        try:
            detected_spec = interpret_publisher_template(file_id, file_path, file.filename)
            # Keep the old detected_trim shape populated too, for any
            # existing frontend code that still reads it directly, using
            # the real extracted/calculated values instead of a guess.
            tw, th = detected_spec["trim_width"], detected_spec["trim_height"]
            if tw["value"] is not None and th["value"] is not None:
                detected_trim = {
                    "raw_width_inches": detected_spec["document_width_in"],
                    "raw_height_inches": detected_spec["document_height_in"],
                    "estimated_trim_width": tw["value"],
                    "estimated_trim_height": th["value"],
                }
        except Exception as e:
            logging.exception("template interpretation failed for %s", file_id)
            detected_spec = {"error": str(e)}
            await log_failure(db, "template_interpretation", e, project_id=project_id, user_id=user["id"],
                               context={"filename": file.filename})

    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {
            "publisher_template": file_id,
            "publisher_template_metadata": metadata,
            "detected_trim": detected_trim,
            "detected_spec": detected_spec,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"template_id": file_id, "metadata": metadata, "detected_trim": detected_trim, "detected_spec": detected_spec}


# ---- Slot Upload (front_cover / back_cover / spine / interior / full_wrap / case_wrap) ----
# case_wrap: the plain hardcover case under a dust jacket. IngramSpark won't
# process a hardcover_jacket book until three files are uploaded (case,
# jacket, interior); it sends both a Case Laminate and a Dust Jacket template
# for the book, and both are needed. Only valid for hardcover_jacket binding.
ALLOWED_SLOTS = {"front_cover", "back_cover", "spine", "interior", "full_wrap", "case_wrap"}


@api_router.post("/projects/{project_id}/slot-upload/{slot}")
async def slot_upload(project_id: str, slot: str, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(400, f"Unknown slot: {slot}")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    if slot == "case_wrap" and p.get("binding") != "hardcover_jacket":
        raise HTTPException(400, "The case file is only needed for Hardcover — Dust Jacket binding. Change Binding first if that's what this book is.")

    billing_user = await get_billing_user(user)
    tier = billing_user.get("tier", "free")
    max_mb = TIERS.get(tier, TIERS["free"])["max_file_mb"]
    if user.get("beta_active") or billing_user.get("beta_active"):
        max_mb = 1024

    ext = Path(file.filename).suffix.lower()
    # A book-only project's interior slot also accepts a Word doc or plain-
    # text manuscript, not just a PDF -- most authors don't have a print-
    # ready PDF sitting around, they have a Word file. It gets converted
    # below rather than stored as-is.
    convertible_manuscript_exts = {".docx", ".txt"}
    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
    if slot == "interior":
        allowed = allowed | convertible_manuscript_exts
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    # Converting a Word/text manuscript into a print-ready interior PDF is a
    # production deliverable, same as the standalone "Compose Interior" tool
    # -- this must carry the same tier gate as /manuscript/compose rather
    # than becoming a free backdoor around that paywall.
    if slot == "interior" and ext in convertible_manuscript_exts:
        promo_bypass = p.get("promo_access") in ("full_access", "interior_only_access")
        if tier == "free" and not (user.get("beta_active") or billing_user.get("beta_active") or promo_bypass):
            raise HTTPException(402, _paid_msg("Converting a Word/text manuscript into a print-ready interior isn't included in the Free plan. Upgrade to Author or higher, or upload a PDF directly.", "Converting a Word/text manuscript into a print-ready interior"))

    file_id = f"{project_id}_{slot}_{uuid.uuid4().hex[:6]}{ext}"
    file_path = UPLOAD_DIR / file_id
    size = 0
    with open(file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_mb * 1024 * 1024:
                f.close()
                os.remove(file_path)
                raise HTTPException(413, f"File exceeds {max_mb}MB limit")
            f.write(chunk)

    compose_warnings = []
    if slot == "interior" and ext in convertible_manuscript_exts:
        # Re-typeset the actual text content through the same composer
        # engine "Compose Interior" already uses (correct margins, trim
        # size, PDF/X-1a output) instead of storing a file that would just
        # fail every layout check as uploaded -- most self-published
        # authors' Word formatting isn't print-ready (wrong margins,
        # screen-sized fonts, no real page breaks).
        try:
            if ext == ".docx":
                extracted = extract_manuscript_text(str(file_path))
                compose_warnings = extracted["warnings"]
                source_text = extracted["source_text"]
            else:
                source_text = file_path.read_text(encoding="utf-8", errors="replace")
            trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
            composed_id = f"{project_id}_interior_composed_{uuid.uuid4().hex[:6]}.pdf"
            composed_path = UPLOAD_DIR / composed_id
            await run_with_timeout(
                compose_manuscript_pdf,
                str(composed_path), "fiction_novel", p.get("name") or "Untitled Book", "",
                source_text, trim["w"], trim["h"], p["platform"],
            )
        except HTTPException:
            raise
        except Exception as e:
            await log_failure(db, "slot_upload_compose", e, project_id=project_id, user_id=user["id"],
                               context={"filename": file.filename, "ext": ext, "slot": slot})
            raise HTTPException(400, f"Couldn't convert this file into a print-ready interior: {e}")
        # The raw .docx/.txt is the customer's actual source manuscript --
        # preserved on disk (not deleted as a mere "staging step" the way
        # this used to work) so they can always get the file they actually
        # wrote back, independent of how the composer interpreted it.
        raw_manuscript_stored_filename = file_id
        file_id = composed_id
        file_path = composed_path
    else:
        raw_manuscript_stored_filename = None

    try:
        metadata = analyze_file(str(file_path))
        metadata["original_filename"] = file.filename
        metadata["stored_filename"] = file_id
        metadata["slot"] = slot
        # A direct (non-composed) upload is its own original -- the first
        # file in this slot's new lineage; a composed interior's original
        # is the raw manuscript preserved above instead of the PDF it
        # produced.
        metadata["original_stored_filename"] = raw_manuscript_stored_filename or file_id
        if compose_warnings:
            metadata["compose_warnings"] = compose_warnings

        # The page count decides the spine, and so every cover size -- so it comes from the interior itself
        # rather than trusting a typed number (which starts at 200 for a new project).
        page_count_set = None
        if slot == "interior" and metadata.get("pdf_pages") and metadata["pdf_pages"] != p.get("page_count"):
            page_count_set = {"from": p.get("page_count"), "to": metadata["pdf_pages"]}
            p["page_count"] = metadata["pdf_pages"]
        compliance = await run_with_timeout(_slot_compliance, p, slot, str(file_path), metadata)
    except HTTPException:
        raise
    except Exception as e:
        await log_failure(db, "slot_upload_analyze", e, project_id=project_id, user_id=user["id"],
                           context={"filename": file.filename, "ext": ext, "slot": slot})
        raise HTTPException(500, f"Couldn't analyze this file: {e}. It may be corrupted or an unsupported variant of {ext}.")

    slots = p.get("slots") or {}
    # The customer is directly replacing this slot's content with a new
    # upload -- unlike a repair/regeneration, this genuinely starts a new
    # lineage, so both the prior working file AND its preserved original
    # (if one had survived past repairs) are cleaned up here rather than
    # kept around orphaned forever.
    prior = slots.get(slot)
    if prior:
        for stale in {prior.get("stored_filename"), prior.get("original_stored_filename")}:
            if stale:
                try: os.remove(UPLOAD_DIR / stale)
                except OSError: pass
    if book_pass.book_pass_on():
        # The free scan's "found N issues" must match what the paid audit will show (one defined audit), so
        # count with the audit itself rather than the Editor's own quicker check list.
        count = await run_with_timeout(_slot_audit_issue_count, p, slot, str(file_path), metadata)
        if count is not None:
            metadata["audit_issue_count"] = count
    slots[slot] = {**metadata, "compliance": compliance}

    # If uploading full_wrap, also mirror into legacy uploaded_file for existing flows
    update = {
        "slots": slots,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if slot == "full_wrap":
        update["uploaded_file"] = file_id
        update["file_metadata"] = metadata
        update["compliance"] = compliance
    if page_count_set:
        update["page_count"] = p["page_count"]
        p["slots"] = slots
        update.update(await run_with_timeout(_rescan_cover_slots, p))   # covers were sized for the old count

    await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": update})
    body = {"slot": slot, "file_metadata": metadata, "compliance": compliance}
    if page_count_set:
        body["page_count_set"] = page_count_set
    return await _present_upload(p, user, body)


# Checks the export re-verifies on the final files -- a source-file warning in one of these areas is
# replaced by the final file's own result (e.g. an RGB upload is CMYK after export, and that's measured).
_REVERIFIED_ON_FINAL = ("colorspace", "transparency", "pdfx1a", "bleed", "pdf_dpi", "total_ink_coverage",
                        "cover_size", "interior_page_size_mismatch", "interior_safety_margin",
                        "cover_safety_margin", "cover_spine_text_margin", "cover_spine_text_forbidden",
                        "cover_template_leftovers", "cover_isbn_mismatch")
_COVER_TEXT_CHECKS = ("cover_safety_margin", "cover_spine_text_margin", "cover_spine_text_forbidden",
                      "cover_template_leftovers", "cover_isbn_mismatch")
_CERT_CHECKS = (   # (label, finding ids that fail it)
    ("PDF/X-1a:2001 print standard", ("pdfx1a_not_declared", "pdfx1a_missing_output_intent", "icc_profile_missing")),
    ("All fonts embedded and licensed for print", ("fonts_not_embedded", "font_license_restricted")),
    ("No live transparency", ("live_transparency_detected",)),
    ("No hidden layers", ("layers_detected",)),
    ("CMYK color only (no RGB)", ("rgb_color_in_final",)),
    ("Ink coverage within the distributor's limit", ("total_ink_coverage",)),
)


def _diy_fix(f: dict, p: dict, where: str) -> tuple:
    """Step-by-step instructions (and tools) for fixing an issue by hand -- for the rare case SparkPrep
    couldn't fix it. Uses the check's own steps when it has them, else a guide written for that issue."""
    steps = [s for s in (f.get("fix_steps") or []) if s]
    tools = [t for t in (f.get("fix_tools") or []) if t]
    fid = (f.get("id") or "").lower()
    if not steps:
        if fid == "dpi" or "resolution" in fid:
            steps = ["Open the original artwork file (Photoshop, Canva, Affinity, InDesign) -- not an exported copy.",
                     "Export it again at 300 DPI at its full final size (the exact size SparkPrep shows under the upload box).",
                     "If you only have a small version, rebuild or re-source the image at full size -- enlarging a small "
                     "image can't add real detail, it only makes it look soft.",
                     "Upload the new file to SparkPrep and export again."]
            tools = tools or ["Adobe Photoshop", "Affinity Photo", "Canva (download as PDF Print)"]
        elif "size" in fid:
            steps = ["Set your document to the exact size SparkPrep shows under the upload box (it already includes bleed "
                     "and the spine for your page count).",
                     "Re-position your artwork so the spine and panels line up with your distributor's template.",
                     "Export as PDF and upload it to SparkPrep again."]
            tools = tools or ["Your distributor's cover template", "Adobe InDesign", "Affinity Publisher"]
        else:
            steps = ["Open the original design file for this " + where.lower() + " and correct the issue described above.",
                     "Export it again as a PDF, upload it to SparkPrep, and export -- SparkPrep re-checks everything."]
    steps.append("Stuck? Ask the SparkPrep Assistant in the app -- tell it the issue name shown here.")
    return steps, tools


def _certify_final_files(p: dict, parts: list, plat: dict, platform_key: str, trim: dict,
                         source_compliance: list, spine_w: float, export_id: str) -> dict:
    """The SparkPrep Certified check: every check below, run on the FINAL exported files (not the uploads).
    Certified only when every one passes AND no source-file issue is left open."""
    import pikepdf
    name = plat.get("name", platform_key)
    checked_at = datetime.now(timezone.utc)
    files, checks, open_items = [], [], []
    interior_pages = None

    def record(label, findings, where):
        ok = not findings
        checks.append({"label": f"{where}: {label}", "passed": ok})
        for f in findings:
            steps, tools = _diy_fix(f, p, where)
            open_items.append({"title": f"{where}: {f.get('title') or label}", "why": f.get("why_it_fails", ""),
                               "id": f.get("id"), "steps": steps, "tools": tools})

    for path, _arc, key, _res in parts:
        where = {"cover": "Cover", "case": "Case cover", "interior": "Interior"}[key]
        with pikepdf.open(str(path)) as pdf:
            n_pages = len(pdf.pages)
            box = pdf.pages[0].mediabox
            size = [round(float(box[2] - box[0]) / 72, 3), round(float(box[3] - box[1]) / 72, 3)]
            rgb = check_rgb_color(pdf, max_pages=ADVANCED_INTERIOR_MAX_PAGES)
        files.append({"part": where, "size_in": size, "pages": n_pages})
        found = run_pdf_structure_audit(str(path), name, max_pages=ADVANCED_INTERIOR_MAX_PAGES)
        if rgb:
            found.append(rgb)
        ink = check_final_pdf_ink_coverage(str(path), platform_key, name, max_pages=ADVANCED_INTERIOR_MAX_PAGES)
        if ink:
            found.append(ink)
        for label, ids in _CERT_CHECKS:
            record(label, [f for f in found if f["id"] in ids], where)
        other = [f for f in found if not any(f["id"] in ids for _l, ids in _CERT_CHECKS)]
        if other:
            record("Other print checks", other, where)

        if key == "interior":
            interior_pages = {"checked": min(n_pages, ADVANCED_INTERIOR_MAX_PAGES), "total": n_pages}
            margins = check_interior_safety_margins(
                str(path), name, trim["w"], trim["h"], max_pages=ADVANCED_INTERIOR_MAX_PAGES,
                bleed_in=plat["bleed"], all_sides_bleed_ok=(platform_key == "lulu"))
            record(f"Page size and text safe margins (all {interior_pages['checked']} pages)", margins, where)
            if n_pages > ADVANCED_INTERIOR_MAX_PAGES:
                open_items.append({"title": f"Interior: pages {ADVANCED_INTERIOR_MAX_PAGES + 1}-{n_pages} weren't checked",
                                   "why": f"SparkPrep checks up to {ADVANCED_INTERIOR_MAX_PAGES} pages, so a longer book can't be certified yet."})
        else:
            geom = _case_wrap_geometry(p) if key == "case" else _full_wrap_geometry(p)
            size_ok = abs(size[0] - geom["total_width"]) <= 0.02 and abs(size[1] - geom["total_height"]) <= 0.02
            record(f"Exact size for a {fmt_in(spine_w)}\" spine ({fmt_in(geom['total_width'])}\" x {fmt_in(geom['total_height'])}\")",
                   [] if size_ok else [{"title": f"is {size[0]}\" x {size[1]}\"", "why_it_fails": "Wrong size for this book."}], where)
            # Checked on the uploaded cover (it had to pass before export), not re-read here: export never moves
            # the artwork -- it's placed at exactly the size the cover-size check already confirmed -- and reading
            # the text on a full-size final cover took 1-4 minutes and ~1 GB on a 2 GB server, which made long
            # exports time out with nothing delivered (2026-10-01).
            slots_here = ("case_wrap",) if key == "case" else ("full_wrap", "front_cover", "back_cover", "spine")
            safety = [{"title": c.get("label") or c.get("id"), "why_it_fails": c.get("message", ""), "id": c.get("id")}
                      for sl in slots_here for c in ((p.get("slots") or {}).get(sl) or {}).get("compliance") or []
                      if c.get("id") in _COVER_TEXT_CHECKS and c.get("status") != "pass"]
            record("Text inside the safe area, spine text clear of the folds, no template parts left, ISBN matches "
                   "(checked on your cover file; export keeps your art in place)",
                   safety, where)

    carried = [c for c in (source_compliance or [])
               if c.get("status") != "pass" and not any(k in (c.get("id") or "") for k in _REVERIFIED_ON_FINAL)]
    record("Image resolution and remaining upload checks", [
        {"title": c.get("label") or c.get("id"), "why_it_fails": c.get("message", ""), "id": c.get("id")}
        for c in carried], "All files")

    certified = not open_items
    return {
        "certified": certified,
        "report_name": ("The SparkPrep Certified Complete Publisher Preflight Report" if certified
                        else "SparkPrep Preflight Report"),
        "certificate_id": f"SPC-{checked_at:%Y%m%d}-{export_id.upper()}" if certified else None,
        "checked_at": checked_at.isoformat(),
        "platform": name,
        "files": files, "checks": checks, "open": open_items, "interior_pages": interior_pages,
    }


_COVER_SLOTS = ("full_wrap", "case_wrap", "front_cover", "back_cover", "spine")
_GEOMETRY_FIELDS = ("page_count", "spine_width_override", "paper_type", "trim_size", "binding", "platform")


def _spine_number_needed(platform: str, binding: str, page_count: int, paper_type: str,
                         override: Optional[float]) -> bool:
    """True for an IngramSpark / KDP / B&N hardcover when the user hasn't entered its spine width. Their cover
    formulas are published (SparkPrep uses them), but the hardcover spine width itself comes on the cover template
    ("the template ... contains book size and spine width information" -- IngramSpark File Creation Guide p.20-26).
    Their own templates show why a guess won't do: Creme, 74 pages = 0.313", 108 pages = 0.375"."""
    if binding not in ("hardcover_case", "hardcover_jacket") or override:
        return False
    paper = PAPER_TYPES.get(paper_type, PAPER_TYPES["white_50lb"])
    return not calculate_spine_width_for_platform(page_count or 0, paper_ppi(paper, platform), platform, binding)[1]


def _project_needs_spine_number(p: dict) -> bool:
    return _spine_number_needed(p.get("platform", "kdp"), p.get("binding", "paperback"), p.get("page_count") or 0,
                                p.get("paper_type", "white_50lb"), p.get("spine_width_override"))


def _spine_needed_message(platform_name: str) -> str:
    return (f"{platform_name} gives a hardcover's spine width on its cover template, set for your exact page count "
            f"and paper (it's printed under the spine, e.g. 0.313). Enter it in Spine Width -- SparkPrep builds your "
            "cover from it, so until then it can't tell whether this cover is the right size.")


def _cover_bleed_for_slot(p: dict, slot: str) -> Optional[float]:
    """How far outside the trim a cover file extends on each side -- 0.625" for a case wrap, 0.125" for most."""
    if slot == "interior":
        return None
    binding = "hardcover_case" if slot == "case_wrap" else p.get("binding", "paperback")
    return resolve_binding_spec(binding, p.get("platform", "kdp"))["bleed"]


def _slot_compliance(p: dict, slot: str, file_path: str, metadata: dict) -> list:
    """The Editor's checks for one uploaded file, against the project's current specs."""
    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    final_w, final_h = _target_inches_for_slot(p, slot)
    spine_kwargs = {}
    if slot == "full_wrap":
        geom = _full_wrap_geometry(p)
        spine_kwargs = {"spine_x_in": geom["spine_x"], "spine_w_in": geom["spine_width"],
                        "page_count": p.get("page_count"), "binding": p.get("binding", "paperback")}
    elif slot == "case_wrap":
        geom = _case_wrap_geometry(p)
        spine_kwargs = {"spine_x_in": geom["spine_x"], "spine_w_in": geom["spine_width"],
                        "page_count": p.get("page_count"), "binding": "hardcover_case"}
    compliance = run_compliance_checks(
        metadata, trim["w"], trim["h"], plat["bleed"], p["platform"],
        file_path=file_path, slot=slot, platform_name=plat.get("name"), max_pages=BASIC_CHECK_MAX_PAGES,
        final_w=final_w, final_h=final_h, cover_bleed_in=_cover_bleed_for_slot(p, slot), **spine_kwargs,
        expected_isbn=p.get("isbn"),
    )
    if slot in ("full_wrap", "case_wrap") and _project_needs_spine_number(p):
        compliance = [{"id": "spine_width_needed", "label": "Spine width needed", "status": "fail",
                       "message": _spine_needed_message(plat.get("name", p["platform"])),
                       "auto_fix": False, "fix_action": None}] + [c for c in compliance if c.get("id") != "cover_size"]
    return compliance


def _rescan_cover_slots(p: dict) -> dict:
    """Re-check every uploaded cover against the project's CURRENT specs -- a cover's right size depends on
    page count, spine, paper, trim and binding, so its stored result goes stale when any of them change.
    Returns the fields to $set."""
    slots = dict(p.get("slots") or {})
    update = {}
    for slot in _COVER_SLOTS:
        meta = slots.get(slot)
        path = UPLOAD_DIR / meta["stored_filename"] if meta and meta.get("stored_filename") else None
        if not path or not path.exists():
            continue
        clean = {k: v for k, v in meta.items() if k not in ("compliance", "audit_issue_count")}
        compliance = _slot_compliance(p, slot, str(path), clean)
        new_meta = {**clean, "compliance": compliance}
        if book_pass.book_pass_on():
            count = _slot_audit_issue_count(p, slot, str(path), clean)
            if count is not None:
                new_meta["audit_issue_count"] = count
        slots[slot] = new_meta
        if slot == "full_wrap":
            update["compliance"] = compliance
            update["file_metadata"] = {k: v for k, v in new_meta.items() if k != "compliance"}
    if slots != (p.get("slots") or {}):
        update["slots"] = slots
    return update


def _save_generated_slot_file(p: dict, project_id: str, slot: str, file_id: str, data: bytes,
                               original_filename: str, extra_meta: dict) -> tuple:
    """Shared by AI cover generation and cover-template rendering: writes
    bytes to UPLOAD_DIR, analyzes + runs compliance the same way a real
    upload does, replaces the prior file in that slot, and returns
    (metadata, compliance) for the endpoint to respond with."""
    file_path = UPLOAD_DIR / file_id
    with open(file_path, "wb") as f:
        f.write(data)

    metadata = analyze_file(str(file_path))
    metadata["original_filename"] = original_filename
    metadata["stored_filename"] = file_id
    metadata["slot"] = slot
    metadata.update(extra_meta)

    # The same checks as an upload (_slot_compliance), so a generated file is judged exactly like any other.
    return metadata, _slot_compliance(p, slot, str(file_path), metadata)


async def _replace_slot(project_id: str, p: dict, slot: str, metadata: dict, compliance: list):
    """Shared by autofix's AI Upscale, AI Cover generation, and Cover
    Template rendering -- all system-driven regenerations of whatever is
    CURRENTLY in a slot, as opposed to slot_upload/slot_delete (the
    customer directly choosing to replace or remove content). Preserves
    the slot's original_stored_filename (the first file this slot's
    current lineage ever held) the same way autofix() does: never delete
    it, and carry it forward so the next regeneration still knows what to
    protect."""
    slots = p.get("slots") or {}
    prior = slots.get(slot)
    original_stored_filename = None
    if prior:
        original_stored_filename = prior.get("original_stored_filename") or prior.get("stored_filename")
        if prior.get("stored_filename") and prior["stored_filename"] != original_stored_filename:
            try:
                os.remove(UPLOAD_DIR / prior["stored_filename"])
            except OSError:
                pass
    if original_stored_filename:
        metadata["original_stored_filename"] = original_stored_filename
    slots[slot] = {**metadata, "compliance": compliance}
    update = {"slots": slots, "updated_at": datetime.now(timezone.utc).isoformat()}
    if slot == "full_wrap":
        update["uploaded_file"] = metadata["stored_filename"]
        update["file_metadata"] = metadata
        update["compliance"] = compliance
    await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": update})


# ---- AI Cover Generation (Author plan+, requires OPENAI_API_KEY) ----
class AICoverIn(BaseModel):
    prompt: str
    genre: Optional[str] = None
    slot: str = "full_wrap"  # full wrap only -- see ai_generate_cover


@api_router.post("/projects/{project_id}/ai-cover")
async def ai_generate_cover(project_id: str, payload: AICoverIn, user: dict = Depends(get_current_user)):
    if payload.slot != "full_wrap":
        # A front-only image can't be exported: nothing assembles separate pieces into a wrap yet.
        raise HTTPException(400, "AI cover art is generated as a full cover wrap (back, spine and front).")
    if not payload.prompt.strip():
        raise HTTPException(400, "Describe the cover art you want first.")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")

    billing_user = await get_billing_user(user)
    if billing_user.get("tier", "free") == "free" and p.get("promo_access") != "full_access":
        raise HTTPException(402, _paid_msg("AI Cover Generation requires the Author plan or higher.", "AI Cover Generation"))
    if not OPENAI_API_KEY:
        raise HTTPException(503, "AI Cover Generation isn't configured yet — add OPENAI_API_KEY to backend/.env")

    prompt = build_cover_prompt(payload.prompt, p.get("name"), payload.genre, payload.slot == "full_wrap")
    try:
        image_bytes = await generate_cover_image(prompt, OPENAI_API_KEY)
    except AICoverError as e:
        raise HTTPException(e.status_code, str(e))

    # The generator returns a fixed shape (e.g. 1792x1024); a real wrap is whatever this book's trim, spine and
    # binding make it. It's artwork with no text, so fill the exact wrap by centered crop -- never stretch it.
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(image_bytes)) as art:
        fitted = ImageOps.fit(art.convert("RGB"), _target_pixels_for_slot(p, "full_wrap"), method=Image.LANCZOS)
    buf = io.BytesIO()
    fitted.save(buf, "PNG", dpi=(300, 300))
    image_bytes = buf.getvalue()

    file_id = f"{project_id}_{payload.slot}_ai_{uuid.uuid4().hex[:6]}.png"
    metadata, compliance = _save_generated_slot_file(
        p, project_id, payload.slot, file_id, image_bytes,
        original_filename="ai_generated_cover.png",
        extra_meta={"ai_generated": True, "ai_prompt": payload.prompt[:500]},
    )
    await _replace_slot(project_id, p, payload.slot, metadata, compliance)
    return await _present_upload(p, user, {"slot": payload.slot, "file_metadata": metadata, "compliance": compliance})


def _target_pixels_for_slot(p: dict, slot: str) -> tuple[int, int]:
    """Target pixel dimensions for a slot at 300 DPI, trim + bleed included."""
    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    binding = p.get("binding", "paperback")
    platform_key = p.get("platform", "kdp")
    if slot == "interior":
        # Interior bleed is a flat platform-level constant regardless of
        # binding (an interior page isn't a "hardcover jacket wrap" -- it
        # doesn't get bigger because the cover binding is). Using the
        # binding's cover bleed here (e.g. 0.625" for hardcover_case) would
        # inflate the target canvas by up to 1" total on a hardcover project's
        # interior page for no real reason.
        bleed = PLATFORMS.get(platform_key, PLATFORMS["kdp"])["bleed"]
        return round((trim["w"] + bleed * 2) * 300), round((trim["h"] + bleed * 2) * 300)

    if slot == "case_wrap":
        # Always a plain hardcover_case wrap, regardless of the project's
        # real binding (hardcover_jacket) -- see _case_wrap_geometry.
        dims = _case_wrap_geometry(p)
        return round(dims["total_width"] * 300), round(dims["total_height"] * 300)

    # Cover bleed is a property of the (platform, binding) pair -- see the
    # export path's cover_bleed for the same fix and why it matters.
    bleed = resolve_binding_spec(binding, platform_key)["bleed"]

    if slot == "full_wrap":
        paper = PAPER_TYPES.get(p["paper_type"], PAPER_TYPES["white_50lb"])
        spine_w = calculate_spine_width_for_platform(p.get("page_count", 0), paper_ppi(paper, platform_key), platform_key, binding)[0]
        if p.get("spine_width_override"):
            spine_w = float(p["spine_width_override"])
        dims = calculate_full_cover_dimensions(trim["w"], trim["h"], spine_w, bleed, binding, platform_key)
        return round(dims["total_width"] * 300), round(dims["total_height"] * 300)

    if slot == "spine":
        paper = PAPER_TYPES.get(p["paper_type"], PAPER_TYPES["white_50lb"])
        spine_w = calculate_spine_width_for_platform(p.get("page_count", 0), paper_ppi(paper, platform_key), platform_key, binding)[0]
        if p.get("spine_width_override"):
            spine_w = float(p["spine_width_override"])
        return round(spine_w * 300), round((trim["h"] + bleed * 2) * 300)

    return round((trim["w"] + bleed * 2) * 300), round((trim["h"] + bleed * 2) * 300)


def _target_inches_for_slot(p: dict, slot: str) -> tuple[float, float]:
    """Inches equivalent of _target_pixels_for_slot -- same bleed-inclusive
    target size (correct per-slot: full wrap size for full_wrap, spine width
    for spine, plain trim+bleed for a single cover panel or interior page),
    used by compliance checks (DPI, cover safety margin) that need a
    physical dimension rather than a pixel count. Keeping this as a thin
    wrapper instead of duplicating the per-slot logic guarantees the DPI/
    margin checks and the AI-upscale target always agree on what "correct
    size" means for a given slot. Full wraps return the exact geometry
    instead: rounding to whole pixels first (20.4375" -> 6131px -> 20.437")
    made the cover-size message disagree with the distributor's own number."""
    if slot in ("full_wrap", "case_wrap"):
        g = _case_wrap_geometry(p) if slot == "case_wrap" else _full_wrap_geometry(p)
        return g["total_width"], g["total_height"]
    if slot in ("front_cover", "back_cover", "spine"):
        g = _full_wrap_geometry(p)
        h = g["panel_height"] + 2 * g["bleed"]
        return (g["spine_width"], h) if slot == "spine" else (g["panel_width"] + 2 * g["bleed"], h)
    w_px, h_px = _target_pixels_for_slot(p, slot)
    return w_px / 300.0, h_px / 300.0


def _full_wrap_geometry(p: dict) -> dict:
    """The full calculate_full_cover_dimensions() breakdown for this
    project's full_wrap cover (panel positions, spine_x/spine_width included)
    -- used by the spine text-safety check, which needs to know exactly
    where the spine column sits, not just the overall canvas size."""
    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    binding = p.get("binding", "paperback")
    platform_key = p.get("platform", "kdp")
    paper = PAPER_TYPES.get(p["paper_type"], PAPER_TYPES["white_50lb"])
    spine_w = calculate_spine_width_for_platform(p.get("page_count", 0), paper_ppi(paper, platform_key), platform_key, binding)[0]
    if p.get("spine_width_override"):
        spine_w = float(p["spine_width_override"])
    bleed = resolve_binding_spec(binding, platform_key)["bleed"]
    return calculate_full_cover_dimensions(trim["w"], trim["h"], spine_w, bleed, binding, platform_key)


def _case_wrap_geometry(p: dict) -> dict:
    """Same breakdown as _full_wrap_geometry, but always computed as a plain
    hardcover_case wrap, regardless of the project's real binding. Used only
    for the case_wrap slot -- the plain case that sits underneath a dust
    jacket on a hardcover_jacket book. Physically, that case is a
    case-laminate wrap either way; it doesn't follow jacket flap/hinge
    geometry just because the book it's inside of has one."""
    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    platform_key = p.get("platform", "kdp")
    paper = PAPER_TYPES.get(p["paper_type"], PAPER_TYPES["white_50lb"])
    spine_w = calculate_spine_width_for_platform(p.get("page_count", 0), paper_ppi(paper, platform_key), platform_key, "hardcover_case")[0]
    if p.get("spine_width_override"):
        spine_w = float(p["spine_width_override"])
    bleed = resolve_binding_spec("hardcover_case", platform_key)["bleed"]
    return calculate_full_cover_dimensions(trim["w"], trim["h"], spine_w, bleed, "hardcover_case", platform_key)


@api_router.post("/projects/{project_id}/ai-enhance/{slot}")
async def ai_enhance_image(project_id: str, slot: str, user: dict = Depends(get_current_user)):
    """'AI Upscale' fix option for a low-DPI compliance failure: resizes the
    existing slot image up to the exact pixel size needed to hit 300 DPI at
    this project's trim + bleed, via LANCZOS resampling + an adaptive
    unsharp mask (see image_upscale_engine.py) -- self-hosted, CPU, no
    external API, and consistently under 2 seconds regardless of image
    size. A prior Real-ESRGAN-based version added marginally more pixel
    detail but ran 90s-4.5min per image on this service's 1-core instance
    and regularly timed out (502/499) on real requests; this trades a
    small amount of sharpness for actually finishing. See the compliance
    re-check the frontend runs immediately after this to confirm it
    actually cleared the DPI failure."""
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(400, f"Unknown slot: {slot}")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    await _require_product(p, user, "AI Upscale")
    slot_data = (p.get("slots") or {}).get(slot)
    if not slot_data or not slot_data.get("stored_filename"):
        raise HTTPException(404, f"No uploaded file in slot '{slot}'")

    billing_user = await get_billing_user(user)
    if billing_user.get("tier", "free") == "free" and p.get("promo_access") != "full_access":
        raise HTTPException(402, _paid_msg("AI Upscale requires the Author plan or higher.", "AI Upscale"))

    source_path = UPLOAD_DIR / slot_data["stored_filename"]
    if not source_path.exists():
        raise HTTPException(404, "Source file is missing on the server")
    ext = source_path.suffix.lower()
    # .tif/.tiff was excluded here, but upscale_to_size() opens it fine --
    # Image.open(...).convert("RGB") handles CMYK TIFF natively. That
    # mattered in practice: SparkPrep's own CMYK Auto-Fix always saves its
    # output as .tif (see convert_to_cmyk), so running Auto-Fix before AI
    # Upscale silently locked a user out of AI Upscale on that exact file --
    # a real, self-inflicted dead end, not a genuine format limitation.
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"):
        raise HTTPException(400, f"AI Upscale supports PNG/JPEG/WEBP/TIFF source images, not {ext} files.")

    target_w_px, target_h_px = _target_pixels_for_slot(p, slot)
    try:
        with open(source_path, "rb") as f:
            source_bytes = f.read()
        image_bytes = await run_with_timeout(upscale_to_size, source_bytes, target_w_px, target_h_px)
    except HTTPException:
        raise
    except Exception as e:
        await log_failure(db, "ai_enhance", e, project_id=project_id, user_id=user["id"], context={"slot": slot})
        raise HTTPException(502, f"AI Upscale failed: {e}")

    file_id = f"{project_id}_{slot}_enhanced_{uuid.uuid4().hex[:6]}.jpg"
    metadata, compliance = _save_generated_slot_file(
        p, project_id, slot, file_id, image_bytes,
        original_filename=slot_data.get("original_filename") or "enhanced.jpg",
        extra_meta={"ai_enhanced": True},
    )
    await _replace_slot(project_id, p, slot, metadata, compliance)
    # Part of the book's story in its preflight report: what was wrong, and that AI Upscale fixed it.
    before = next((c for c in slot_data.get("compliance") or [] if c.get("id") == "dpi"), None)
    after = next((c for c in compliance if c.get("id") == "dpi"), None)
    if before and before.get("status") != "pass":
        entry = {"at": datetime.now(timezone.utc).isoformat(), "slot": slot, "status": "ai_upscale",
                 "found": [{"id": "dpi", "label": before.get("label", "Resolution"), "message": before.get("message", "")}],
                 "resolved": ["dpi"] if after and after.get("status") == "pass" else [],
                 "remaining": [] if after and after.get("status") == "pass" else ["dpi"]}
        log = list((await db.projects.find_one({"_id": ObjectId(project_id)}) or {}).get("repair_log") or []) + [entry]
        await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": {"repair_log": log[-200:]}})
    return {"slot": slot, "file_metadata": metadata, "compliance": compliance}


# ---- Cover Design Template library (typographic starter covers, no AI/art assets needed) ----
@api_router.get("/cover-templates")
async def list_cover_templates_route():
    return {"templates": list_cover_templates()}


class CoverTemplateApplyIn(BaseModel):
    template_key: str


@api_router.post("/projects/{project_id}/cover-template")
async def apply_cover_template(project_id: str, payload: CoverTemplateApplyIn, user: dict = Depends(get_current_user)):
    if payload.template_key not in COVER_TEMPLATES:
        raise HTTPException(400, f"Unknown template: {payload.template_key}")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")

    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    paper = PAPER_TYPES.get(p["paper_type"], PAPER_TYPES["white_50lb"])
    binding = p.get("binding", "paperback")
    platform_key = p.get("platform", "kdp")
    cover_bleed = resolve_binding_spec(binding, platform_key)["bleed"]
    spine_w = calculate_spine_width_for_platform(p.get("page_count", 0), paper_ppi(paper, platform_key), platform_key, binding)[0]
    if p.get("spine_width_override"):
        spine_w = float(p["spine_width_override"])

    title = (p.get("name") or "Untitled Book").strip()
    file_id = f"{project_id}_full_wrap_tpl_{uuid.uuid4().hex[:6]}.pdf"
    file_path = UPLOAD_DIR / file_id
    render_cover_template(
        str(file_path), payload.template_key, title=title, author=(user.get("name") or ""),
        trim_w=trim["w"], trim_h=trim["h"], spine_w=spine_w, bleed=cover_bleed, binding=binding,
        platform=platform_key,
    )
    metadata, compliance = _save_generated_slot_file(
        p, project_id, "full_wrap", file_id, file_path.read_bytes(),
        original_filename=f"{payload.template_key}.pdf",
        extra_meta={"cover_template": payload.template_key},
    )
    await _replace_slot(project_id, p, "full_wrap", metadata, compliance)
    return await _present_upload(p, user, {"slot": "full_wrap", "file_metadata": metadata, "compliance": compliance})


@api_router.delete("/projects/{project_id}/slot/{slot}")
async def slot_delete(project_id: str, slot: str, user: dict = Depends(get_current_user)):
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(400, "Unknown slot")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    slots = p.get("slots") or {}
    prior = slots.pop(slot, None)
    if prior:
        # Explicit customer-directed removal -- unlike a repair, this is
        # the one case where cleaning up the preserved original too is
        # correct: the customer said to delete this content entirely.
        for stale in {prior.get("stored_filename"), prior.get("original_stored_filename")}:
            if stale:
                try: os.remove(UPLOAD_DIR / stale)
                except OSError: pass
    await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": {"slots": slots}})
    return {"ok": True, "slot": slot}


@api_router.get("/projects/{project_id}/slot/{slot}/preview")
async def slot_preview(project_id: str, slot: str, request: Request):
    token = request.cookies.get("access_token") or request.query_params.get("token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        user_id = payload["sub"]
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user_id})
    if not p:
        raise HTTPException(404, "Project not found")
    slots = p.get("slots") or {}
    slot_data = slots.get(slot)
    if not slot_data or not slot_data.get("stored_filename"):
        raise HTTPException(404, "Slot empty")
    fp = UPLOAD_DIR / slot_data["stored_filename"]
    if not fp.exists():
        raise HTTPException(404, "File missing")
    try:
        return _render_web_preview(fp)
    except Exception as e:
        await log_failure(db, "slot_preview_render", e, project_id=project_id, user_id=user_id,
                           context={"slot": slot, "filename": slot_data["stored_filename"]})
        raise HTTPException(500, f"Couldn't render a preview of this file: {e}")


@api_router.get("/projects/{project_id}/slot/{slot}/original")
async def slot_original_download(project_id: str, slot: str, user: dict = Depends(get_current_user)):
    """Lets the customer retrieve exactly what they originally uploaded to
    this slot, independent of anything Auto-Fix/AI Upscale/AI Cover/Cover
    Template have since done to it -- SparkPrep's repair pipeline updates
    a slot's *working* file in place, but never deletes the file recorded
    here (see autofix()/_replace_slot()'s original_stored_filename
    handling), so this is always the true as-uploaded source, not a
    repaired copy. A project created before this endpoint existed has no
    original_stored_filename on file and gets a clear 404 rather than
    silently serving the current (possibly already-repaired) file under
    a misleading name.
    """
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(400, "Unknown slot")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    slot_data = (p.get("slots") or {}).get(slot)
    if not slot_data:
        raise HTTPException(404, "Slot empty")
    original_stored_filename = slot_data.get("original_stored_filename")
    if not original_stored_filename:
        raise HTTPException(404, "No preserved original on file for this slot")
    fp = UPLOAD_DIR / original_stored_filename
    if not fp.exists():
        raise HTTPException(404, "Original file missing on disk")
    download_name = slot_data.get("original_filename") or original_stored_filename
    return FileResponse(str(fp), filename=download_name)


@api_router.patch("/projects/{project_id}/adjustments")
async def update_adjustments(project_id: str, payload: ManualAdjustments, user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    adj = p.get("adjustments") or {}
    for k, v in payload.model_dump().items():
        if v is not None:
            adj[k] = v
    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {"adjustments": adj, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"adjustments": adj}


# ---- $1.99 Print Failure Audit (anonymous, one-time payment) ----
AUDIT_PRICE_CENTS = 199


class AuditStart(BaseModel):
    platform: str = "kdp"
    trim_size: str = "6x9"
    # A cover isn't shaped like an interior page -- it's front+spine+back(+flaps),
    # and the expected canvas size depends on binding and page count (for spine
    # width), not just trim + bleed. Without this, the audit's size/DPI checks
    # had no way to tell a cover from an interior page and always computed
    # "expected size" as if it were an interior file, which produces a false
    # "wrong size" / "resolution too low" verdict on essentially every real
    # cover (its actual size legitimately includes spine width that the
    # interior-only formula never accounted for).
    file_type: str = "interior"  # "interior" or "cover"
    binding: str = "paperback"
    page_count: int = 0
    paper_type: str = "white_50lb"
    spine_width: Optional[float] = None   # a hardcover's spine from the distributor's cover template


class AuditCheckoutIn(BaseModel):
    origin_url: str
    level: Literal["standard", "advanced"] = "standard"


@api_router.post("/audit/start")
async def audit_start(payload: AuditStart):
    if payload.platform not in PLATFORMS:
        raise HTTPException(400, "Invalid platform")
    if payload.trim_size not in TRIM_SIZES:
        raise HTTPException(400, "Invalid trim size")
    if payload.file_type not in ("interior", "cover"):
        raise HTTPException(400, "file_type must be 'interior' or 'cover'")
    if payload.file_type == "cover" and payload.binding not in BINDING_TYPES:
        raise HTTPException(400, "Invalid binding")
    if payload.file_type == "cover" and _spine_number_needed(payload.platform, payload.binding, payload.page_count,
                                                             payload.paper_type, payload.spine_width):
        raise HTTPException(400, _spine_needed_message(PLATFORMS[payload.platform]["name"]))
    audit_id = uuid.uuid4().hex
    doc = {
        "audit_id": audit_id,
        "platform": payload.platform,
        "trim_size": payload.trim_size,
        "file_type": payload.file_type,
        "binding": payload.binding,
        "page_count": payload.page_count,
        "paper_type": payload.paper_type,
        "spine_width_override": payload.spine_width if payload.file_type == "cover" else None,
        "file_id": None,
        "file_metadata": None,
        "preview_findings": None,
        "full_findings": None,
        "summary": None,
        "detected_spec": None,
        "paid": False,
        "session_id": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.audits.insert_one(doc)
    return {"audit_id": audit_id}


@api_router.post("/audit/{audit_id}/template-upload")
async def audit_template_upload(audit_id: str, file: UploadFile = File(...)):
    """Lets a person upload their publisher's own blank template PDF (the
    kind IngramSpark/KDP provide as a design guide) right at the start of
    the no-signup audit flow, so their file gets checked against the
    real, current trim/spine/bleed numbers read out of that template --
    instead of a static best-guess preset selected from a dropdown.
    """
    a = await db.audits.find_one({"audit_id": audit_id})
    if not a:
        raise HTTPException(404, "Audit not found -- start an audit first.")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload the publisher's template as a PDF.")

    file_id = f"{audit_id}_tmpl_{uuid.uuid4().hex[:8]}.pdf"
    file_path = UPLOAD_DIR / file_id
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(await file.read())

    try:
        detected_spec = interpret_publisher_template(file_id, file_path, file.filename)
    except Exception as e:
        logging.exception("template interpretation failed for %s", file_id)
        raise HTTPException(400, f"Could not read this template: {e}")

    await db.audits.update_one(
        {"audit_id": audit_id},
        {"$set": {"detected_spec": detected_spec, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"detected_spec": detected_spec}


def _audit_file_findings(file_path: str, metadata: dict, *, platform: str, trim_size: str, file_type: str,
                         binding: str = "paperback", page_count: int = 0, paper_type: str = "white_50lb",
                         spine_width_override: Optional[float] = None, piece: Optional[str] = None,
                         max_pages: int = BASIC_CHECK_MAX_PAGES) -> list:
    """THE audit (owner's rule: one defined audit). Both the no-account /audit page and the Editor's
    "See My Results" run exactly this: what failed, where, why, and the publisher requirement.
    file_type "cover" is a full wrap for `binding`; `piece` ("front_cover"/"back_cover"/"spine") audits one
    separately uploaded paperback piece against its own size. Interiors are checked on page 1
    (BASIC_CHECK_MAX_PAGES) -- every page is the book's Advanced Interior Check."""
    trim = TRIM_SIZES[trim_size]
    plat = PLATFORMS[platform]
    spine_unknown = file_type == "cover" and not piece and _spine_number_needed(
        platform, binding, page_count, paper_type, spine_width_override)
    if file_type == "cover":
        paper = PAPER_TYPES.get(paper_type, PAPER_TYPES["white_50lb"])
        spine_w = calculate_spine_width_for_platform(page_count or 0, paper_ppi(paper, platform), platform, binding)[0]
        if spine_width_override:
            spine_w = float(spine_width_override)
        bleed = resolve_binding_spec(binding, platform)["bleed"]
        full = calculate_full_cover_dimensions(trim["w"], trim["h"], spine_w, bleed, binding, platform)
        if piece:
            h = full["panel_height"] + 2 * bleed
            w = full["spine_width"] if piece == "spine" else full["panel_width"] + 2 * bleed
            findings = deep_audit(metadata, w, h, bleed, plat["name"], is_cover=True,
                                  shape_note=f"{piece.replace('_', ' ')} piece, with bleed")
        else:
            shape_note = f"front + back + {fmt_in(spine_w)}\" spine (binding: {BINDING_TYPES[binding]['label']}), plus bleed"
            findings = deep_audit(metadata, full["total_width"], full["total_height"], bleed, plat["name"],
                                  is_cover=True, shape_note=shape_note)
            if spine_unknown:
                # Size, resolution-for-size and spine position all depend on the real spine width.
                findings = [{
                    "id": "spine_width_needed", "severity": "fail", "title": "Spine width needed",
                    "why_it_fails": _spine_needed_message(plat["name"]),
                    "publisher_rule": f"{plat['name']} — the cover must match the spine width on your cover template",
                    "pinpoint": {"region": "spine"},
                }] + [f for f in findings if f["id"] not in ("bleed_dimension_mismatch", "resolution_too_low", "resolution_marginal")]
            else:
                findings += check_cover_safety_margins(
                    str(file_path), metadata.get("is_pdf", False), full["total_width"], full["total_height"], plat["name"],
                    spine_x_in=full["spine_x"], spine_w_in=full["spine_width"], page_count=page_count, binding=binding,
                    bleed_in=full["bleed"],
                )
    else:
        bleed = plat["bleed"]
        findings = deep_audit(metadata, trim["w"] + bleed * 2, trim["h"] + bleed * 2, bleed, plat["name"],
                              pdf_size_checked_elsewhere=bool(metadata.get("is_pdf")))
        # deep_audit() only sees file-level metadata, never where text sits on the page -- so this adds the
        # "content outside the safety area" check (the most common real rejection): page 1 for the standard
        # audit, every page (up to ADVANCED_AUDIT_MAX_PAGES) for the Advanced Audit.
        if metadata.get("is_pdf"):
            findings += check_interior_safety_margins(str(file_path), plat["name"], trim["w"], trim["h"], max_pages=max_pages,
                                                      bleed_in=bleed, all_sides_bleed_ok=(platform == "lulu"))
    tac_finding = check_total_ink_coverage(str(file_path), metadata.get("is_pdf", False), platform, plat["name"])
    if tac_finding:
        findings.append(tac_finding)
    # Structural checks that need the actual PDF: PDF/X-1a declaration, live transparency, layers,
    # embedded fonts, ICC output intent.
    if metadata.get("is_pdf"):
        findings += run_pdf_structure_audit(str(file_path), plat["name"], max_pages=max_pages)
    return findings


_EDITOR_AUDIT_PARTS = (("full_wrap", "Cover", None), ("front_cover", "Front cover", "front_cover"),
                       ("spine", "Spine", "spine"), ("back_cover", "Back cover", "back_cover"),
                       ("case_wrap", "Case cover", None), ("interior", "Interior", None))


def _editor_audit_findings(p: dict, max_pages: int = BASIC_CHECK_MAX_PAGES) -> list:
    """The same audit, run over every file in an Editor project (a snapshot taken when the results are
    first viewed after paying). Titles say which file each finding is about."""
    slots = dict(p.get("slots") or {})
    if not slots and p.get("uploaded_file"):                 # a project from before per-slot uploads
        slots["interior" if p.get("project_type") == "interior" else "full_wrap"] = {
            **(p.get("file_metadata") or {}), "stored_filename": p["uploaded_file"]}
    findings = []
    for slot, label, piece in _EDITOR_AUDIT_PARTS:
        meta = slots.get(slot)
        path = UPLOAD_DIR / meta["stored_filename"] if meta and meta.get("stored_filename") else None
        if not path or not path.exists():
            continue
        part = _audit_file_findings(
            str(path), meta, platform=p["platform"], trim_size=p["trim_size"],
            file_type="interior" if slot == "interior" else "cover",
            binding="hardcover_case" if slot == "case_wrap" else p.get("binding", "paperback"),
            page_count=p.get("page_count") or 0, paper_type=p.get("paper_type", "white_50lb"),
            spine_width_override=p.get("spine_width_override"), piece=piece, max_pages=max_pages,
        )
        findings += [{**f, "title": f"{label}: {f['title']}"} for f in part]
    return findings


def _slot_audit_issue_count(p: dict, slot: str, path: str, metadata: dict) -> Optional[int]:
    """How many issues THE audit finds in this one uploaded file (page-1 level) -- the free scan's count."""
    part = next(((label, piece) for s, label, piece in _EDITOR_AUDIT_PARTS if s == slot), None)
    if not part:
        return None
    try:
        return len(_audit_file_findings(
            path, metadata, platform=p["platform"], trim_size=p["trim_size"],
            file_type="interior" if slot == "interior" else "cover",
            binding="hardcover_case" if slot == "case_wrap" else p.get("binding", "paperback"),
            page_count=p.get("page_count") or 0, paper_type=p.get("paper_type", "white_50lb"),
            spine_width_override=p.get("spine_width_override"), piece=part[1],
        ))
    except Exception:  # noqa: BLE001 - the count falls back to the Editor's own checks; never block an upload
        logging.exception("audit issue count failed for slot %s", slot)
        return None


async def _ensure_audit_built(a: dict) -> dict:
    """Builds a paid audit's findings at the level that was paid for, the first time they're opened: an Editor
    audit from the project's files, an Advanced (every-page) upgrade of a no-account audit from its stored upload.
    A no-account standard audit was already built at upload. The result is kept as that audit's record."""
    if not a.get("paid"):
        return a
    level = a.get("level") or "standard"
    built = a.get("findings_level") or ("standard" if a.get("full_findings") is not None else None)
    if built == level:
        return a
    max_pages = book_pass.ADVANCED_AUDIT_MAX_PAGES if level == "advanced" else BASIC_CHECK_MAX_PAGES
    update = {}
    if a.get("source") == "editor":
        p = await db.projects.find_one({"_id": ObjectId(a["project_id"])})
        if not p:
            return a
        findings = await run_with_timeout(_editor_audit_findings, p, max_pages)
        update["file_metadata"] = {"original_filename": p.get("name") or "Your book"}
    else:
        path = UPLOAD_DIR / a["file_id"] if a.get("file_id") else None
        if not path or not path.exists():
            return a                                         # upload already deleted: keep the results it has
        findings = await run_with_timeout(
            _audit_file_findings, str(path), a.get("file_metadata") or {}, platform=a["platform"],
            trim_size=a["trim_size"], file_type=a.get("file_type") or "interior", binding=a.get("binding") or "paperback",
            page_count=a.get("page_count") or 0, paper_type=a.get("paper_type") or "white_50lb",
            spine_width_override=a.get("spine_width_override"), max_pages=max_pages,
        )
    update.update({"full_findings": findings, "summary": audit_summary(findings), "findings_level": level,
                   "built_at": datetime.now(timezone.utc).isoformat()})
    await db.audits.update_one({"audit_id": a["audit_id"]}, {"$set": update})
    return {**a, **update}


@api_router.post("/audit/{audit_id}/upload")
async def audit_upload(audit_id: str, file: UploadFile = File(...)):
    a = await db.audits.find_one({"audit_id": audit_id})
    if not a:
        raise HTTPException(404, "Audit not found")
    ext = Path(file.filename).suffix.lower()
    if ext not in {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}:
        raise HTTPException(400, "Unsupported file type")

    max_mb = 50
    file_id = f"audit_{audit_id}_{uuid.uuid4().hex[:6]}{ext}"
    file_path = UPLOAD_DIR / file_id
    size = 0
    with open(file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_mb * 1024 * 1024:
                f.close()
                os.remove(file_path)
                raise HTTPException(413, f"File exceeds {max_mb}MB limit for audit")
            f.write(chunk)

    def _run_audit_checks():
        metadata = analyze_file(str(file_path))
        metadata["original_filename"] = file.filename
        metadata["stored_filename"] = file_id
        findings = _audit_file_findings(
            str(file_path), metadata, platform=a["platform"], trim_size=a["trim_size"],
            file_type=a.get("file_type", "interior"), binding=a.get("binding") or "paperback",
            page_count=a.get("page_count") or 0, paper_type=a.get("paper_type") or "white_50lb",
            spine_width_override=a.get("spine_width_override"),
        )
        return metadata, findings

    # The whole synchronous check sequence above (OCR, PDF structure audit,
    # ink-coverage scan) runs in one worker thread under one time budget --
    # see run_with_timeout's docstring for why a bare asyncio.wait_for
    # around this wouldn't actually enforce anything.
    metadata, findings = await run_with_timeout(_run_audit_checks)
    summary = audit_summary(findings)

    await db.audits.update_one(
        {"audit_id": audit_id},
        {"$set": {
            "file_id": file_id,
            "file_metadata": metadata,
            "full_findings": findings,
            "summary": summary,
        }},
    )
    return {"audit_id": audit_id, "summary": _audit_unpaid_summary(summary), "check_type": "basic"}


@api_router.get("/audit/{audit_id}/report")
async def audit_download_report(audit_id: str):
    """Generates and returns a real downloadable PDF audit report -- the
    actual export artifact, distinct from the AuditReport.jsx in-app page.
    Gated behind the same one-time $0.99 unlock as the rest of the full
    report (matches the `paid` check used for full_report elsewhere) --
    the preview stays free, but the downloadable report is a paid unlock,
    not a free bypass of it.
    """
    a = await db.audits.find_one({"audit_id": audit_id})
    if a:
        a = await _ensure_audit_built(a)
    if not a or a.get("full_findings") is None:
        raise HTTPException(404, "Audit not found or not yet run")
    if not a.get("paid"):
        raise HTTPException(402, "Unlock the full report ($1.99) to download the PDF.")

    report_path = UPLOAD_DIR.parent / "exports" / f"{audit_id}_report.pdf"
    generate_audit_brief_pdf(
        findings=a["full_findings"],
        summary=a.get("summary") or {},
        project_meta={
            "title": (a.get("file_metadata") or {}).get("original_filename", "Untitled"),
            "platform": PLATFORMS.get(a["platform"], {}).get("name", a.get("platform", "")),
            "trim_size": a.get("trim_size", ""),
            "audit_label": ("SparkPrep Advanced Audit — every interior page checked"
                            if (a.get("level") or "standard") == "advanced" else
                            "SparkPrep Audit" + (" — page 1 of the interior" if a.get("file_type") in ("interior", "combined") else "")),
        },
        output_path=str(report_path),
    )
    # Owner's rule: a customer's audit files are deleted as soon as they download their report. The report is
    # built from the stored findings, never from the upload, so re-downloading and the on-screen report keep
    # working; the delete runs only after the PDF has finished sending.
    if not a.get("files_deleted_at"):
        await db.audits.update_one({"audit_id": audit_id}, {"$set": {"files_deleted_at": datetime.now(timezone.utc).isoformat()}})
    return FileResponse(
        str(report_path),
        media_type="application/pdf",
        filename=f"sparkprep-audit-report-{audit_id}.pdf",
        background=BackgroundTask(_delete_audit_files, audit_id, report_path),
    )


def _delete_audit_files(audit_id: str, report_path: Path) -> int:
    """The audit's upload, any distributor template sent with it, and the generated report PDF."""
    if not re.fullmatch(r"[0-9a-f]{32}", audit_id or ""):
        return 0
    paths = [*UPLOAD_DIR.glob(f"audit_{audit_id}_*"), *UPLOAD_DIR.glob(f"{audit_id}_tmpl_*"), Path(report_path)]
    removed = 0
    for p in paths:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# Owner's rule: the audit detects -- what failed, where, why, and the publisher requirement. It never
# hands out a repair tutorial (step lists, tool lists, fix times); doing the work is SparkPrep's main
# service. Stripped here so it can't be read out of the network response either.
_AUDIT_REPAIR_KEYS = ("fix_steps", "fix_tools", "est_fix_minutes")


def _audit_public_findings(findings: Optional[list]) -> Optional[list]:
    if findings is None:
        return None
    return [{k: v for k, v in f.items() if k not in _AUDIT_REPAIR_KEYS} for f in findings]


def _audit_public_summary(summary: Optional[dict]) -> Optional[dict]:
    return {k: v for k, v in summary.items() if k != "estimated_fix_minutes"} if summary else summary


def _audit_unpaid_summary(summary: Optional[dict]) -> Optional[dict]:
    """Owner's rule: no free preview. Before paying, only the honest total leaves the server -- not which
    issues, not their severity, not the rejection risk (the same view the Editor's free scan gives)."""
    return {"total_issues": summary.get("total_issues", 0)} if summary else summary


@api_router.get("/audit/{audit_id}")
async def audit_get(audit_id: str):
    a = await db.audits.find_one({"audit_id": audit_id})
    if not a:
        raise HTTPException(404, "Audit not found")
    a = await _ensure_audit_built(a)
    trim = TRIM_SIZES.get(a["trim_size"])
    plat = PLATFORMS.get(a["platform"])
    paid = a.get("paid", False)
    return {
        "audit_id": a["audit_id"],
        "platform": a["platform"],
        "platform_name": plat["name"] if plat else a["platform"],
        "trim_size": a["trim_size"],
        "trim_label": trim["label"] if trim else a["trim_size"],
        "file_type": a.get("file_type"),
        "file_metadata": a.get("file_metadata") if paid else _strip_file_facts(a.get("file_metadata")),
        "summary": _audit_public_summary(a.get("summary")) if paid else _audit_unpaid_summary(a.get("summary")),
        "paid": paid,
        "level": a.get("level") or "standard",
        "can_upgrade": _audit_can_upgrade(a),
        # what this audit is worth toward a book: everything paid for it, until it's been used on one
        "credit_cents": 0 if not paid or a.get("credit_consumed") else await _audit_paid_cents(a["audit_id"]),
        "full_report": _audit_public_findings(a.get("full_findings")) if paid else None,
    }


def _audit_can_upgrade(a: dict) -> bool:
    """A paid standard audit of an interior can become an Advanced (every-page) Audit for the difference --
    while its file still exists (a no-account audit's upload is deleted once its report is downloaded)."""
    return bool(a.get("paid") and (a.get("level") or "standard") == "standard" and book_pass.book_pass_on()
                and a.get("file_type") in ("interior", "combined")
                and (a.get("source") == "editor" or not a.get("files_deleted_at")))


async def _audit_paid_cents(audit_id: str) -> int:
    total = 0
    async for t in db.payment_transactions.find({"audit_id": audit_id, "product": "audit_099", "payment_status": "paid"}):
        total += int(t.get("amount") or 0)
    return total


async def _start_audit_checkout(a: dict, level: str, *, origin: str, success_url: str, cancel_url: str,
                                email: Optional[str] = None, extra_tx: Optional[dict] = None) -> dict:
    """The one checkout for both audits (the no-account /audit and the Editor's "See My Results"): the standard
    audit, the Advanced (every-page) Audit, or upgrading a paid standard audit to Advanced for the difference.
    Prices come from book_pass config (99-Day Special until it ends, then regular). Every payment is an
    audit_099 record, so the webhook, verify and the credit toward the book work unchanged."""
    current = a.get("level") or "standard"
    if a.get("paid") and (level == "standard" or current == "advanced"):
        return {"already_paid": True}
    if level == "advanced":
        if not book_pass.book_pass_on():
            raise HTTPException(400, "The Advanced Audit isn't available right now.")
        if a.get("file_type") not in ("interior", "combined"):
            raise HTTPException(400, "The Advanced Audit checks every page of an interior — a cover is fully checked by the standard audit.")
        if a.get("paid") and not _audit_can_upgrade(a):
            raise HTTPException(400, "This audit's file was deleted after its report was downloaded — start a new audit and choose Advanced to check every page.")
    if not stripe.api_key or stripe.api_key in ("sk_test_not_configured", ""):
        raise HTTPException(503, "Payments not configured — Stripe key missing")

    upgrading = bool(a.get("paid"))
    price = book_pass.audit_level_price_cents(level, AUDIT_PRICE_CENTS)
    amount = max(price - (await _audit_paid_cents(a["audit_id"]) if upgrading else 0), 50)
    advanced = level == "advanced"
    name = f"SparkPrep Advanced Audit — every page{' (upgrade)' if upgrading else ''}" if advanced else "SparkPrep Audit"
    desc = (f"Every interior page (up to {book_pass.ADVANCED_AUDIT_MAX_PAGES}) checked: what failed, where, and the publisher requirement."
            if advanced else "Every issue pinpointed: what failed, where, and the publisher requirement it breaks.")
    if upgrading:
        desc += " You pay only the difference from your standard audit."
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price_data": {"currency": "usd", "product_data": {"name": name, "description": desc},
                                        "unit_amount": amount}, "quantity": 1}],
            success_url=success_url, cancel_url=cancel_url,
            metadata={"audit_id": a["audit_id"], "level": level, "purpose": "print_failure_audit"},
            **({"customer_email": email} if email else {}),
        )
    except stripe.error.StripeError as e:
        raise HTTPException(500, f"Stripe error: {e}")
    await db.audits.update_one({"audit_id": a["audit_id"]}, {"$set": {"session_id": session.id}})
    await db.payment_transactions.insert_one({
        "session_id": session.id, "audit_id": a["audit_id"], "amount": amount, "level": level,
        "currency": "usd", "status": "initiated", "payment_status": "pending", "product": "audit_099",
        "created_at": datetime.now(timezone.utc).isoformat(), **(extra_tx or {}),
    })
    return {"checkout_url": session.url, "session_id": session.id, "audit_id": a["audit_id"], "amount_cents": amount}


async def _mark_audit_paid(audit_id: str, level: Optional[str]):
    update = {"paid": True, "paid_at": datetime.now(timezone.utc).isoformat()}
    if level == "advanced":
        update["level"] = "advanced"
    await db.audits.update_one({"audit_id": audit_id}, {"$set": update})


@api_router.post("/audit/{audit_id}/checkout")
async def audit_checkout(audit_id: str, payload: AuditCheckoutIn):
    a = await db.audits.find_one({"audit_id": audit_id})
    if not a:
        raise HTTPException(404, "Audit not found")
    origin = book_pass.purchases.safe_origin(payload.origin_url)
    return await _start_audit_checkout(
        a, payload.level, origin=origin,
        success_url=f"{origin}/audit/{audit_id}/report?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{origin}/audit/{audit_id}/{'report' if a.get('paid') else 'preview'}",
    )


@api_router.get("/audit/{audit_id}/verify")
async def audit_verify(audit_id: str, session_id: str):
    """Confirms THIS checkout session (not just "is the audit paid") -- an upgrade to Advanced is a second
    payment on an audit that's already paid, and must still be recorded."""
    a = await db.audits.find_one({"audit_id": audit_id})
    if not a:
        raise HTTPException(404, "Audit not found")
    tx = await db.payment_transactions.find_one({"session_id": session_id, "audit_id": audit_id})
    if tx and tx.get("payment_status") == "paid":
        return {"paid": True}
    if not tx and a.get("paid"):
        return {"paid": True}
    try:
        s = stripe.checkout.Session.retrieve(session_id)
        if s.payment_status == "paid" or s.status == "complete":
            await _mark_audit_paid(audit_id, (tx or {}).get("level"))
            await db.payment_transactions.update_one(
                {"session_id": session_id, "payment_status": {"$ne": "paid"}},
                {"$set": {"status": "completed", "payment_status": "paid"}},
            )
            return {"paid": True}
        return {"paid": False, "status": s.payment_status}
    except stripe.error.StripeError as e:
        raise HTTPException(500, f"Stripe verify failed: {e}")


class ResultsUnlockIn(BaseModel):
    origin_url: str
    level: Literal["standard", "advanced"] = "standard"


@api_router.post("/projects/{project_id}/results-unlock/checkout")
async def results_unlock_checkout(project_id: str, payload: ResultsUnlockIn, user: dict = Depends(get_current_user)):
    """"See My Results — $1.99" in the Editor. It's the same audit product as the no-account audit (one price
    constant, the audit_099 payment record, the same webhook/verify), just linked to this project -- so paying
    unlocks this project's full results, and the audit credit toward the book works exactly as it already does.
    Results only: repairs stay with the book (see _require_product)."""
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    if await _owns_product(p, user):
        return {"already_unlocked": True}                   # the book includes results and the every-page check
    if payload.level == "standard" and await _results_unlocked(p, user):
        return {"already_unlocked": True}
    if not any((s.get("compliance") for s in (p.get("slots") or {}).values())) and not p.get("compliance"):
        raise HTTPException(400, "Upload your file first — the audit shows what the scan found in it.")

    aid = p.get("results_audit_id")
    a = await db.audits.find_one({"audit_id": aid}) if aid else None
    if not a:
        aid = uuid.uuid4().hex
        a = {
            "audit_id": aid, "source": "editor", "project_id": project_id, "user_id": user["id"],
            "platform": p.get("platform"), "trim_size": p.get("trim_size"), "file_type": p.get("project_type"),
            "file_id": None, "file_metadata": None, "full_findings": None, "summary": None,
            "paid": False, "session_id": None, "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.audits.insert_one(dict(a))
        await db.projects.update_one({"_id": p["_id"]}, {"$set": {"results_audit_id": aid}})

    origin = book_pass.purchases.safe_origin(payload.origin_url)
    result = await _start_audit_checkout(
        a, payload.level, origin=origin, email=user.get("email"),
        success_url=f"{origin}/editor/{project_id}?results_session={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{origin}/editor/{project_id}",
        extra_tx={"project_id": project_id, "user_id": user["id"]},
    )
    if result.get("already_paid"):
        return {"already_unlocked": True}
    return result


# =====================================================================
# BETA PROGRAM
# =====================================================================
class BetaRedeemIn(BaseModel):
    code: str
    email: EmailStr
    password: str = Field(min_length=8)
    name: Optional[str] = None


class BetaFeedbackChecklistItem(BaseModel):
    key: str
    label: str
    status: str  # "worked" | "didnt_work" | "not_tested"
    notes: Optional[str] = ""


class BetaFeedbackIn(BaseModel):
    checklist: List[BetaFeedbackChecklistItem]
    critical_review: str = ""
    public_review: str = ""
    would_recommend: bool = False


class BetaGenerateIn(BaseModel):
    count: int = 10
    note: Optional[str] = ""


@api_router.get("/beta/checklist")
async def beta_default_checklist():
    """Public — returns the default sign-off checklist so the form can render before login."""
    return {"features": DEFAULT_CHECKLIST_FEATURES}


@api_router.post("/beta/redeem")
async def beta_redeem(payload: BetaRedeemIn, response: Response):
    """Single-use redemption: validate the code, create or upgrade the user, mark beta active.
    Follows the exact same signup flow real users go through — code just unlocks the grant."""
    code = payload.code.strip().upper()
    pass_doc = await db.beta_passes.find_one({"code": code})
    if not pass_doc:
        raise HTTPException(404, "Invalid beta code — please double-check with the sender.")
    if pass_doc.get("status") == "revoked":
        raise HTTPException(410, "This beta code has been revoked.")
    if pass_doc.get("status") in ("active", "consumed"):
        raise HTTPException(409, "This beta code has already been redeemed.")

    email = payload.email.lower()
    existing = await db.users.find_one({"email": email})
    now = datetime.now(timezone.utc)

    if existing:
        # Existing account — apply beta grant on top
        if existing.get("beta_active"):
            raise HTTPException(400, "Your account already has an active beta pass.")
        await db.users.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "beta_active": True,
                "beta_pass_code": code,
                "beta_activated_at": now.isoformat(),
            }},
        )
        uid = str(existing["_id"])
    else:
        doc = {
            "email": email,
            "password_hash": hash_password(payload.password),
            "name": payload.name or email.split("@")[0],
            "tier": "free",
            "stripe_customer_id": None,
            "subscription_status": None,
            "exports_this_month": 0,
            "books_this_month": 0,
            "billing_period_start": now.isoformat(),
            "created_at": now.isoformat(),
            "beta_active": True,
            "beta_pass_code": code,
            "beta_activated_at": now.isoformat(),
        }
        result = await db.users.insert_one(doc)
        uid = str(result.inserted_id)

    await db.beta_passes.update_one(
        {"code": code},
        {"$set": {
            "status": "active",
            "redeemed_by_user_id": uid,
            "redeemed_by_email": email,
            "redeemed_at": now.isoformat(),
        }},
    )

    token = create_access_token(uid, email)
    set_auth_cookie(response, token)
    user = await db.users.find_one({"_id": ObjectId(uid)})
    user["id"] = uid
    user.pop("_id", None)
    user.pop("password_hash", None)
    return {"user": enrich_user(user), "token": token}


@api_router.get("/beta/status")
async def beta_status(user: dict = Depends(get_current_user)):
    """Return whether this user has an active beta grant + the checklist to sign off with."""
    return {
        "beta_active": bool(user.get("beta_active")),
        "beta_pass_code": user.get("beta_pass_code"),
        "beta_activated_at": user.get("beta_activated_at"),
        "checklist_template": DEFAULT_CHECKLIST_FEATURES,
        "feedback_submitted": bool(user.get("beta_feedback_submitted_at")),
    }


@api_router.post("/beta/feedback")
async def beta_submit_feedback(payload: BetaFeedbackIn, user: dict = Depends(get_current_user)):
    """Tester submits their sign-off. Burns their beta pass, records feedback."""
    if user.get("beta_feedback_submitted_at"):
        raise HTTPException(400, "You've already submitted feedback for this beta pass.")

    checklist = [item.model_dump() for item in payload.checklist]
    doc = new_feedback_doc(
        user_id=user["id"],
        user_email=user["email"],
        pass_code=user.get("beta_pass_code"),
        payload={
            "checklist": checklist,
            "critical_review": payload.critical_review,
            "public_review": payload.public_review,
            "would_recommend": payload.would_recommend,
        },
    )
    result = await db.beta_feedback.insert_one(doc)

    now = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"_id": ObjectId(user["id"])},
        {"$set": {
            "beta_active": False,
            "beta_feedback_submitted_at": now,
        }},
    )
    if user.get("beta_pass_code"):
        await db.beta_passes.update_one(
            {"code": user["beta_pass_code"]},
            {"$set": {
                "status": "consumed",
                "feedback_submitted_at": now,
                "feedback_id": str(result.inserted_id),
            }},
        )
    return {"ok": True, "feedback_id": str(result.inserted_id)}


# ---- Admin-only ----
@api_router.get("/admin/failures")
async def admin_list_failures(stage: str = None, limit: int = 100, _: dict = Depends(require_admin)):
    """Read-only view of the failure_log collection (see failure_log.py) --
    every processing crash (upload analysis, autofix, export, template
    detection) with its stage, error, traceback and the project/user it
    happened to, most recent first. Filter by `stage` to isolate one
    failure point (e.g. ?stage=export_build_pdf) while debugging a
    specific report from a user."""
    limit = max(1, min(500, limit))
    query = {"stage": stage} if stage else {}
    cursor = db.failure_log.find(query).sort("timestamp", -1).limit(limit)
    items = []
    async for f in cursor:
        f["id"] = str(f.pop("_id"))
        items.append(f)
    return {"failures": items, "count": len(items)}


# Owner's rule (2026-09-29): every issue SparkPrep can't fix is kept as an "unsolved case" to study, until
# SparkPrep has no issue it can't fix. DETAILS ONLY -- never the customer's file: the book's specs, what
# went wrong and why, and file facts that carry none of its content (format, size, resolution, color type).
_CASE_FILE_FACTS = ("format", "is_pdf", "width_px", "height_px", "dpi_x", "dpi_y", "color_mode",
                    "has_transparency", "pdf_pages", "file_size", "size_bytes")


async def _record_unsolved_case(project_id: str, user_id: str, source: str, slots: list, open_items: list) -> None:
    """Keeps the details of an issue SparkPrep couldn't fix. The same project + same open issues is one
    case (seen again -> its count goes up)."""
    if not open_items:
        return
    p = await db.projects.find_one({"_id": ObjectId(project_id)})
    if not p:
        return
    key = f"{project_id}|{'/'.join(sorted(slots))}|{'/'.join(sorted(o.get('title', '') for o in open_items))}"
    now = datetime.now(timezone.utc).isoformat()
    existing = await db.unsolved_cases.find_one({"key": key, "solved": False})
    if existing:
        await db.unsolved_cases.update_one({"_id": existing["_id"]}, {"$set": {"last_seen": now}, "$inc": {"times_seen": 1}})
        return
    file_facts = {}
    for slot in slots:
        meta = (p.get("slots") or {}).get(slot) or {}
        facts = {k: meta[k] for k in _CASE_FILE_FACTS if meta.get(k) is not None}
        if facts:
            file_facts[slot] = facts
    await db.unsolved_cases.insert_one({
        "case_id": uuid.uuid4().hex[:12], "key": key, "project_id": project_id, "user_id": user_id, "source": source,
        "platform": p.get("platform"), "trim_size": p.get("trim_size"), "binding": p.get("binding"),
        "paper_type": p.get("paper_type"), "page_count": p.get("page_count"),
        "spine_width_override": p.get("spine_width_override"), "slots": slots, "file_facts": file_facts,
        "open": [{"title": o.get("title"), "why": o.get("why", ""), "id": o.get("id")} for o in open_items],
        "created_at": now, "last_seen": now, "times_seen": 1, "solved": False,
    })


@api_router.get("/admin/unsolved-cases")
async def admin_unsolved_cases(include_solved: bool = False, limit: int = 200, _: dict = Depends(require_admin)):
    """Every issue SparkPrep couldn't fix, newest first -- the to-do list for "no issue SparkPrep can't fix"."""
    query = {} if include_solved else {"solved": False}
    items = []
    async for c in db.unsolved_cases.find(query).sort("last_seen", -1).limit(max(1, min(500, limit))):
        c.pop("_id", None)
        items.append(c)
    return {"cases": items, "count": len(items)}


@api_router.post("/admin/unsolved-cases/{case_id}/solved")
async def admin_mark_case_solved(case_id: str, _: dict = Depends(require_admin)):
    """Once SparkPrep can fix this kind of issue, mark it solved (the case stays as a record)."""
    r = await db.unsolved_cases.update_one({"case_id": case_id}, {"$set": {"solved": True,
                                           "solved_at": datetime.now(timezone.utc).isoformat()}})
    if not r.matched_count:
        raise HTTPException(404, "Case not found")
    return {"ok": True}


@api_router.get("/admin/book-flags")
async def admin_book_flags(limit: int = 100, _: dict = Depends(require_admin)):
    """Exports the same-book check blocked ("different") or let through while unsure ("unclear"), newest first."""
    limit = max(1, min(500, limit))
    items = []
    async for f in db.book_flags.find({}).sort("created_at", -1).limit(limit):
        f["id"] = str(f.pop("_id"))
        u = await db.users.find_one({"_id": ObjectId(f["user_id"])}) if ObjectId.is_valid(f.get("user_id", "")) else None
        f["user_email"] = u.get("email") if u else None
        items.append(f)
    return {"flags": items, "count": len(items)}


class RebaselineIn(BaseModel):
    project_id: str


@api_router.post("/admin/book-flags/rebaseline")
async def admin_rebaseline_book(payload: RebaselineIn, _: dict = Depends(require_admin)):
    """Support override: "yes, it's the same book". Clears the recorded fingerprint on that project's book so its
    next export becomes the new baseline, and marks its flags reviewed."""
    now = datetime.now(timezone.utc)
    active = None
    async for c in db.book_credits.find({"project_id": payload.project_id}):
        if book_pass.entitlements.credit_state(c, now) == "active":
            active = c
    if not active:
        raise HTTPException(404, "No active book on that project")
    await db.book_credits.update_one({"credit_id": active["credit_id"]}, {"$set": {"fingerprint": None}})
    await db.book_flags.update_many({"project_id": payload.project_id}, {"$set": {"reviewed": True}})
    return {"ok": True}


@api_router.get("/admin/beta/passes")
async def admin_list_passes(_: dict = Depends(require_admin)):
    cursor = db.beta_passes.find().sort("created_at", -1)
    items = []
    async for p in cursor:
        p["id"] = str(p.pop("_id"))
        items.append(p)
    return {"passes": items}


@api_router.post("/admin/beta/generate")
async def admin_generate_passes(payload: BetaGenerateIn, admin: dict = Depends(require_admin)):
    count = max(1, min(200, int(payload.count)))
    docs = [new_pass_doc(admin["email"], payload.note or "") for _ in range(count)]
    await db.beta_passes.insert_many(docs)
    return {
        "generated": count,
        "codes": [d["code"] for d in docs],
    }


@api_router.get("/admin/beta/feedback")
async def admin_list_feedback(_: dict = Depends(require_admin)):
    cursor = db.beta_feedback.find().sort("submitted_at", -1)
    items = []
    async for fb in cursor:
        fb["id"] = str(fb.pop("_id"))
        items.append(fb)
    return {"feedback": items}


@api_router.post("/admin/beta/revoke/{code}")
async def admin_revoke_pass(code: str, _: dict = Depends(require_admin)):
    result = await db.beta_passes.update_one(
        {"code": code.upper()},
        {"$set": {"status": "revoked", "revoked_at": datetime.now(timezone.utc).isoformat()}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Code not found")
    return {"ok": True}



async def _find_owned_project(project_id: str, user: dict):
    try:
        return await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    except Exception:  # noqa: BLE001 - malformed id
        return None


api_router.include_router(book_pass.build_router(db=db, get_current_user=get_current_user, stripe=stripe,
                                                 find_project=_find_owned_project))
# ---- Database backups (owner's rule, 2026-09-29: no lockout or accident can ever cost SparkPrep its customers)
# Every collection, exported to one gzip'd JSON file (Mongo-exact types via bson.json_util, so a restore brings
# back real ObjectIds and dates). Nightly copies stay on the server (last BACKUP_KEEP); the admin page's
# "Download backup" gives the owner a copy for a USB drive at home. tools/restore_backup.py puts one back.
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)
BACKUP_KEEP = 7
BACKUP_FORMAT = "sparkprep-backup-v1"


async def _collection_names() -> list:
    if isinstance(db, MemoryDatabase):
        fixed = [k for k, v in vars(db).items() if isinstance(v, MemoryCollection)]
        return sorted(set(fixed) | set(db._dynamic))
    return sorted(n for n in await db.list_collection_names() if not n.startswith("system."))


async def _export_database() -> tuple:
    """-> (gzip bytes, {collection: document count})."""
    import gzip
    from bson import json_util
    collections, counts = {}, {}
    for name in await _collection_names():
        docs = [d async for d in getattr(db, name).find({})]
        collections[name] = docs
        counts[name] = len(docs)
    payload = {"format": BACKUP_FORMAT, "exported_at": datetime.now(timezone.utc).isoformat(), "counts": counts,
               "collections": collections}
    return gzip.compress(json_util.dumps(payload).encode("utf-8")), counts


async def _write_nightly_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)   # recreate it if anything ever removed it
    data, counts = await _export_database()
    path = BACKUP_DIR / f"sparkprep-backup-{datetime.now(timezone.utc):%Y-%m-%d}.json.gz"
    path.write_bytes(data)
    for old in sorted(BACKUP_DIR.glob("sparkprep-backup-*.json.gz"))[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    logger.info("backup written: %s (%d bytes, %s)", path.name, len(data), counts)
    return path


async def _nightly_backup_loop():
    while True:
        try:
            today = BACKUP_DIR / f"sparkprep-backup-{datetime.now(timezone.utc):%Y-%m-%d}.json.gz"
            if not today.exists():
                await _write_nightly_backup()
        except Exception as e:                      # a failed backup must never take the site down
            await log_failure(db, "nightly_backup", e)
        await asyncio.sleep(60 * 60)                # check hourly; writes once per day


@api_router.get("/admin/backup")
async def admin_download_backup(_: dict = Depends(require_admin)):
    """A full backup right now, as a file for the owner to keep (USB drive at home)."""
    data, _counts = await _export_database()
    name = f"sparkprep-backup-{datetime.now(timezone.utc):%Y-%m-%d-%H%M}.json.gz"
    return Response(content=data, media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@api_router.get("/admin/backups")
async def admin_list_backups(_: dict = Depends(require_admin)):
    files = sorted(BACKUP_DIR.glob("sparkprep-backup-*.json.gz"), reverse=True)
    return {"backups": [{"name": f.name, "bytes": f.stat().st_size,
                         "created_at": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()} for f in files],
            "keep": BACKUP_KEEP}


@api_router.get("/admin/backups/{name}")
async def admin_download_nightly_backup(name: str, _: dict = Depends(require_admin)):
    path = BACKUP_DIR / name
    if not name.startswith("sparkprep-backup-") or path.parent != BACKUP_DIR or not path.exists():
        raise HTTPException(404, "Backup not found")
    return FileResponse(str(path), filename=name, media_type="application/gzip")


@app.on_event("startup")
async def start_nightly_backups():
    if client is None:                              # local in-memory runs have nothing worth backing up
        return
    task = asyncio.create_task(_nightly_backup_loop())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


app.include_router(api_router)

app.add_middleware(RequestTimeoutMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[
        os.environ.get("FRONTEND_URL", "https://sparkprep.legenddary.com"),
        "https://sparkprepfinal.pages.dev",
        "https://sparkprep-live.pages.dev",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"https://[a-z0-9]+\.sparkprep-live\.pages\.dev",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    if client is None:
        logger.info("Using in-memory database for local development startup")
        return

    await db.users.create_index("email", unique=True)
    await db.projects.create_index("user_id")
    await db.payment_transactions.create_index("session_id", unique=True)
    await db.beta_passes.create_index("code", unique=True)
    await db.beta_feedback.create_index("user_id")

    # Seed admin account if configured and missing
    if ADMIN_EMAIL:
        existing_admin = await db.users.find_one({"email": ADMIN_EMAIL})
        if not existing_admin:
            seed_pw = os.environ.get("ADMIN_SEED_PASSWORD", "ChangeMe2026!")
            now = datetime.now(timezone.utc)
            await db.users.insert_one({
                "email": ADMIN_EMAIL,
                "password_hash": hash_password(seed_pw),
                "name": "Legenddary Admin",
                "tier": "studio",  # give admin top tier so they can freely test paid flows
                "stripe_customer_id": None,
                "subscription_status": None,
                "exports_this_month": 0,
                "books_this_month": 0,
                "billing_period_start": now.isoformat(),
                "created_at": now.isoformat(),
                "beta_active": False,
            })
            logger.info(f"Admin account seeded: {ADMIN_EMAIL}")
        # Seed the first 10 beta passes if none exist
        pass_count = await db.beta_passes.count_documents({})
        if pass_count == 0:
            docs = [new_pass_doc(ADMIN_EMAIL, "initial batch") for _ in range(10)]
            await db.beta_passes.insert_many(docs)
            logger.info(f"Seeded {len(docs)} beta passes: {[d['code'] for d in docs]}")

    if not os.environ.get("STRIPE_WEBHOOK_SECRET"):
        logger.warning("STRIPE_WEBHOOK_SECRET is not set — webhook accepts unsigned JSON. Do NOT ship to production without it.")
    if stripe.api_key and (stripe.api_key.startswith("sk_live_") or stripe.api_key.startswith("rk_live_")):
        logger.info("Stripe is in LIVE mode.")
    elif stripe.api_key and (stripe.api_key.startswith("sk_test_") or stripe.api_key.startswith("rk_test_")) and stripe.api_key != "sk_test_not_configured":
        logger.info("Stripe is in TEST mode.")
    logger.info("SparkPrep API ready")


@app.on_event("shutdown")
async def on_shutdown():
    if client is not None:
        client.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
