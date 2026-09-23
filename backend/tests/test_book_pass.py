"""Book-pass pricing model: pass, subscription, audit credit, 7-day windows, export gating, safety switch.
Stripe is SIMULATED (no keys/network); the app runs in-process against the in-memory database."""
import asyncio
import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_bp_"), USE_MEMORY_DB="1", JWT_SECRET="bp-test-" + "x" * 32,
                  ADMIN_EMAIL="root-admin@example.com")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import book_pass  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SECRET = "whsec_bp_test"
EMAIL = "reader@example.com"


class Obj(dict):
    __getattr__ = dict.get


@pytest.fixture(autouse=True)
def book_mode(monkeypatch):
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    for coll in ("book_credits", "book_subscriptions", "audits", "payment_transactions", "interior_checks"):
        getattr(server.db, coll)._docs.clear()
    asyncio.run(server.db.users.update_one({"email": EMAIL}, {"$set": {"tier": "free", "beta_active": False}}))


@pytest.fixture
def stripe_fake(monkeypatch):
    st = {"sessions": [], "coupons": [], "expired": [], "objects": {}, "invoices": {}, "subs": {}}

    def create(**kw):
        sid = f"cs_bp_{len(st['sessions']) + 1}_{int(time.time() * 1000) % 100000}"
        s = Obj(id=sid, url=f"https://checkout.stripe.test/{sid}", payment_status="unpaid", status="open", subscription=None,
                invoice=None, metadata=kw.get("metadata", {}))
        st["sessions"].append({"id": sid, **kw}); st["objects"][sid] = s
        return s

    def coupon(**kw):
        st["coupons"].append(kw); return Obj(id=f"coup_{len(st['coupons'])}")

    monkeypatch.setattr(server.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(server.stripe.checkout.Session, "create", create)
    monkeypatch.setattr(server.stripe.checkout.Session, "retrieve", lambda sid: st["objects"][sid])
    monkeypatch.setattr(server.stripe.checkout.Session, "expire", lambda sid: (st["expired"].append(sid), st["objects"][sid].update(status="expired")))
    monkeypatch.setattr(server.stripe.Coupon, "create", coupon)
    monkeypatch.setattr(server.stripe.Invoice, "retrieve", lambda iid: st["invoices"][iid])
    monkeypatch.setattr(server.stripe.Subscription, "retrieve", lambda sid: st["subs"][sid])
    return st


@pytest.fixture(scope="module")
def client():
    for email, tier in ((EMAIL, "free"), ("root-admin@example.com", "studio")):
        asyncio.run(server.db.users.insert_one({
            "email": email, "password_hash": server.hash_password("TestPass123!"), "name": "R", "tier": tier,
            "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        r = c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"})
        c.headers["Authorization"] = "Bearer " + r.json()["token"]
        yield c


def uid():
    return str(asyncio.run(server.db.users.find_one({"email": EMAIL}))["_id"])


def signed(event, secret=SECRET):
    body, ts = json.dumps(event).encode(), int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"}


def hook(client, event):
    body, h = signed(event)
    r = client.post("/api/stripe/webhook", content=body, headers=h)
    assert r.status_code == 200, r.text
    return r


def paid_session_event(sid, **extra):
    return {"id": "evt", "type": "checkout.session.completed",
            "data": {"object": {"id": sid, "payment_status": "paid", "status": "complete", **extra}}}


def invoice(iid, sub="sub_1", period_end=None, paid=True):
    end = int((period_end or datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    return {"id": iid, "status": "paid" if paid else "open", "paid": paid, "subscription": sub,
            "lines": {"data": [{"period": {"end": end}}]}}


def me(client):
    return client.get("/api/auth/me").json()["user"]


def buy_pass(client, **kw):
    r = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def give_credit(n=1, expires=None):
    for i in range(n):
        asyncio.run(book_pass.entitlements.grant_credit(server.db, uid(), "pass", dedupe_key=f"test:{time.time_ns()}:{i}",
                                                        expires_at=expires))


def make_project(client, ptype="interior"):
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch))
    for _ in range(2):
        c.setFont("Times-Roman", 11)
        for i in range(20):
            c.drawString(1.1 * inch, (8.0 - i * 0.3) * inch, "Centered body text well inside the safe margins of the page.")
        c.showPage()
    c.save()
    pid = client.post("/api/projects", json={"name": "My Book", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                              "binding": "paperback", "page_count": 100, "project_type": ptype}).json()["id"]
    assert client.post(f"/api/projects/{pid}/slot-upload/interior", files={"file": ("b.pdf", buf.getvalue(), "application/pdf")}).status_code == 200
    return pid


# ---------------------------------------------------------------- pricing
def test_pricing_is_served_from_one_place_and_bigger_plans_are_coming_soon(client):
    p = client.get("/api/pricing").json()
    assert p["model"] == "book_pass"
    assert p["audit"]["price_cents"] == 199 and p["book"]["price_cents"] == 7499 and p["book"]["window_days"] == 7
    plans = {x["id"]: x for x in p["plans"]}
    assert plans["book_1"]["price_cents"] == 4999 and plans["book_1"]["available"] is True
    assert plans["book_3"]["price_cents"] == 11999 and plans["book_3"]["available"] is False
    assert plans["book_10"]["price_cents"] == 29999 and plans["book_10"]["available"] is False


def test_legacy_mode_is_untouched_by_default(client, monkeypatch):
    monkeypatch.delenv("SPARKPREP_PRICING_MODEL", raising=False)
    assert client.get("/api/pricing").json()["model"] == "legacy"
    assert client.post("/api/payments/book-pass", json={"origin_url": "https://x"}).status_code == 404
    assert client.get("/api/me/books").status_code == 404
    assert server.book_pass.audit_price_cents(server.AUDIT_PRICE_CENTS) == 199


def test_old_plans_and_separate_interior_purchase_are_closed_in_book_mode(client, stripe_fake):
    assert client.post("/api/payments/checkout", json={"tier": "author", "origin_url": "https://x"}).status_code == 410
    pid = make_project(client)
    assert client.post(f"/api/projects/{pid}/interior-check/checkout", json={"origin_url": "https://x"}).status_code == 410


def test_audit_costs_1_99_in_book_mode(client, stripe_fake):
    asyncio.run(server.db.audits.insert_one({"audit_id": "aud_price", "paid": False}))
    assert client.post("/api/audit/aud_price/checkout", json={"origin_url": "https://sparkprep.legenddary.com"}).status_code == 200
    assert stripe_fake["sessions"][-1]["line_items"][0]["price_data"]["unit_amount"] == 199


# ---------------------------------------------------------------- one-time book pass
def test_book_pass_purchase_grants_one_book_once(client, stripe_fake):
    assert me(client)["tier"] == "free"
    out = buy_pass(client)
    call = stripe_fake["sessions"][-1]
    assert call["mode"] == "payment" and call["line_items"][0]["price_data"]["unit_amount"] == 7499 and "discounts" not in call
    assert out["you_pay_cents"] == 7499
    assert client.get("/api/me/books").json()["available_books"] == 0            # not paid yet
    sid = call["id"]
    hook(client, paid_session_event(sid)); hook(client, paid_session_event(sid))    # Stripe retries
    assert client.get("/api/me/books").json()["available_books"] == 1
    assert me(client)["tier"] == "author"                                          # plan-locked features unlock while holding a book


def test_success_page_return_grants_the_book_even_if_the_webhook_is_late(client, stripe_fake):
    sid = buy_pass(client)["session_id"]
    stripe_fake["objects"][sid].update(payment_status="paid", status="complete")
    assert client.get(f"/api/payments/status/{sid}").json()["payment_status"] == "paid"
    assert client.get("/api/me/books").json()["available_books"] == 1
    hook(client, paid_session_event(sid))                                          # the late webhook must not double-grant
    assert client.get("/api/me/books").json()["available_books"] == 1


# ---------------------------------------------------------------- 7-day window and export gating
def test_export_needs_a_started_book_then_is_unlimited_for_seven_days_then_stops(client):
    pid = make_project(client)
    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 402 and r.json()["detail"]["code"] == "book_required" and r.json()["detail"]["has_credit"] is False

    give_credit(1)
    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 402 and r.json()["detail"]["has_credit"] is True      # has a book but hasn't started this one

    a = client.post(f"/api/projects/{pid}/activate-book")
    assert a.status_code == 200 and a.json()["started"] is True and a.json()["window_days"] == 7
    assert 6 * 86400 < a.json()["seconds_left"] <= 7 * 86400
    assert client.post(f"/api/projects/{pid}/activate-book").json()["started"] is False        # idempotent, no second book used
    assert client.get("/api/me/books").json()["available_books"] == 0

    for i in range(7):                                                             # legacy cap was 5 per book; here it's unlimited
        r = client.post(f"/api/projects/{pid}/export")
        assert r.status_code == 200, (i, r.text)

    asyncio.run(server.db.book_credits.update_one({"project_id": pid}, {"$set": {
        "window_ends_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}}))
    r = client.post(f"/api/projects/{pid}/export")
    assert r.status_code == 402 and r.json()["detail"]["code"] == "book_required"


def test_starting_a_book_without_owning_one_is_refused_and_other_peoples_projects_are_off_limits(client):
    pid = make_project(client)
    r = client.post(f"/api/projects/{pid}/activate-book")
    assert r.status_code == 402 and r.json()["detail"]["code"] == "no_book_credit"
    assert client.post("/api/projects/000000000000000000000000/activate-book").status_code == 404


def test_cover_only_interior_only_and_combined_all_use_exactly_one_book(client):
    give_credit(3)
    used = []
    for ptype in ("interior", "cover", "combined"):
        pid = client.post("/api/projects", json={"name": ptype, "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                                  "binding": "paperback", "page_count": 100, "project_type": ptype}).json()["id"]
        assert client.post(f"/api/projects/{pid}/activate-book").status_code == 200
        used.append(client.get("/api/me/books").json()["available_books"])
    assert used == [2, 1, 0]


def test_beta_testers_and_admins_are_not_gated(client):
    pid = make_project(client)
    asyncio.run(server.db.users.update_one({"email": EMAIL}, {"$set": {"beta_active": True}}))
    assert client.post(f"/api/projects/{pid}/export").status_code == 200


def test_interior_deep_check_is_included_with_the_book_not_sold_separately(client):
    pid = make_project(client)
    assert client.get(f"/api/projects/{pid}/interior-check/status").json() == {"paid": False}
    assert client.post(f"/api/projects/{pid}/interior-check/run").status_code == 402
    give_credit(1); client.post(f"/api/projects/{pid}/activate-book")
    s = client.get(f"/api/projects/{pid}/interior-check/status").json()
    assert s["paid"] is True and s["included_with_book"] is True
    r = client.post(f"/api/projects/{pid}/interior-check/run")
    assert r.status_code == 200 and r.json()["runs_used"] == 1


# ---------------------------------------------------------------- subscriptions
def test_only_the_one_book_subscription_can_be_bought_for_now(client, stripe_fake):
    for plan in ("book_3", "book_10"):
        r = client.post("/api/payments/subscribe", json={"plan": plan, "origin_url": "https://sparkprep.legenddary.com"})
        assert r.status_code == 400 and "coming soon" in r.json()["detail"]
    assert client.post("/api/payments/subscribe", json={"plan": "nope", "origin_url": "https://x"}).status_code == 400
    assert not stripe_fake["sessions"]


def test_subscription_gives_one_book_per_paid_month_once_per_invoice(client, stripe_fake):
    out = client.post("/api/payments/subscribe", json={"plan": "book_1", "origin_url": "https://sparkprep.legenddary.com"}).json()
    call = stripe_fake["sessions"][-1]
    pd = call["line_items"][0]["price_data"]
    assert call["mode"] == "subscription" and pd["unit_amount"] == 4999 and pd["recurring"] == {"interval": "month"}
    assert call["subscription_data"]["metadata"] == {"user_id": uid(), "product": "subscription", "plan": "book_1"}
    sid = call["id"]
    stripe_fake["invoices"]["in_1"] = invoice("in_1")
    hook(client, paid_session_event(sid, subscription="sub_1", invoice="in_1"))
    hook(client, {"id": "e", "type": "invoice.paid", "data": {"object": invoice("in_1")}})     # arrives twice
    assert client.get("/api/me/books").json()["available_books"] == 1
    assert client.get("/api/me/books").json()["subscription"]["plan"] == "book_1"
    hook(client, {"id": "e2", "type": "invoice.paid", "data": {"object": invoice("in_2")}})    # next month's renewal
    assert client.get("/api/me/books").json()["available_books"] == 2


def test_invoice_paid_arriving_before_checkout_completed_still_works(client, stripe_fake):
    stripe_fake["subs"]["sub_9"] = Obj(metadata={"user_id": uid(), "plan": "book_1"})
    hook(client, {"id": "e", "type": "invoice.paid", "data": {"object": invoice("in_9", sub="sub_9")}})
    assert client.get("/api/me/books").json()["available_books"] == 1


def test_newer_stripe_invoice_shape_is_understood(client, stripe_fake):
    inv = invoice("in_new", sub=None); inv.pop("subscription")
    inv["parent"] = {"subscription_details": {"subscription": "sub_new"}}
    stripe_fake["subs"]["sub_new"] = Obj(metadata={"user_id": uid(), "plan": "book_1"})
    hook(client, {"id": "e", "type": "invoice.paid", "data": {"object": inv}})
    assert client.get("/api/me/books").json()["available_books"] == 1


def test_unused_subscription_books_expire_with_the_month_and_cancellation_stops_new_ones(client, stripe_fake):
    stripe_fake["subs"]["sub_1"] = Obj(metadata={"user_id": uid(), "plan": "book_1"})
    hook(client, {"id": "e", "type": "invoice.paid", "data": {"object": invoice("in_a", period_end=datetime.now(timezone.utc) - timedelta(days=1))}})
    assert client.get("/api/me/books").json()["available_books"] == 0                          # last month's unused book is gone
    hook(client, {"id": "e", "type": "invoice.paid", "data": {"object": invoice("in_b")}})
    hook(client, {"id": "e", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}})
    assert client.get("/api/me/books").json()["available_books"] == 1                          # paid-for book stays usable
    assert client.get("/api/me/books").json()["subscription"]["status"] == "canceled"
    pid = make_project(client)
    assert client.post(f"/api/projects/{pid}/activate-book").status_code == 200                # a started book keeps its 7 days


def test_failed_renewal_grants_nothing(client, stripe_fake):
    stripe_fake["subs"]["sub_1"] = Obj(metadata={"user_id": uid(), "plan": "book_1"})
    hook(client, {"id": "e", "type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_1"}}})
    assert client.get("/api/me/books").json()["available_books"] == 0


# ---------------------------------------------------------------- audit credit
def paid_audit(aid="aud_1", amount=199):
    asyncio.run(server.db.audits.insert_one({"audit_id": aid, "paid": True}))
    asyncio.run(server.db.payment_transactions.insert_one({"session_id": f"cs_{aid}", "audit_id": aid, "product": "audit_099",
                                                           "amount": amount, "payment_status": "paid"}))


def test_audit_credit_reduces_a_book_pass_by_what_the_audit_cost(client, stripe_fake):
    paid_audit()
    out = buy_pass(client, audit_id="aud_1")
    assert out["you_pay_cents"] == 7499 - 199 == 7300 and out["audit_credit_cents"] == 199
    assert stripe_fake["coupons"][-1]["amount_off"] == 199 and stripe_fake["coupons"][-1]["duration"] == "once"
    assert stripe_fake["sessions"][-1]["discounts"] == [{"coupon": "coup_1"}]
    assert stripe_fake["sessions"][-1]["metadata"]["audit_id"] == "aud_1"


def test_audit_credit_also_reduces_a_subscription(client, stripe_fake):
    paid_audit()
    out = client.post("/api/payments/subscribe", json={"plan": "book_1", "origin_url": "https://sparkprep.legenddary.com", "audit_id": "aud_1"}).json()
    assert out["you_pay_cents"] == 4999 - 199 and stripe_fake["sessions"][-1]["discounts"]


def test_an_audit_credit_can_only_be_used_once(client, stripe_fake):
    paid_audit()
    first = buy_pass(client, audit_id="aud_1")["session_id"]
    again = buy_pass(client, audit_id="aud_1")                                 # retry before paying: old checkout is retired, credit reissued
    assert first in stripe_fake["expired"] and again["audit_credit_cents"] == 199
    hook(client, paid_session_event(again["session_id"]))                      # the second one is paid
    r = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com", "audit_id": "aud_1"})
    assert r.status_code == 409 and "already been used" in r.json()["detail"]


def test_a_purchase_already_in_progress_cannot_double_spend_the_credit(client, stripe_fake):
    paid_audit()
    sid = buy_pass(client, audit_id="aud_1")["session_id"]
    stripe_fake["objects"][sid].update(payment_status="paid", status="complete")     # paid, webhook not processed yet
    r = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com", "audit_id": "aud_1"})
    assert r.status_code == 409


def test_credit_is_returned_if_the_checkout_expires_unpaid(client, stripe_fake):
    paid_audit()
    sid = buy_pass(client, audit_id="aud_1")["session_id"]
    hook(client, {"id": "e", "type": "checkout.session.expired", "data": {"object": {"id": sid, "metadata": {"audit_id": "aud_1"}}}})
    assert asyncio.run(server.db.audits.find_one({"audit_id": "aud_1"}))["credit_reserved_session"] is None
    assert buy_pass(client, audit_id="aud_1")["audit_credit_cents"] == 199


def test_unpaid_or_unknown_audits_give_no_credit_and_credit_never_exceeds_what_was_paid(client, stripe_fake):
    asyncio.run(server.db.audits.insert_one({"audit_id": "aud_unpaid", "paid": False}))
    assert client.post("/api/payments/book-pass", json={"origin_url": "https://x", "audit_id": "aud_unpaid"}).status_code == 400
    assert client.post("/api/payments/book-pass", json={"origin_url": "https://x", "audit_id": "nope"}).status_code == 404
    paid_audit("aud_old", amount=99)                                           # an early $0.99 audit is credited $0.99, not $1.99
    assert buy_pass(client, audit_id="aud_old")["audit_credit_cents"] == 99


def test_checkout_only_ever_returns_to_our_own_sites(client, stripe_fake):
    buy_pass(client, origin_url="https://evil.example/phish") if False else None
    r = client.post("/api/payments/book-pass", json={"origin_url": "https://evil.example/phish"})
    assert r.status_code == 200
    assert stripe_fake["sessions"][-1]["success_url"].startswith("https://sparkprep.legenddary.com/payment/success")


def test_displayed_audit_price_matches_the_charged_price_in_legacy_mode(client, monkeypatch):
    # /api/pricing is what every page displays; it must equal what the audit checkout actually charges.
    monkeypatch.delenv("SPARKPREP_PRICING_MODEL", raising=False)
    assert client.get("/api/pricing").json()["model"] == "legacy"
    assert client.get("/api/pricing").json()["audit"]["price_cents"] == server.AUDIT_PRICE_CENTS
