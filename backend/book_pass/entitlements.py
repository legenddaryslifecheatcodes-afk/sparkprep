"""Book credits and 7-day windows.

A *credit* is the right to start ONE book. It comes from a one-time book pass, or from a subscription (N per
billing period; unused ones expire at the period end). Starting a book on a project consumes a credit and opens a
window (PASS_WINDOW_DAYS) of unlimited exports for that project. Everything is stored in `book_credits`.

Only plain equality filters are used so this runs identically on MongoDB and on the in-memory dev database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import PASS_WINDOW_DAYS, PAID_EFFECTIVE_TIER, PLANS


class NoCredit(Exception):
    """The customer has no unused book to start."""


class BookRequired(Exception):
    """A project needs an active book window for what it is trying to do."""

    def __init__(self, has_credit: bool):
        self.has_credit = has_credit
        super().__init__("book_required")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def credit_state(c: dict, now: datetime) -> str:
    """available | active | finished | expired"""
    if c.get("project_id"):
        ends = parse(c.get("window_ends_at"))
        return "active" if ends and ends > now else "finished"
    exp = parse(c.get("expires_at"))
    return "expired" if exp and exp <= now else "available"


async def _credits(db, user_id: str) -> list[dict]:
    out = []
    async for c in db.book_credits.find({"user_id": user_id}):
        out.append(c)
    return out


async def grant_credit(db, user_id: str, source: str, *, dedupe_key: str, session_id: Optional[str] = None,
                       invoice_id: Optional[str] = None, subscription_id: Optional[str] = None,
                       plan: Optional[str] = None, expires_at: Optional[datetime] = None) -> tuple[dict, bool]:
    """Idempotent: the same Stripe session/invoice never grants twice (webhooks retry; the customer's browser
    may also trigger the same grant). Returns (credit, created)."""
    existing = await db.book_credits.find_one({"dedupe_key": dedupe_key})
    if existing:
        return existing, False
    doc = {
        "credit_id": uuid.uuid4().hex, "user_id": user_id, "source": source, "dedupe_key": dedupe_key,
        "session_id": session_id, "invoice_id": invoice_id, "subscription_id": subscription_id, "plan": plan,
        "created_at": iso(now_utc()), "expires_at": iso(expires_at) if expires_at else None,
        "project_id": None, "activated_at": None, "window_ends_at": None, "fingerprint": None,
    }
    await db.book_credits.insert_one(doc)
    return doc, True


async def has_paid_access(db, user_id: str, now: Optional[datetime] = None) -> bool:
    now = now or now_utc()
    return any(credit_state(c, now) in ("available", "active") for c in await _credits(db, user_id))


async def effective_tier(db, user: dict, *, exempt: bool) -> str:
    """The plan tier the existing plan-locked features should see under the book model. Admins/beta testers keep
    whatever they have; everyone else is 'free' until they hold a book, then unlocked."""
    if exempt:
        return user.get("tier", "free")
    return PAID_EFFECTIVE_TIER if await has_paid_access(db, user["id"]) else "free"


async def project_window(db, user_id: str, project_id: str, now: Optional[datetime] = None) -> Optional[dict]:
    now = now or now_utc()
    async for c in db.book_credits.find({"user_id": user_id, "project_id": project_id}):
        if credit_state(c, now) == "active":
            return c
    return None


async def activate_book(db, user_id: str, project_id: str, *, now: Optional[datetime] = None,
                        fingerprint: Optional[dict] = None) -> tuple[dict, bool]:
    """Start a book: consume one unused credit and open the window. Already-active projects are left alone."""
    now = now or now_utc()
    existing = await project_window(db, user_id, project_id, now)
    if existing:
        return existing, False
    usable = [c for c in await _credits(db, user_id) if credit_state(c, now) == "available"]
    # spend the credit that will vanish soonest first (subscription months), then never-expiring passes
    usable.sort(key=lambda c: (c.get("expires_at") is None, c.get("expires_at") or ""))
    for c in usable:
        ends = now + timedelta(days=PASS_WINDOW_DAYS)
        res = await db.book_credits.update_one(
            {"credit_id": c["credit_id"], "project_id": None},          # atomic claim: only if still unused
            {"$set": {"project_id": project_id, "activated_at": iso(now), "window_ends_at": iso(ends),
                      "fingerprint": fingerprint}},
        )
        if getattr(res, "matched_count", 1):
            c.update(project_id=project_id, activated_at=iso(now), window_ends_at=iso(ends), fingerprint=fingerprint)
            return c, True
    raise NoCredit()


async def require_active_book(db, user_id: str, project_id: str, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    w = await project_window(db, user_id, project_id, now)
    if not w:
        raise BookRequired(has_credit=any(credit_state(c, now) == "available" for c in await _credits(db, user_id)))
    return w


async def summary(db, user_id: str, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    credits = await _credits(db, user_id)
    windows, available = [], 0
    for c in credits:
        st = credit_state(c, now)
        if st == "available":
            available += 1
        elif st == "active":
            ends = parse(c["window_ends_at"])
            windows.append({"project_id": c["project_id"], "ends_at": c["window_ends_at"],
                            "seconds_left": int((ends - now).total_seconds())})
    sub = None
    async for s in db.book_subscriptions.find({"user_id": user_id}):
        sub = {"plan": s.get("plan"), "status": s.get("status"), "plan_name": PLANS.get(s.get("plan"), {}).get("name")}
    return {"available_books": available, "active_windows": windows, "subscription": sub}


# ------------------------------------------------------------------ Stripe -> credits
def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def invoice_period_end(invoice) -> Optional[datetime]:
    """End of the billing period an invoice pays for (when that month's unused books expire)."""
    lines = _get(_get(invoice, "lines"), "data") or []
    ts = _get(_get(lines[0], "period"), "end") if lines else None
    ts = ts or _get(invoice, "period_end")
    return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None


async def register_subscription(db, *, user_id: str, subscription_id: str, plan: str, status: str = "active"):
    existing = await db.book_subscriptions.find_one({"subscription_id": subscription_id})
    if existing:
        await db.book_subscriptions.update_one({"subscription_id": subscription_id}, {"$set": {"status": status}})
        return
    await db.book_subscriptions.insert_one({"subscription_id": subscription_id, "user_id": user_id, "plan": plan,
                                            "status": status, "created_at": iso(now_utc())})


async def grant_for_invoice(db, invoice, *, user_id: str, subscription_id: str, plan: str) -> int:
    """One paid subscription invoice = books_per_period credits, expiring when that period ends."""
    inv_id = _get(invoice, "id")
    n = PLANS.get(plan, {}).get("books_per_period", 1)
    expires = invoice_period_end(invoice)
    made = 0
    for i in range(n):
        _, created = await grant_credit(db, user_id, "subscription", dedupe_key=f"invoice:{inv_id}:{i}", invoice_id=inv_id,
                                        subscription_id=subscription_id, plan=plan, expires_at=expires)
        made += int(created)
    return made
