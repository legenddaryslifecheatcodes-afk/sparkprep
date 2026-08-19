from dotenv import load_dotenv
from pathlib import Path
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import io
import uuid
import logging
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import bcrypt
import jwt
import stripe
try:
    from emergentintegrations.llm.chat import LlmChat, UserMessage
except Exception:  # pragma: no cover - fallback for local/dev environments
    from types import SimpleNamespace

    class UserMessage:
        def __init__(self, text: str):
            self.text = text

    class LlmChat:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def with_model(self, *args, **kwargs):
            return self

        async def send_message(self, *_args, **_kwargs):
            raise RuntimeError("emergentintegrations is not installed in this local environment")

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
    TRIM_SIZES, PAPER_TYPES, BINDING_TYPES, PLATFORMS,
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
from barcode_engine import normalize_isbn, generate_barcode_png_bytes
from manuscript_composer import compose_manuscript_pdf, list_templates as list_manuscript_templates, TEMPLATES as MANUSCRIPT_TEMPLATES
from beta_engine import (
    generate_pass_code, new_pass_doc, new_feedback_doc, DEFAULT_CHECKLIST_FEATURES,
)

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
            return next(self._iterator)
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
                return doc
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
stripe.api_key = os.environ.get("STRIPE_API_KEY") or "sk_test_emergent"

app = FastAPI(title="SparkPrep API")
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("sparkprep")

# ---- Subscription tiers ----
TIERS = {
    "free": {
        "name": "Free",
        "books_per_month": 0,
        "monthly_exports": 0,
        "max_file_mb": 25,
        "price_cents": 0,
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
        "monthly_exports": 8,
        "max_file_mb": 100,
        "price_cents": 1999,
        "price_cents_annual": 19999,
        "features": [
            "1 full book / month (cover + spine + back + interior)",
            "8 print-ready exports / month",
            "Uploads up to 100 MB",
            "All distributor templates",
            "AI Blurb Writer",
        ],
    },
    "creator_pro": {
        "name": "Creator Pro",
        "books_per_month": 3,
        "monthly_exports": 24,
        "max_file_mb": 250,
        "price_cents": 3999,
        "price_cents_annual": 39999,
        "features": [
            "3 full books / month",
            "24 exports / month",
            "Uploads up to 250 MB",
            "Priority AI blurb + 3D mockup",
            "All distributor templates",
            "Email support",
        ],
    },
    "publisher": {
        "name": "Publisher",
        "books_per_month": 7,
        "monthly_exports": 56,
        "max_file_mb": 500,
        "price_cents": 6999,
        "price_cents_annual": 69999,
        "features": [
            "7 full books / month",
            "56 exports / month",
            "Team seats (up to 3)",
            "Uploads up to 500 MB",
            "Bulk audit + batch export",
            "Priority support",
        ],
    },
    "studio": {
        "name": "Studio",
        "books_per_month": 30,
        "monthly_exports": 240,
        "max_file_mb": 1024,
        "price_cents": 19999,
        "price_cents_annual": 199999,
        "features": [
            "30 full books / month",
            "240 exports / month",
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
    return user


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

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    platform: Optional[str] = None
    trim_size: Optional[str] = None
    paper_type: Optional[str] = None
    binding: Optional[str] = None
    page_count: Optional[int] = None
    project_type: Optional[str] = None

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
        "uploaded_file": None,
        "file_metadata": None,
        "compliance": [],
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


# ---- File Upload ----
@api_router.post("/projects/{project_id}/upload")
async def upload_file(project_id: str, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p:
        raise HTTPException(404, "Project not found")

    tier = user.get("tier", "free")
    max_mb = TIERS.get(tier, TIERS["free"])["max_file_mb"]
    if user.get("beta_active"):
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

    metadata = analyze_file(str(file_path))
    metadata["original_filename"] = file.filename
    metadata["stored_filename"] = file_id

    # Run compliance checks
    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    compliance = run_compliance_checks(metadata, trim["w"], trim["h"], plat["bleed"], p["platform"])

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
async def autofix(project_id: str, user: dict = Depends(get_current_user)):
    """Run all auto-fixes (convert to CMYK, upscale, flatten transparency,
    declare PDF/X-1a)."""
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p or not p.get("uploaded_file"):
        raise HTTPException(404, "No uploaded file")
    file_path = UPLOAD_DIR / p["uploaded_file"]
    ghostscript_result = None
    if p.get("file_metadata", {}).get("is_pdf"):
        # Check what actually needs fixing before touching the file --
        # only PDF/X-1a declaration, live transparency, and layers are
        # things Ghostscript's -dPDFX pipeline can genuinely repair (it
        # flattens transparency and forces CMYK/PDF-X metadata in the same
        # pass). Font embedding and missing ICC profiles aren't safely
        # auto-fixable this way, so those are left as manual guidance.
        structure_findings = run_pdf_structure_audit(str(file_path), PLATFORMS.get(p["platform"], {}).get("name", "your distributor"))
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
                fixed_name = f"{project_id}_gsfixed_{uuid.uuid4().hex[:6]}.pdf"
                fixed_path = UPLOAD_DIR / fixed_name
                try:
                    convert_to_pdfx1a(str(file_path), str(fixed_path), title=p.get("name", "SparkPrep Export"))
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                    await db.projects.update_one(
                        {"_id": ObjectId(project_id)},
                        {"$set": {"uploaded_file": fixed_name}},
                    )
                    file_path = fixed_path
                    metadata = analyze_file(str(fixed_path))
                    metadata["original_filename"] = p.get("file_metadata", {}).get("original_filename")
                    metadata["stored_filename"] = fixed_name
                    metadata["autofixed"] = True
                    # Re-run the structural audit against the fixed file so the
                    # response reflects what's actually true now, not a promise.
                    after_findings = run_pdf_structure_audit(str(fixed_path), PLATFORMS.get(p["platform"], {}).get("name", "your distributor"))
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
        else:
            metadata = analyze_file(str(file_path))
    else:
        # Convert to CMYK TIFF
        fixed_name = f"{project_id}_fixed_{uuid.uuid4().hex[:6]}.tif"
        fixed_path = UPLOAD_DIR / fixed_name
        convert_to_cmyk(str(file_path), str(fixed_path), 300)
        # Update project to use the fixed file
        try:
            os.remove(file_path)
        except OSError:
            pass
        await db.projects.update_one(
            {"_id": ObjectId(project_id)},
            {"$set": {"uploaded_file": fixed_name}},
        )
        metadata = analyze_file(str(fixed_path))
        metadata["original_filename"] = p.get("file_metadata", {}).get("original_filename")
        metadata["stored_filename"] = fixed_name
        metadata["autofixed"] = True

    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    compliance = run_compliance_checks(metadata, trim["w"], trim["h"], plat["bleed"], p["platform"])
    await db.projects.update_one(
        {"_id": ObjectId(project_id)},
        {"$set": {
            "file_metadata": metadata, "compliance": compliance,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"file_metadata": metadata, "compliance": compliance, "ghostscript_fix": ghostscript_result}


# ---- Export ----
@api_router.post("/projects/{project_id}/export")
async def export_project(project_id: str, user: dict = Depends(get_current_user)):
    p = await db.projects.find_one({"_id": ObjectId(project_id), "user_id": user["id"]})
    if not p or not p.get("uploaded_file"):
        raise HTTPException(404, "No uploaded file to export")

    # Check usage limits (bypassed entirely for active beta testers)
    tier = user.get("tier", "free")
    tier_info = TIERS.get(tier, TIERS["free"])
    export_limit = tier_info["monthly_exports"]
    used = user.get("exports_this_month", 0)
    beta_bypass = bool(user.get("beta_active"))
    if not beta_bypass and used >= export_limit:
        raise HTTPException(402, f"Monthly export limit reached ({used}/{export_limit}). Upgrade to continue.")

    # Book counter — this project counts as a "book" the first time it's exported this period
    books_used = user.get("books_this_month", 0)
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

    # Multi-page interior branch: source is a PDF → preserve vector text + fonts, tag PDF/X-1a
    if is_interior and file_ext == ".pdf":
        result = build_interior_pdf_x1a(
            str(file_path), str(export_path),
            trim_w=trim["w"], trim_h=trim["h"],
            bleed=plat["bleed"],
            title=p["name"],
            author=(user.get("name") or ""),
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
            barcode_png_bytes=barcode_png,
        )

    # Increment usage
    inc_fields = {"exports_this_month": 1}
    if is_new_book:
        inc_fields["books_this_month"] = 1
        await db.projects.update_one(
            {"_id": ObjectId(project_id)},
            {"$set": {"first_exported_at": datetime.now(timezone.utc).isoformat()}},
        )
    await db.users.update_one(
        {"_id": ObjectId(user["id"])},
        {"$inc": inc_fields},
    )
    new_used = used + 1
    new_books_used = books_used + (1 if is_new_book else 0)
    # Record export
    await db.exports.insert_one({
        "project_id": project_id,
        "user_id": user["id"],
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
        **result,
    }


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


# ---- Stripe Payments ----
@api_router.post("/payments/checkout")
async def create_checkout(payload: CheckoutIn, user: dict = Depends(get_current_user)):
    tier = payload.tier
    if tier not in ("author", "creator_pro", "publisher", "studio"):
        raise HTTPException(400, "Invalid tier")
    tier_info = TIERS[tier]
    origin = payload.origin_url.rstrip("/")
    if not stripe.api_key or stripe.api_key in ("sk_test_emergent", ""):
        raise HTTPException(
            status_code=503,
            detail="Payments not configured yet. Add a valid STRIPE_API_KEY to backend/.env to enable checkout.",
        )
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
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
@api_router.post("/ai/blurb")
async def generate_blurb(payload: BlurbIn, user: dict = Depends(get_current_user)):
    key = os.environ.get("EMERGENT_LLM_KEY")
    if not key:
        raise HTTPException(503, "AI not configured")
    system = (
        "You are a professional book-jacket copywriter for indie authors. "
        "Given a title and metadata, produce compelling back-cover copy suitable for print. "
        "Respond in strict JSON matching this schema exactly and nothing else:\n"
        '{"tagline": "string (max 12 words, punchy hook)", '
        '"variations": [ {"tone": "string", "copy": "string 100-160 words"} ]}'
        "\nProvide EXACTLY 3 variations with tones: 'Punchy', 'Literary', 'Commercial'."
    )
    prompt = (
        f"Title: {payload.title}\n"
        f"Genre: {payload.genre or 'general'}\n"
        f"Page count: {payload.page_count or 'unknown'}\n"
        f"Themes: {payload.themes or 'not specified'}\n"
        f"Target audience: {payload.audience or 'general adult'}\n"
        "Write back-cover copy variations now. Return only the JSON object."
    )
    try:
        chat = LlmChat(
            api_key=key,
            session_id=f"blurb-{user['id']}-{uuid.uuid4().hex[:6]}",
            system_message=system,
        ).with_model("anthropic", "claude-sonnet-4-6")
        response = await chat.send_message(UserMessage(text=prompt))
    except Exception as e:
        logger.exception("blurb generation failed")
        raise HTTPException(500, f"AI generation failed: {e}")

    # Best-effort parse of JSON from the response
    import json as _json
    text = str(response).strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    try:
        data = _json.loads(text)
    except Exception:
        # Fall back — try to slice the JSON object out
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = _json.loads(text[start:end+1])
            except Exception:
                data = {"tagline": "", "variations": [{"tone": "Raw", "copy": text}]}
        else:
            data = {"tagline": "", "variations": [{"tone": "Raw", "copy": text}]}
    return data


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

    metadata = analyze_file(str(file_path))
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

    tier = user.get("tier", "free")
    max_mb = TIERS.get(tier, TIERS["free"])["max_file_mb"]
    if user.get("beta_active"):
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

    metadata = analyze_file(str(file_path))
    metadata["original_filename"] = file.filename
    metadata["stored_filename"] = file_id
    metadata["slot"] = slot

    trim = TRIM_SIZES.get(p["trim_size"], TRIM_SIZES["6x9"])
    plat = PLATFORMS.get(p["platform"], PLATFORMS["kdp"])
    compliance = run_compliance_checks(metadata, trim["w"], trim["h"], plat["bleed"], p["platform"])

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
        findings += run_pdf_structure_audit(str(file_path), plat["name"])
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
    return {"audit_id": audit_id, "summary": summary, "preview": preview}


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
    if not stripe.api_key or stripe.api_key in ("sk_test_emergent", ""):
        raise HTTPException(503, "Payments not configured — Stripe key missing")
    origin = payload.origin_url.rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
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
        os.environ.get("FRONTEND_URL", "*"),
        "https://sparkprep-print.preview.emergentagent.com",
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
    elif stripe.api_key and (stripe.api_key.startswith("sk_test_") or stripe.api_key.startswith("rk_test_")) and stripe.api_key != "sk_test_emergent":
        logger.info("Stripe is in TEST mode.")
    logger.info("SparkPrep API ready")


@app.on_event("shutdown")
async def on_shutdown():
    if client is not None:
        client.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
