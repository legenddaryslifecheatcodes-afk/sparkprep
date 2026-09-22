"""Buying a book (one-time pass or subscription), the audit credit, and Stripe's confirmations.

Audit credit rules (owner's): the audit fee is credited toward the first purchase, for subscribers and
non-subscribers alike, and ONLY ONE audit credit works per purchase/book. A credit is reserved when checkout is
created, consumed when Stripe confirms payment, and released if that checkout expires unpaid.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from .config import (AUDIT_CREDIT_MAX_CENTS, BOOK_PASS_PRICE_CENTS, PASS_WINDOW_DAYS, PLANS)
from .entitlements import (_get, grant_credit, grant_for_invoice, register_subscription, iso, now_utc)

DEFAULT_ORIGIN = "https://sparkprep.legenddary.com"
_ALLOWED_ORIGIN_RE = re.compile(
    r"^(https://sparkprep\.legenddary\.com|https://([a-z0-9]+\.)?sparkprep-live\.pages\.dev|http://localhost:3000)$")


def safe_origin(origin: str) -> str:
    """Where Stripe sends the customer back to. Only our own sites; anything else falls back to the real domain."""
    o = (origin or "").rstrip("/")
    return o if _ALLOWED_ORIGIN_RE.match(o) else DEFAULT_ORIGIN


def _stripe_ready(stripe):
    if not stripe.api_key or stripe.api_key in ("sk_test_not_configured", ""):
        raise HTTPException(503, "Payments not configured yet.")


# ------------------------------------------------------------------ audit credit
async def audit_credit_cents(db, stripe, audit_id: str) -> int:
    """How many cents of credit this audit is worth right now, or raise. Also frees a stale reservation."""
    a = await db.audits.find_one({"audit_id": audit_id})
    if not a:
        raise HTTPException(404, "Audit not found")
    if not a.get("paid"):
        raise HTTPException(400, "That audit hasn't been paid for, so there is no credit to apply.")
    if a.get("credit_consumed"):
        raise HTTPException(409, "This audit's credit has already been used on another book.")
    reserved = a.get("credit_reserved_session")
    if reserved:
        try:
            old = await asyncio.to_thread(stripe.checkout.Session.retrieve, reserved)
            if old.payment_status == "paid":
                raise HTTPException(409, "This audit's credit is already being used by a purchase in progress.")
            if old.status == "open":
                await asyncio.to_thread(stripe.checkout.Session.expire, reserved)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 - a stale reservation we can't inspect must not block the customer
            pass
    paid = None
    async for t in db.payment_transactions.find({"audit_id": audit_id}):
        if t.get("product") == "audit_099":
            paid = t.get("amount")
    return min(paid or AUDIT_CREDIT_MAX_CENTS, AUDIT_CREDIT_MAX_CENTS)


async def consume_audit_credit(db, record: dict):
    aid = record.get("audit_id")
    if not aid:
        return
    a = await db.audits.find_one({"audit_id": aid})
    if a and not a.get("credit_consumed"):
        await db.audits.update_one({"audit_id": aid}, {"$set": {
            "credit_consumed": True, "credit_consumed_by": record.get("user_id"),
            "credit_consumed_session": record.get("session_id"), "credit_consumed_at": iso(now_utc())}})


# ------------------------------------------------------------------ creating a checkout
async def start_checkout(*, db, stripe, user: dict, product: str, origin_url: str, plan: Optional[str] = None,
                         audit_id: Optional[str] = None) -> dict:
    _stripe_ready(stripe)
    origin = safe_origin(origin_url)
    credit = await audit_credit_cents(db, stripe, audit_id) if audit_id else 0

    if product == "book_pass":
        list_price, name = BOOK_PASS_PRICE_CENTS, "SparkPrep Book Pass"
        desc = f"One book — cover, interior, or both — with {PASS_WINDOW_DAYS} days of unlimited exports."
        price_data = {"currency": "usd", "unit_amount": list_price, "product_data": {"name": name, "description": desc}}
        mode, sub_kwargs = "payment", {}
    else:
        p = PLANS[plan]
        list_price = p["price_cents"]
        price_data = {"currency": "usd", "unit_amount": list_price, "recurring": {"interval": "month"},
                      "product_data": {"name": f"SparkPrep {p['name']}",
                                       "description": f"{p['books_per_period']} book(s) each month, {PASS_WINDOW_DAYS} days of unlimited exports per book."}}
        mode = "subscription"
        sub_kwargs = {"subscription_data": {"metadata": {"user_id": user["id"], "product": "subscription", "plan": plan}}}

    metadata = {"user_id": user["id"], "product": product}
    if plan:
        metadata["plan"] = plan
    kwargs = dict(mode=mode, line_items=[{"price_data": price_data, "quantity": 1}],
                  success_url=f"{origin}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
                  cancel_url=f"{origin}/pricing", customer_email=user["email"], metadata=metadata, **sub_kwargs)
    if credit:
        metadata["audit_id"] = audit_id
        coupon = await asyncio.to_thread(stripe.Coupon.create, amount_off=credit, currency="usd", duration="once",
                                         max_redemptions=1, name="SparkPrep audit credit")
        kwargs["discounts"] = [{"coupon": coupon.id}]
    try:
        session = await asyncio.to_thread(stripe.checkout.Session.create, **kwargs)
    except stripe.error.StripeError as e:
        raise HTTPException(500, f"Stripe error: {e}")

    if audit_id:
        await db.audits.update_one({"audit_id": audit_id}, {"$set": {"credit_reserved_session": session.id}})
    await db.payment_transactions.insert_one({
        "session_id": session.id, "user_id": user["id"], "product": product, "plan": plan, "audit_id": audit_id,
        "amount": list_price - credit, "list_price": list_price, "audit_credit": credit, "currency": "usd",
        "status": "initiated", "payment_status": "pending", "created_at": iso(now_utc()),
    })
    return {"checkout_url": session.url, "session_id": session.id, "you_pay_cents": list_price - credit,
            "audit_credit_cents": credit}


# ------------------------------------------------------------------ Stripe's confirmations
def invoice_subscription_id(invoice) -> Optional[str]:
    sid = _get(invoice, "subscription")
    if sid:
        return sid if isinstance(sid, str) else _get(sid, "id")
    parent = _get(_get(invoice, "parent"), "subscription_details")      # newer Stripe API versions
    sid = _get(parent, "subscription")
    return sid if isinstance(sid, str) or sid is None else _get(sid, "id")


async def on_session_paid(db, stripe, record: dict, session) -> None:
    """A checkout finished and Stripe says it is paid (webhook OR the customer's return page; idempotent)."""
    product, user_id, sid = record.get("product"), record.get("user_id"), record.get("session_id")
    if product == "book_pass":
        await grant_credit(db, user_id, "pass", dedupe_key=f"session:{sid}", session_id=sid)
    elif product == "subscription":
        sub_id = _get(session, "subscription")
        sub_id = sub_id if isinstance(sub_id, str) or sub_id is None else _get(sub_id, "id")
        if sub_id:
            await register_subscription(db, user_id=user_id, subscription_id=sub_id, plan=record.get("plan"))
            inv_id = _get(session, "invoice")
            inv_id = inv_id if isinstance(inv_id, str) or inv_id is None else _get(inv_id, "id")
            if inv_id:
                inv = await asyncio.to_thread(stripe.Invoice.retrieve, inv_id)
                if _get(inv, "status") == "paid" or _get(inv, "paid"):
                    await grant_for_invoice(db, inv, user_id=user_id, subscription_id=sub_id, plan=record.get("plan"))
    await consume_audit_credit(db, record)


async def on_invoice_paid(db, stripe, invoice) -> int:
    """invoice.paid: the first month's invoice, and every renewal. Grants that month's books (once per invoice)."""
    sub_id = invoice_subscription_id(invoice)
    if not sub_id:
        return 0
    sub = await db.book_subscriptions.find_one({"subscription_id": sub_id})
    if not sub:                                     # invoice.paid can beat checkout.session.completed
        try:
            s = await asyncio.to_thread(stripe.Subscription.retrieve, sub_id)
            md = _get(s, "metadata") or {}
            user_id, plan = _get(md, "user_id"), _get(md, "plan")
        except Exception:  # noqa: BLE001
            return 0
        if not user_id or plan not in PLANS:
            return 0
        await register_subscription(db, user_id=user_id, subscription_id=sub_id, plan=plan)
        sub = {"user_id": user_id, "plan": plan}
    return await grant_for_invoice(db, invoice, user_id=sub["user_id"], subscription_id=sub_id, plan=sub["plan"])


async def on_session_expired(db, session) -> None:
    """An unpaid checkout timed out: give its audit credit back so it can be used on the next attempt."""
    md = _get(session, "metadata") or {}
    aid, sid = _get(md, "audit_id"), _get(session, "id")
    if aid:
        a = await db.audits.find_one({"audit_id": aid})
        if a and a.get("credit_reserved_session") == sid and not a.get("credit_consumed"):
            await db.audits.update_one({"audit_id": aid}, {"$set": {"credit_reserved_session": None}})


async def on_subscription_deleted(db, sub_id: str) -> None:
    """Cancelled: no new books. Books already granted stay usable until their period ends."""
    await db.book_subscriptions.update_one({"subscription_id": sub_id}, {"$set": {"status": "canceled"}})
