from dotenv import load_dotenv
from pathlib import Path
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import io
import uuid
import asyncio
import logging
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import bcrypt
import jwt
import stripe
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware
try:
    from motor.motor_asyncio import AsyncIOMotorClient
except Exception:  # pragma: no cover - fallback for local/dev environments
    AsyncIOMotorClient = None
from pydantic import BaseModel, EmailStr, Field
from bson import ObjectId

from print_specs import (
    TRIM_SIZES, PAPER_TYPES, BINDING_TYPES, PLATFORMS, COLOR_PROFILES, DEFAULT_COLOR_PROFILE,
    calculate_spine_width, calculate_full_cover_dimensions,
)
from file_processor import (
    analyze_file, compute_effective_dpi, convert_to_cmyk,
    build_print_ready_pdf, build_interior_pdf_x1a, run_compliance_checks,
)
from audit_engine import deep_audit, audit_summary
from template_interpreter_adapter import interpret_publisher_template
from pdfx_validator import run_pdf_structure_audit
from ghostscript_engine import convert_to_pdfx1a, find_ghostscript
from report_export import generate_audit_report_pdf, generate_audit_brief_pdf
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
from cover_template_engine import COVER_TEMPLATES, render_cover_template, list_cover_templates

class MemoryCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=-1):
        self._docs = sorted(self._docs, key=lambda d: d.get(field, "") or "", reverse=direction != 1)
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

    async def find_one(self, filter=None):
        for doc in self._docs:
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

    async def delete_one(self, filter=None):
        before = len(self._docs)
        self._docs = [doc for doc in self._docs if not all(doc.get(key) == value for key, value in (filter or {}).items())]
        return type("DeleteResult", (), {"deleted_count": before - len(self._docs)})()

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

UPLOAD_DIR = ROOT_DIR / "uploads"
EXPORT_DIR = ROOT_DIR / "exports"
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

# 99-Day Audit Season launch (Sept 23 → Dec 31)
AUDIT_SEASON_START = datetime(2026, 9, 23, tzinfo=timezone.utc)
AUDIT_SEASON_END = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


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
    return owner


# ---- Models ----
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
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


@api_router.post("/auth/login")
async def login(payload: LoginIn, response: Response):
    email = payload.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
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
        raise HTTPException(402, f"Team seats aren't included on the {TIERS.get(tier, {}).get('name', tier)} plan. Upgrade to Publisher (3 seats) or Studio (10 seats).")
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
        raise HTTPException(402, "White-label branding is a Studio plan feature.")
    name = (payload.brand_name or "").strip()[:60]
    await db.users.update_one({"_id": ObjectId(user["id"])}, {"$set": {"white_label_brand_name": name or None}})
    return {"ok": True, "white_label_brand_name": name or None}


# ---- Specs ----
@api_router.get("/specs")
async def get_specs():
    return {
        "platforms": PLATFORMS,
        "trim_sizes": TRIM_SIZES,
        "paper_types": PAPER_TYPES,
        "binding_types": BINDING_TYPES,
        "tiers": TIERS,
    }


@api_router.post("/specs/spine")
async def spine_calc(payload: dict):
    page_count = int(payload.get("page_count", 0))
    paper = payload.get("paper_type", "white_50lb")
    paper_info = PAPER_TYPES.get(paper, PAPER_TYPES["white_50lb"])
    spine_w = calculate_spine_width(page_count, paper_info["ppi"])
    trim_key = payload.get("trim_size", "6x9")
    trim = TRIM_SIZES.get(trim_key, TRIM_SIZES["6x9"])
    binding = payload.get("binding", "paperback")
    plat = payload.get("platform", "kdp")
    bleed = PLATFORMS.get(plat, PLATFORMS["kdp"])["bleed"]
    full = calculate_full_cover_dimensions(trim["w"], trim["h"], spine_w, bleed, binding)
    return {
        "spine_width": spine_w,
        "full_cover": full,
        "trim": trim,
        "paper_ppi": paper_info["ppi"],
        "bleed": bleed,
        "spine_text_allowed": page_count >= PLATFORMS.get(plat, PLATFORMS["kdp"])["spine_text_min_pages"],
    }


# ---- Projects ----
def project_to_dict(p: dict) -> dict:
    p = dict(p)
    p["id"] = str(p.pop("_id"))
    p.pop("user_id_obj", None)
    return p


@api_router.get("/projects")
async def list_projects(user: dict = Depends(get_current_user)):
    cursor = db.projects.find({"user_id": user["id"]}).sort("updated_at", -1)
    items = [project_to_dict(p) async for p in cursor]
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
    return project_to_dict(doc)


@api_router.get("/projects/{project_id}")
async def get_project(project_id: str, user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    return project_to_dict(p)


@api_router.patch("/projects/{project_id}")
async def update_project(project_id: str, payload: ProjectUpdate, user: dict = Depends(get_current_user)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = await db.projects.update_one(
        {"_id": ObjectId(project_id), "user_id": user["id"]}, {"$set": updates}
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Project not found")
    p = await db.projects.find_one({"_id": ObjectId(project_id)})
    return project_to_dict(p)


@api_router.delete("/projects/{project_id}")
async def delete_project(project_id: str, user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    if p.get("uploaded_file"):
        try:
            os.remove(UPLOAD_DIR / p["uploaded_file"])
        except OSError:
            pass
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
    return {"file_metadata": metadata, "compliance": compliance, "file_id": file_id}


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
    return FileResponse(str(file_path))


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
    ghostscript_result = None
    if current_metadata.get("is_pdf"):
        # Check what actually needs fixing before touching the file --
        # only PDF/X-1a declaration, live transparency, and layers are
        # things Ghostscript's -dPDFX pipeline can genuinely repair (it
        # flattens transparency and forces CMYK/PDF-X metadata in the same
        # pass). Font embedding and missing ICC profiles aren't safely
        # auto-fixable this way, so those are left as manual guidance.
        structure_findings = run_pdf_structure_audit(str(file_path), PLATFORMS.get(p["platform"], {}).get("name", "your distributor"), max_pages=BASIC_CHECK_MAX_PAGES)
        fixable_ids = {"pdfx1a_not_declared", "live_transparency_detected", "layers_detected", "pdfx1a_missing_output_intent"}
        needs_gs_fix = any(f["id"] in fixable_ids for f in structure_findings)

        if needs_gs_fix:
            if not find_ghostscript():
                ghostscript_result = {
                    "attempted": True,
                    "succeeded": False,
                    "reason": "Ghostscript isn't installed on this server, so live transparency/layers/PDF-X1a declaration couldn't be auto-repaired. These issues still need a manual fix (see fix_steps below) until Ghostscript is set up.",
                }
                metadata = analyze_file(str(file_path))
            else:
                fixed_name = f"{project_id}_{slot or 'cover'}_gsfixed_{uuid.uuid4().hex[:6]}.pdf"
                fixed_path = UPLOAD_DIR / fixed_name
                try:
                    convert_to_pdfx1a(str(file_path), str(fixed_path), title=p.get("name", "SparkPrep Export"))
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                    file_path = fixed_path
                    metadata = analyze_file(str(fixed_path))
                    metadata["original_filename"] = current_metadata.get("original_filename")
                    metadata["stored_filename"] = fixed_name
                    metadata["autofixed"] = True
                    # Re-run the structural audit against the fixed file so the
                    # response reflects what's actually true now, not a promise.
                    after_findings = run_pdf_structure_audit(str(fixed_path), PLATFORMS.get(p["platform"], {}).get("name", "your distributor"), max_pages=BASIC_CHECK_MAX_PAGES)
                    still_broken = [f["id"] for f in after_findings if f["id"] in fixable_ids]
                    ghostscript_result = {
                        "attempted": True,
                        "succeeded": not still_broken,
                        "fixed_issues": [f["id"] for f in structure_findings if f["id"] in fixable_ids],
                        "still_present": still_broken,
                    }
                except RuntimeError as e:
                    ghostscript_result = {"attempted": True, "succeeded": False, "reason": str(e)}
                    metadata = analyze_file(str(file_path))
                    await log_failure(db, "autofix_ghostscript", e, project_id=project_id, user_id=user["id"],
                                       context={"fixable_ids": [f["id"] for f in structure_findings if f["id"] in fixable_ids]})
        else:
            metadata = analyze_file(str(file_path))
    else:
        # Convert to CMYK TIFF
        fixed_name = f"{project_id}_{slot or 'cover'}_fixed_{uuid.uuid4().hex[:6]}.tif"
        fixed_path = UPLOAD_DIR / fixed_name
        try:
            convert_to_cmyk(str(file_path), str(fixed_path), 300)
        except Exception as e:
            await log_failure(db, "autofix_cmyk", e, project_id=project_id, user_id=user["id"],
                               context={"file_ext": Path(file_path).suffix.lower(), "slot": slot})
            raise HTTPException(500, f"CMYK conversion failed: {e}")
        try:
            os.remove(file_path)
        except OSError:
            pass
        metadata = analyze_file(str(fixed_path))
        metadata["original_filename"] = current_metadata.get("original_filename")
        metadata["stored_filename"] = fixed_name
        metadata["autofixed"] = True

    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    compliance = run_compliance_checks(metadata, trim["w"], trim["h"], plat["bleed"], p["platform"])
    # Branches above that didn't actually replace the file (no fix needed, or
    # Ghostscript unavailable) don't set stored_filename on the fresh
    # metadata -- fall back to the file we started with so it doesn't go
    # missing from the project record.
    metadata.setdefault("stored_filename", stored_filename)

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
    await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": update})
    return {"slot": slot, "file_metadata": metadata, "compliance": compliance, "ghostscript_fix": ghostscript_result, "check_type": "basic"}


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
            sections["cover"] = {"uploaded": True, "compliance": run_compliance_checks(cover_meta, trim["w"], trim["h"], plat["bleed"], p["platform"])}
    if needs_interior:
        interior_meta = (p.get("slots") or {}).get("interior")
        if not interior_meta:
            sections["interior"] = {"uploaded": False, "compliance": []}
        else:
            sections["interior"] = {"uploaded": True, "compliance": run_compliance_checks(interior_meta, trim["w"], trim["h"], plat["bleed"], p["platform"])}

    all_checks = [c for s in sections.values() for c in s["compliance"]]
    any_missing = any(not s["uploaded"] for s in sections.values())
    any_fail = any(c["status"] == "fail" for c in all_checks)
    any_warning = any(c["status"] == "warning" for c in all_checks)

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
async def _export_project_core(project_id: str, user: dict) -> dict:
    """Core export logic, shared by the single-project export endpoint and
    batch_export(). acting `user` owns the project; billing (tier, usage
    counters, white-label branding) resolves through get_billing_user() so
    team members correctly draw against their org owner's shared pool."""
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p or not p.get("uploaded_file"):
        raise HTTPException(404, "No uploaded file to export")

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

    # Check usage limits (bypassed entirely for active beta testers)
    tier = billing_user.get("tier", "free")
    tier_info = TIERS.get(tier, TIERS["free"])
    export_limit = tier_info["monthly_exports"]
    used = billing_user.get("exports_this_month", 0)
    beta_bypass = bool(user.get("beta_active") or billing_user.get("beta_active"))
    if not beta_bypass and used >= export_limit:
        raise HTTPException(402, f"Monthly export limit reached ({used}/{export_limit}). Upgrade to continue.")

    # Per-book export cap -- flat 5 exports per book, independent of the
    # account's monthly allowance above.
    book_exports_used = p.get("exports_used", 0)
    if not beta_bypass and book_exports_used >= EXPORTS_PER_BOOK:
        raise HTTPException(402, f"You have reached the {EXPORTS_PER_BOOK}-export limit for this book.")

    # Book counter — this project counts as a "book" the first time it's exported this period
    books_used = billing_user.get("books_this_month", 0)
    books_limit = tier_info["books_per_month"]
    is_new_book = not p.get("first_exported_at")
    if is_new_book and not beta_bypass:
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

    file_path = UPLOAD_DIR / p["uploaded_file"]
    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    paper = PAPER_TYPES.get(p["paper_type"], PAPER_TYPES["white_50lb"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    spine_w = calculate_spine_width(p.get("page_count", 0), paper["ppi"]) if p["project_type"] in ("cover", "combined") else 0

    export_name = f"{project_id}_export_{uuid.uuid4().hex[:6]}.pdf"
    export_path = EXPORT_DIR / export_name

    is_cover = p["project_type"] in ("cover", "combined")
    is_interior = p["project_type"] == "interior"
    file_ext = Path(file_path).suffix.lower()

    color_profile = (p.get("adjustments") or {}).get("color_profile") or DEFAULT_COLOR_PROFILE
    # White-label: only takes effect if the *billing* account's tier actually
    # includes it (Studio) -- a team member exporting under a Publisher
    # owner's plan still gets "SparkPrep", not a stale/downgraded brand name.
    producer_name = "SparkPrep"
    if TIERS.get(tier, {}).get("white_label") and billing_user.get("white_label_brand_name"):
        producer_name = billing_user["white_label_brand_name"]

    try:
        # Multi-page interior branch: source is a PDF → preserve vector text + fonts, tag PDF/X-1a
        if is_interior and file_ext == ".pdf":
            result = build_interior_pdf_x1a(
                str(file_path), str(export_path),
                trim_w=trim["w"], trim_h=trim["h"],
                bleed=plat["bleed"],
                title=p["name"],
                author=(user.get("name") or ""),
                color_profile=color_profile,
                producer_name=producer_name,
            )
        else:
            # Cover, combined, or image-source interior → rasterized single-page flow
            # If a valid ISBN is set on the project and this is a cover, generate barcode PNG
            barcode_png = None
            if is_cover and p.get("isbn"):
                try:
                    from barcode_engine import generate_barcode_png_bytes
                    barcode_png = generate_barcode_png_bytes(p["isbn"])
                except Exception:
                    barcode_png = None
            result = build_print_ready_pdf(
                str(file_path), str(export_path),
                trim_w=trim["w"], trim_h=trim["h"],
                bleed=plat["bleed"], spine_w=spine_w,
                is_cover=is_cover, title=p["name"],
                author=(user.get("name") or ""),
                barcode_png_bytes=barcode_png,
                color_profile=color_profile,
                producer_name=producer_name,
            )
    except Exception as e:
        await log_failure(db, "export_build_pdf", e, project_id=project_id, user_id=user["id"],
                           context={"project_type": p["project_type"], "file_ext": file_ext, "platform": p["platform"]})
        raise HTTPException(500, f"Export failed while building the print-ready PDF: {e}")

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
    return FileResponse(str(fp), media_type="application/pdf", filename=f"sparkprep_{export_name}")


# ---- Batch audit + batch export (Publisher/Studio -- see TIERS[*]["batch_enabled"]) ----
BATCH_MAX_PROJECTS = 25


class BatchIn(BaseModel):
    project_ids: List[str] = Field(min_length=1, max_length=BATCH_MAX_PROJECTS)


async def _require_batch_enabled(user: dict) -> dict:
    billing_user = await get_billing_user(user)
    tier = billing_user.get("tier", "free")
    if not TIERS.get(tier, TIERS["free"]).get("batch_enabled"):
        raise HTTPException(402, "Bulk audit + batch export is a Publisher/Studio plan feature.")
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
        if not p.get("uploaded_file"):
            results.append({"project_id": pid, "ok": False, "error": "No uploaded file"})
            continue
        file_path = UPLOAD_DIR / p["uploaded_file"]
        trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
        plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
        compliance = run_compliance_checks(p.get("file_metadata") or {}, trim["w"], trim["h"], plat["bleed"], p["platform"])
        structure = []
        if p.get("file_metadata", {}).get("is_pdf") and file_path.exists():
            structure = run_pdf_structure_audit(str(file_path), plat.get("name", "your distributor"), max_pages=BASIC_CHECK_MAX_PAGES)
        fails = sum(1 for c in compliance if c.get("status") == "fail") + sum(1 for f in structure if f.get("severity") == "fail")
        warnings = sum(1 for c in compliance if c.get("status") == "warning") + sum(1 for f in structure if f.get("severity") == "warning")
        results.append({
            "project_id": pid, "ok": True, "name": p.get("name"),
            "critical_failures": fails, "warnings": warnings,
            "compliance": compliance, "structure_findings": structure,
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


class InteriorCheckCheckoutIn(BaseModel):
    origin_url: str


@api_router.post("/projects/{project_id}/interior-check/checkout")
async def interior_check_checkout(project_id: str, payload: InteriorCheckCheckoutIn, user: dict = Depends(get_current_user)):
    """One-time purchase for a full structural interior check, up to
    ADVANCED_INTERIOR_MAX_PAGES pages. Priced by the buyer's current
    subscription tier. This is a one-time purchase, not a lifetime license --
    it unlocks full findings for this project's current interior file only,
    and is intentionally separate from the $0.99 anonymous audit flow.
    """
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    if p.get("project_type") != "interior":
        raise HTTPException(400, "Advanced Interior Check only applies to interior projects")
    if not p.get("uploaded_file"):
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
            payment_method_types=["card", "cashapp"],
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


@api_router.get("/projects/{project_id}/interior-check/status")
async def interior_check_status(project_id: str, user: dict = Depends(get_current_user)):
    """Lets the UI show the unlocked report again on a page reload/revisit,
    without needing the Stripe session_id to still be in the URL.
    """
    check = await db.interior_checks.find_one(
        {"project_id": project_id, "user_id": user["id"], "paid": True},
        sort=[("created_at", -1)],
    )
    if not check:
        return {"paid": False}

    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p or not p.get("uploaded_file"):
        raise HTTPException(404, "Interior file not found")
    file_path = UPLOAD_DIR / p["uploaded_file"]
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    findings = []
    if p.get("file_metadata", {}).get("is_pdf"):
        findings = run_pdf_structure_audit(str(file_path), plat.get("name", "your distributor"), max_pages=ADVANCED_INTERIOR_MAX_PAGES)
    return {"paid": True, "findings": findings, "check_type": "advanced"}


@api_router.get("/projects/{project_id}/interior-check/verify")
async def interior_check_verify(project_id: str, session_id: str, user: dict = Depends(get_current_user)):
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
    if not p or not p.get("uploaded_file"):
        raise HTTPException(404, "Interior file not found")
    file_path = UPLOAD_DIR / p["uploaded_file"]
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    findings = []
    if p.get("file_metadata", {}).get("is_pdf"):
        # The only call site allowed to scan beyond page 1 -- explicitly
        # capped at ADVANCED_INTERIOR_MAX_PAGES regardless of the file's
        # actual page count, enforcing the 300-page limit defense-in-depth
        # (on top of the page_count check already done at checkout time).
        findings = run_pdf_structure_audit(str(file_path), plat.get("name", "your distributor"), max_pages=ADVANCED_INTERIOR_MAX_PAGES)
    return {"paid": True, "findings": findings, "check_type": "advanced"}


# ---- Stripe Payments ----
@api_router.post("/payments/checkout")
async def create_checkout(payload: CheckoutIn, user: dict = Depends(get_current_user)):
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
            payment_method_types=["card", "cashapp"],
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
    return {"status": "ok"}


@api_router.get("/season")
async def get_season():
    return audit_season_status()


@api_router.get("/payments/status/{session_id}")
async def payment_status(session_id: str):
    record = await db.payment_transactions.find_one({"session_id": session_id})
    if not record:
        raise HTTPException(404, "Transaction not found")
    if record.get("payment_status") != "paid":
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
            if record.get("product") == "audit_099" and record.get("audit_id"):
                await db.audits.update_one(
                    {"audit_id": record["audit_id"]},
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
    elif t == "customer.subscription.deleted":
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
        raise HTTPException(402, "Interior composition isn't included in the Free plan. Upgrade to Author or higher.")
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
        raise HTTPException(402, "AI Blurb Writer requires the Author plan or higher.")
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


# ---- Slot Upload (front_cover / back_cover / spine / interior / full_wrap) ----
ALLOWED_SLOTS = {"front_cover", "back_cover", "spine", "interior", "full_wrap"}


@api_router.post("/projects/{project_id}/slot-upload/{slot}")
async def slot_upload(project_id: str, slot: str, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(400, f"Unknown slot: {slot}")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")

    billing_user = await get_billing_user(user)
    tier = billing_user.get("tier", "free")
    max_mb = TIERS.get(tier, TIERS["free"])["max_file_mb"]
    if user.get("beta_active") or billing_user.get("beta_active"):
        max_mb = 1024

    ext = Path(file.filename).suffix.lower()
    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type: {ext}")

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

    try:
        metadata = analyze_file(str(file_path))
        metadata["original_filename"] = file.filename
        metadata["stored_filename"] = file_id
        metadata["slot"] = slot

        trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
        plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
        compliance = run_compliance_checks(metadata, trim["w"], trim["h"], plat["bleed"], p["platform"])
    except Exception as e:
        await log_failure(db, "slot_upload_analyze", e, project_id=project_id, user_id=user["id"],
                           context={"filename": file.filename, "ext": ext, "slot": slot})
        raise HTTPException(500, f"Couldn't analyze this file: {e}. It may be corrupted or an unsupported variant of {ext}.")

    slots = p.get("slots") or {}
    # Clean up prior file for this slot
    prior = slots.get(slot)
    if prior and prior.get("stored_filename"):
        try: os.remove(UPLOAD_DIR / prior["stored_filename"])
        except OSError: pass
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

    await db.projects.update_one({"_id": ObjectId(project_id)}, {"$set": update})
    return {"slot": slot, "file_metadata": metadata, "compliance": compliance}


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

    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    compliance = run_compliance_checks(metadata, trim["w"], trim["h"], plat["bleed"], p["platform"])
    return metadata, compliance


async def _replace_slot(project_id: str, p: dict, slot: str, metadata: dict, compliance: list):
    slots = p.get("slots") or {}
    prior = slots.get(slot)
    if prior and prior.get("stored_filename"):
        try:
            os.remove(UPLOAD_DIR / prior["stored_filename"])
        except OSError:
            pass
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
    slot: str = "front_cover"  # front_cover or full_wrap


@api_router.post("/projects/{project_id}/ai-cover")
async def ai_generate_cover(project_id: str, payload: AICoverIn, user: dict = Depends(get_current_user)):
    if payload.slot not in ("front_cover", "full_wrap"):
        raise HTTPException(400, "AI cover generation supports the front_cover or full_wrap slot only")
    if not payload.prompt.strip():
        raise HTTPException(400, "Describe the cover art you want first.")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")

    billing_user = await get_billing_user(user)
    if billing_user.get("tier", "free") == "free":
        raise HTTPException(402, "AI Cover Generation requires the Author plan or higher.")
    if not OPENAI_API_KEY:
        raise HTTPException(503, "AI Cover Generation isn't configured yet — add OPENAI_API_KEY to backend/.env")

    prompt = build_cover_prompt(payload.prompt, p.get("name"), payload.genre, payload.slot == "full_wrap")
    try:
        image_bytes = await generate_cover_image(prompt, OPENAI_API_KEY)
    except AICoverError as e:
        raise HTTPException(e.status_code, str(e))

    file_id = f"{project_id}_{payload.slot}_ai_{uuid.uuid4().hex[:6]}.png"
    metadata, compliance = _save_generated_slot_file(
        p, project_id, payload.slot, file_id, image_bytes,
        original_filename="ai_generated_cover.png",
        extra_meta={"ai_generated": True, "ai_prompt": payload.prompt[:500]},
    )
    await _replace_slot(project_id, p, payload.slot, metadata, compliance)
    return {"slot": payload.slot, "file_metadata": metadata, "compliance": compliance}


def _target_pixels_for_slot(p: dict, slot: str) -> tuple[int, int]:
    """Target pixel dimensions for a slot at 300 DPI, trim + bleed included."""
    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    bleed = plat["bleed"]

    if slot == "full_wrap":
        paper = PAPER_TYPES.get(p["paper_type"], PAPER_TYPES["white_50lb"])
        spine_w = calculate_spine_width(p.get("page_count", 0), paper["ppi"])
        dims = calculate_full_cover_dimensions(trim["w"], trim["h"], spine_w, bleed, p.get("binding", "paperback"))
        return round(dims["total_width"] * 300), round(dims["total_height"] * 300)

    if slot == "spine":
        paper = PAPER_TYPES.get(p["paper_type"], PAPER_TYPES["white_50lb"])
        spine_w = calculate_spine_width(p.get("page_count", 0), paper["ppi"])
        return round(spine_w * 300), round((trim["h"] + bleed * 2) * 300)

    return round((trim["w"] + bleed * 2) * 300), round((trim["h"] + bleed * 2) * 300)


@api_router.post("/projects/{project_id}/ai-enhance/{slot}")
async def ai_enhance_image(project_id: str, slot: str, user: dict = Depends(get_current_user)):
    """'AI Upscale' fix option for a low-DPI compliance failure: runs the
    existing slot image through Real-ESRGAN (self-hosted, CPU, no external
    API) to add genuine pixel detail and resize it to the exact size needed
    to hit 300 DPI at this project's trim + bleed -- unlike the old
    OpenAI-based approach, which re-painted the image generatively and
    couldn't reliably reach the needed resolution. See the compliance
    re-check the frontend runs immediately after this to confirm it
    actually cleared the DPI failure."""
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(400, f"Unknown slot: {slot}")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    slot_data = (p.get("slots") or {}).get(slot)
    if not slot_data or not slot_data.get("stored_filename"):
        raise HTTPException(404, f"No uploaded file in slot '{slot}'")

    billing_user = await get_billing_user(user)
    if billing_user.get("tier", "free") == "free":
        raise HTTPException(402, "AI Upscale requires the Author plan or higher.")

    source_path = UPLOAD_DIR / slot_data["stored_filename"]
    if not source_path.exists():
        raise HTTPException(404, "Source file is missing on the server")
    ext = source_path.suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, f"AI Upscale supports PNG/JPEG/WEBP source images, not {ext} files.")

    target_w_px, target_h_px = _target_pixels_for_slot(p, slot)
    try:
        with open(source_path, "rb") as f:
            source_bytes = f.read()
        # CPU-bound (no GPU) -- runs in a thread so it doesn't block the
        # event loop for every other request while one image upscales.
        image_bytes = await asyncio.to_thread(upscale_to_size, source_bytes, target_w_px, target_h_px)
    except Exception as e:
        await log_failure(db, "ai_enhance", e, project_id=project_id, user_id=user["id"], context={"slot": slot})
        raise HTTPException(502, f"AI Upscale failed: {e}")

    file_id = f"{project_id}_{slot}_enhanced_{uuid.uuid4().hex[:6]}.png"
    metadata, compliance = _save_generated_slot_file(
        p, project_id, slot, file_id, image_bytes,
        original_filename=slot_data.get("original_filename") or "enhanced.png",
        extra_meta={"ai_enhanced": True},
    )
    await _replace_slot(project_id, p, slot, metadata, compliance)
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
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    spine_w = calculate_spine_width(p.get("page_count", 0), paper["ppi"])

    title = (p.get("name") or "Untitled Book").strip()
    file_id = f"{project_id}_full_wrap_tpl_{uuid.uuid4().hex[:6]}.pdf"
    file_path = UPLOAD_DIR / file_id
    render_cover_template(
        str(file_path), payload.template_key, title=title, author=(user.get("name") or ""),
        trim_w=trim["w"], trim_h=trim["h"], spine_w=spine_w, bleed=plat["bleed"], binding=p["binding"],
    )
    metadata, compliance = _save_generated_slot_file(
        p, project_id, "full_wrap", file_id, file_path.read_bytes(),
        original_filename=f"{payload.template_key}.pdf",
        extra_meta={"cover_template": payload.template_key},
    )
    await _replace_slot(project_id, p, "full_wrap", metadata, compliance)
    return {"slot": "full_wrap", "file_metadata": metadata, "compliance": compliance}


@api_router.delete("/projects/{project_id}/slot/{slot}")
async def slot_delete(project_id: str, slot: str, user: dict = Depends(get_current_user)):
    if slot not in ALLOWED_SLOTS:
        raise HTTPException(400, "Unknown slot")
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")
    slots = p.get("slots") or {}
    prior = slots.pop(slot, None)
    if prior and prior.get("stored_filename"):
        try: os.remove(UPLOAD_DIR / prior["stored_filename"])
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
    return FileResponse(str(fp))


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


# ---- $0.99 Print Failure Audit (anonymous, one-time payment) ----
AUDIT_PRICE_CENTS = 99


class AuditStart(BaseModel):
    platform: str = "kdp"
    trim_size: str = "6x9"


class AuditCheckoutIn(BaseModel):
    origin_url: str


@api_router.post("/audit/start")
async def audit_start(payload: AuditStart):
    if payload.platform not in PLATFORMS:
        raise HTTPException(400, "Invalid platform")
    if payload.trim_size not in TRIM_SIZES:
        raise HTTPException(400, "Invalid trim size")
    audit_id = uuid.uuid4().hex
    doc = {
        "audit_id": audit_id,
        "platform": payload.platform,
        "trim_size": payload.trim_size,
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

    metadata = analyze_file(str(file_path))
    metadata["original_filename"] = file.filename
    metadata["stored_filename"] = file_id

    trim = TRIM_SIZES[a["trim_size"]]
    plat = PLATFORMS[a["platform"]]
    findings = deep_audit(metadata, trim["w"], trim["h"], plat["bleed"], plat["name"])
    # Structural checks that require opening the actual PDF (not just its
    # source metadata): PDF/X-1a declaration, live transparency, layers,
    # embedded fonts, ICC output intent. Only applies to PDFs -- an image
    # upload has none of this structure yet.
    if metadata.get("is_pdf"):
        findings += run_pdf_structure_audit(str(file_path), plat["name"], max_pages=BASIC_CHECK_MAX_PAGES)
    summary = audit_summary(findings)

    preview = [
        {
            "id": f["id"],
            "severity": f["severity"],
            "title": f["title"],
            "why_it_fails": f["why_it_fails"][:120] + ("…" if len(f["why_it_fails"]) > 120 else ""),
            "one_click_fix": f.get("one_click_fix", False),
        }
        for f in findings
    ]

    await db.audits.update_one(
        {"audit_id": audit_id},
        {"$set": {
            "file_id": file_id,
            "file_metadata": metadata,
            "preview_findings": preview,
            "full_findings": findings,
            "summary": summary,
        }},
    )
    return {"audit_id": audit_id, "summary": summary, "preview": preview, "check_type": "basic"}


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
    if not a or not a.get("full_findings"):
        raise HTTPException(404, "Audit not found or not yet run")
    if not a.get("paid"):
        raise HTTPException(402, "Unlock the full report ($0.99) to download the PDF.")

    report_path = UPLOAD_DIR.parent / "exports" / f"{audit_id}_report.pdf"
    generate_audit_brief_pdf(
        findings=a["full_findings"],
        summary=a.get("summary") or {},
        project_meta={
            "title": a.get("file_metadata", {}).get("original_filename", "Untitled"),
            "platform": PLATFORMS.get(a["platform"], {}).get("name", a.get("platform", "")),
            "trim_size": a.get("trim_size", ""),
        },
        output_path=str(report_path),
    )
    return FileResponse(
        str(report_path),
        media_type="application/pdf",
        filename=f"sparkprep-audit-report-{audit_id}.pdf",
    )


@api_router.get("/audit/{audit_id}")
async def audit_get(audit_id: str):
    a = await db.audits.find_one({"audit_id": audit_id})
    if not a:
        raise HTTPException(404, "Audit not found")
    trim = TRIM_SIZES.get(a["trim_size"])
    plat = PLATFORMS.get(a["platform"])
    return {
        "audit_id": a["audit_id"],
        "platform": a["platform"],
        "platform_name": plat["name"] if plat else a["platform"],
        "trim_size": a["trim_size"],
        "trim_label": trim["label"] if trim else a["trim_size"],
        "file_metadata": a.get("file_metadata"),
        "summary": a.get("summary"),
        "preview": a.get("preview_findings"),
        "paid": a.get("paid", False),
        "full_report": a.get("full_findings") if a.get("paid") else None,
    }


@api_router.post("/audit/{audit_id}/checkout")
async def audit_checkout(audit_id: str, payload: AuditCheckoutIn):
    a = await db.audits.find_one({"audit_id": audit_id})
    if not a:
        raise HTTPException(404, "Audit not found")
    if a.get("paid"):
        return {"already_paid": True}
    if not stripe.api_key or stripe.api_key in ("sk_test_not_configured", ""):
        raise HTTPException(503, "Payments not configured — Stripe key missing")
    origin = payload.origin_url.rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card", "cashapp"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "SparkPrep Print Failure Audit",
                        "description": "Full detailed report with pinpointed issues, publisher rules and step-by-step fixes.",
                    },
                    "unit_amount": AUDIT_PRICE_CENTS,
                },
                "quantity": 1,
            }],
            success_url=f"{origin}/audit/{audit_id}/report?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/audit/{audit_id}/preview",
            metadata={"audit_id": audit_id, "purpose": "print_failure_audit"},
        )
    except stripe.error.StripeError as e:
        raise HTTPException(500, f"Stripe error: {e}")

    await db.audits.update_one({"audit_id": audit_id}, {"$set": {"session_id": session.id}})
    await db.payment_transactions.insert_one({
        "session_id": session.id,
        "audit_id": audit_id,
        "amount": AUDIT_PRICE_CENTS,
        "currency": "usd",
        "status": "initiated",
        "payment_status": "pending",
        "product": "audit_099",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"checkout_url": session.url, "session_id": session.id}


@api_router.get("/audit/{audit_id}/verify")
async def audit_verify(audit_id: str, session_id: str):
    a = await db.audits.find_one({"audit_id": audit_id})
    if not a:
        raise HTTPException(404, "Audit not found")
    if a.get("paid"):
        return {"paid": True}
    try:
        s = stripe.checkout.Session.retrieve(session_id)
        if s.payment_status == "paid" or s.status == "complete":
            await db.audits.update_one({"audit_id": audit_id}, {"$set": {"paid": True, "paid_at": datetime.now(timezone.utc).isoformat()}})
            await db.payment_transactions.update_one(
                {"session_id": session_id, "payment_status": {"$ne": "paid"}},
                {"$set": {"status": "completed", "payment_status": "paid"}},
            )
            return {"paid": True}
        return {"paid": False, "status": s.payment_status}
    except stripe.error.StripeError as e:
        raise HTTPException(500, f"Stripe verify failed: {e}")


# =====================================================================
# BETA PROGRAM
# =====================================================================
class BetaRedeemIn(BaseModel):
    code: str
    email: EmailStr
    password: str = Field(min_length=6)
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



app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[
        os.environ.get("FRONTEND_URL", "https://sparkprep.legenddary.com"),
        "https://sparkprepfinal.pages.dev",
        "http://localhost:3000",
    ],
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
