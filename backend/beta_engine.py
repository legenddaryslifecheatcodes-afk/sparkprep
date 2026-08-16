"""Beta program helpers — pass code generation, redemption logic, feedback storage.

Beta pass semantics:
- 1 pass = 1 full-book unlock for exactly one account (single-use)
- While `beta_active == True` on the user, tier limits (books, exports, file size) are bypassed
- Once the user submits sign-off feedback, `beta_active` flips to False and their account reverts to Free tier
- Passes are cryptographically random codes prefixed "LGND-" for brand recognition
"""
import secrets
import string
from datetime import datetime, timezone
from typing import Optional


CODE_PREFIX = "LGND"
CODE_LEN = 8  # after the prefix, e.g. LGND-A7X9K2QP


def generate_pass_code() -> str:
    """Generate a random beta pass code like `LGND-A7X9K2QP`."""
    alphabet = string.ascii_uppercase + string.digits
    # Skip visually confusing chars
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    body = "".join(secrets.choice(alphabet) for _ in range(CODE_LEN))
    return f"{CODE_PREFIX}-{body}"


def new_pass_doc(created_by_email: str, note: str = "") -> dict:
    return {
        "code": generate_pass_code(),
        "status": "unredeemed",  # unredeemed | active | consumed | revoked
        "redeemed_by_user_id": None,
        "redeemed_by_email": None,
        "redeemed_at": None,
        "feedback_submitted_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by_email,
        "note": note,
    }


def new_feedback_doc(user_id: str, user_email: str, pass_code: Optional[str], payload: dict) -> dict:
    """Build a feedback document for insertion.
    payload contains: checklist (list of {feature, status, notes}), critical_review, public_review, would_recommend."""
    return {
        "user_id": user_id,
        "user_email": user_email,
        "pass_code": pass_code,
        "checklist": payload.get("checklist", []),
        "critical_review": (payload.get("critical_review") or "").strip(),
        "public_review": (payload.get("public_review") or "").strip(),
        "would_recommend": bool(payload.get("would_recommend", False)),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


# The default sign-off checklist testers see. Each item = a core user-facing capability.
DEFAULT_CHECKLIST_FEATURES = [
    {"key": "signup_login", "label": "Sign up + Log in flow"},
    {"key": "project_create", "label": "Create a new book project (trim, paper, binding, page count)"},
    {"key": "distributor_specs", "label": "Distributor spec library (KDP, IngramSpark, B&N, Lulu)"},
    {"key": "file_upload", "label": "Upload a cover or interior file (PDF/JPG/PNG/TIFF)"},
    {"key": "compliance_report", "label": "Compliance report (DPI, CMYK, bleed, spine)"},
    {"key": "auto_fix", "label": "One-click Auto-Fix (RGB → CMYK, 300 DPI, flatten)"},
    {"key": "spine_calculator", "label": "Spine width calculator + full cover geometry"},
    {"key": "3d_mockup", "label": "3D photorealistic book mockup"},
    {"key": "ai_blurb", "label": "AI Blurb Writer (Claude)"},
    {"key": "isbn_barcode", "label": "ISBN barcode auto-generation"},
    {"key": "manuscript_composer", "label": "Interior manuscript composer (templates → PDF)"},
    {"key": "audit_99c", "label": "99¢ Print Failure Audit (standalone flow)"},
    {"key": "export_pdfx1a", "label": "Export print-ready PDF/X-1a"},
    {"key": "billing_upgrade", "label": "Pricing page + subscription upgrade (do NOT actually pay)"},
    {"key": "brand_polish", "label": "Overall brand & visual polish"},
]
