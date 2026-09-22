"""HTTP endpoints for the book-pass model. Built with injected dependencies (no import of server.py)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import entitlements as ent
from .config import (PASS_WINDOW_DAYS, PLANS, BOOK_PASS_PRICE_CENTS, book_pass_on, public_pricing)
from .purchases import start_checkout


class PassIn(BaseModel):
    origin_url: str
    audit_id: Optional[str] = None


class SubscribeIn(BaseModel):
    plan: str
    origin_url: str
    audit_id: Optional[str] = None


def build_router(*, db, get_current_user, stripe, find_project) -> APIRouter:
    """find_project(project_id, user) -> project dict or None (ownership check lives in server.py)."""
    router = APIRouter()

    def _require_model():
        if not book_pass_on():
            raise HTTPException(404, "Not available")

    @router.get("/pricing")
    async def pricing():
        return public_pricing()

    @router.post("/payments/book-pass")
    async def buy_book(payload: PassIn, user: dict = Depends(get_current_user)):
        _require_model()
        return await start_checkout(db=db, stripe=stripe, user=user, product="book_pass",
                                    origin_url=payload.origin_url, audit_id=payload.audit_id)

    @router.post("/payments/subscribe")
    async def subscribe(payload: SubscribeIn, user: dict = Depends(get_current_user)):
        _require_model()
        plan = PLANS.get(payload.plan)
        if not plan:
            raise HTTPException(400, "Unknown plan")
        if not plan["available"]:
            raise HTTPException(400, f"The {plan['name']} plan is coming soon.")
        return await start_checkout(db=db, stripe=stripe, user=user, product="subscription", plan=payload.plan,
                                    origin_url=payload.origin_url, audit_id=payload.audit_id)

    @router.get("/me/books")
    async def my_books(user: dict = Depends(get_current_user)):
        _require_model()
        return await ent.summary(db, user["id"])

    @router.get("/projects/{project_id}/book")
    async def book_status(project_id: str, user: dict = Depends(get_current_user)):
        _require_model()
        if not await find_project(project_id, user):
            raise HTTPException(404, "Project not found")
        w = await ent.project_window(db, user["id"], project_id)
        s = await ent.summary(db, user["id"])
        out = {"active": bool(w), "window_days": PASS_WINDOW_DAYS, "available_books": s["available_books"],
               "book_price_cents": BOOK_PASS_PRICE_CENTS}
        if w:
            ends = ent.parse(w["window_ends_at"])
            out.update(ends_at=w["window_ends_at"], seconds_left=int((ends - ent.now_utc()).total_seconds()))
        return out

    @router.post("/projects/{project_id}/activate-book")
    async def activate(project_id: str, user: dict = Depends(get_current_user)):
        _require_model()
        p = await find_project(project_id, user)
        if not p:
            raise HTTPException(404, "Project not found")
        try:
            credit, started = await ent.activate_book(db, user["id"], project_id)
        except ent.NoCredit:
            raise HTTPException(402, {"code": "no_book_credit", "msg": "You don't have a book to start yet. Buy one, or subscribe."})
        ends = ent.parse(credit["window_ends_at"])
        return {"started": started, "ends_at": credit["window_ends_at"],
                "seconds_left": int((ends - ent.now_utc()).total_seconds()), "window_days": PASS_WINDOW_DAYS}

    return router
