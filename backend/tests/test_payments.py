"""Payment-flow tests. Stripe is SIMULATED (no keys, no network, no real charges): checkout creation is
captured and Stripe's webhook messages are hand-built and signed exactly the way Stripe signs them.

Covers what must be true before real money moves: the server (never the browser) decides the price, every
product unlocks when Stripe's confirmation arrives -- even if the customer closes the tab -- forged or
tampered messages are rejected, and repeats/cancellations behave.
"""
import asyncio
import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_pay_"), USE_MEMORY_DB="1", JWT_SECRET="pay-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SECRET = "whsec_test_secret"


class FakeSession:
    def __init__(self, sid):
        self.id, self.url = sid, f"https://checkout.stripe.test/{sid}"
        self.payment_status, self.status, self.subscription = "unpaid", "open", None


@pytest.fixture
def stripe_calls(monkeypatch):
    """Captures every checkout session the app asks Stripe to create."""
    calls = []

    def fake_create(**kw):
        sid = f"cs_test_{len(calls) + 1}_{int(time.time() * 1000) % 100000}"
        calls.append({"id": sid, **kw})
        return FakeSession(sid)
    monkeypatch.setattr(server.stripe, "api_key", "sk_test_dummy")
    monkeypatch.setattr(server.stripe.checkout.Session, "create", fake_create)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    return calls


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": "buyer@example.com", "password_hash": server.hash_password("TestPass123!"), "name": "Buyer", "tier": "free",
        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        r = c.post("/api/auth/login", json={"email": "buyer@example.com", "password": "TestPass123!"})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = "Bearer " + r.json()["token"]
        yield c


def _user():
    return asyncio.run(server.db.users.find_one({"email": "buyer@example.com"}))


def _reset_user():
    asyncio.run(server.db.users.update_one({"email": "buyer@example.com"}, {"$set": {"tier": "free", "subscription_status": None, "stripe_subscription_id": None}}))


def _txn(sid):
    return asyncio.run(server.db.payment_transactions.find_one({"session_id": sid}))


def signed(event: dict, secret=SECRET, ts=None):
    body = json.dumps(event).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"}


def completed(sid, subscription=None):
    return {"id": "evt_1", "type": "checkout.session.completed",
            "data": {"object": {"id": sid, "payment_status": "paid", "status": "complete", "subscription": subscription}}}


def send(client, event, **kw):
    body, headers = signed(event, **kw)
    return client.post("/api/stripe/webhook", content=body, headers=headers)


# ------------------------------------------------------- the server decides the price
@pytest.mark.parametrize("tier", ["author", "creator_pro", "publisher", "studio"])
def test_subscription_charges_the_servers_price_not_the_browsers(client, stripe_calls, tier):
    r = client.post("/api/payments/checkout", json={"tier": tier, "origin_url": "https://sparkprep.legenddary.com/",
                                                   "amount": 1, "price_cents": 1, "unit_amount": 1})   # a tampering attempt
    assert r.status_code == 200, r.text
    call = stripe_calls[-1]
    li = call["line_items"][0]["price_data"]
    assert li["unit_amount"] == server.TIERS[tier]["price_cents"] > 100
    assert li["recurring"] == {"interval": "month"} and li["currency"] == "usd"
    assert call["mode"] == "subscription" and call["metadata"] == {"user_id": str(_user()["_id"]), "tier": tier}
    assert call["success_url"].startswith("https://sparkprep.legenddary.com/payment/success?session_id=")
    assert _txn(stripe_calls[-1]["id"])["amount"] == server.TIERS[tier]["price_cents"]


def test_unknown_or_free_tier_cannot_be_bought(client, stripe_calls):
    for bad in ("free", "enterprise", "", "AUTHOR"):
        assert client.post("/api/payments/checkout", json={"tier": bad, "origin_url": "https://x.test"}).status_code == 400
    assert not stripe_calls


def test_checkout_refuses_when_stripe_is_not_configured(client, monkeypatch):
    monkeypatch.setattr(server.stripe, "api_key", "sk_test_not_configured")
    assert client.post("/api/payments/checkout", json={"tier": "author", "origin_url": "https://x.test"}).status_code == 503


# ------------------------------------------------------- confirmation unlocks the purchase
def test_paid_webhook_upgrades_the_account_once(client, stripe_calls):
    _reset_user()
    client.post("/api/payments/checkout", json={"tier": "publisher", "origin_url": "https://x.test"})
    sid = stripe_calls[-1]["id"]
    assert _user()["tier"] == "free"                                   # paying hasn't happened yet
    assert send(client, completed(sid, "sub_123")).status_code == 200
    u = _user()
    assert u["tier"] == "publisher" and u["subscription_status"] == "active" and u["stripe_subscription_id"] == "sub_123"
    assert _txn(sid)["payment_status"] == "paid"
    assert send(client, completed(sid, "sub_123")).status_code == 200   # Stripe retries: must be harmless
    assert _user()["tier"] == "publisher"


def test_cancellation_downgrades_and_failed_payment_flags_past_due(client, stripe_calls):
    _reset_user()
    client.post("/api/payments/checkout", json={"tier": "author", "origin_url": "https://x.test"})
    send(client, completed(stripe_calls[-1]["id"], "sub_777"))
    send(client, {"id": "e2", "type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_777"}}})
    assert _user()["subscription_status"] == "past_due" and _user()["tier"] == "author"
    send(client, {"id": "e3", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_777"}}})
    assert _user()["tier"] == "free" and _user()["subscription_status"] == "canceled"


def test_audit_099_is_priced_by_the_server_and_unlocks_on_confirmation(client, stripe_calls):
    asyncio.run(server.db.audits.insert_one({"audit_id": "aud_pay_1", "paid": False}))
    r = client.post("/api/audit/aud_pay_1/checkout", json={"origin_url": "https://x.test", "amount": 1})
    assert r.status_code == 200
    assert stripe_calls[-1]["line_items"][0]["price_data"]["unit_amount"] == server.AUDIT_PRICE_CENTS == 99
    assert stripe_calls[-1]["mode"] == "payment"
    send(client, completed(stripe_calls[-1]["id"]))
    assert asyncio.run(server.db.audits.find_one({"audit_id": "aud_pay_1"}))["paid"] is True
    assert client.post("/api/audit/aud_pay_1/checkout", json={"origin_url": "https://x.test"}).json() == {"already_paid": True}


def test_interior_check_unlocks_on_stripes_confirmation_even_if_customer_never_returns(client, stripe_calls):
    """The bug this file was written to catch: the customer pays, closes the tab before the success page loads.
    Stripe's own confirmation must unlock the purchase -- it must not depend on the browser coming back."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch)); c.drawString(72, 400, "hello"); c.showPage(); c.save()
    pid = client.post("/api/projects", json={"name": "P", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb",
                                              "binding": "paperback", "page_count": 100, "project_type": "interior"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/slot-upload/interior", files={"file": ("b.pdf", buf.getvalue(), "application/pdf")}).status_code == 200
    r = client.post(f"/api/projects/{pid}/interior-check/checkout", json={"origin_url": "https://x.test"})
    assert r.status_code == 200, r.text
    call = stripe_calls[-1]
    assert call["line_items"][0]["price_data"]["unit_amount"] == server.ADVANCED_INTERIOR_PRICE_CENTS["free"]
    assert client.get(f"/api/projects/{pid}/interior-check/status").json() == {"paid": False}
    assert send(client, completed(call["id"])).status_code == 200      # Stripe says paid; the customer's browser never returns
    assert client.get(f"/api/projects/{pid}/interior-check/status").json()["paid"] is True


# ------------------------------------------------------- forged / tampered messages are rejected
def test_forged_and_tampered_webhooks_are_rejected_and_change_nothing(client, stripe_calls):
    _reset_user()
    client.post("/api/payments/checkout", json={"tier": "studio", "origin_url": "https://x.test"})
    sid = stripe_calls[-1]["id"]
    body, headers = signed(completed(sid))
    cases = {
        "no signature at all": (body, {"content-type": "application/json"}),
        "signed with the wrong secret": signed(completed(sid), secret="whsec_attacker"),
        "body edited after signing": (body.replace(b"cs_test", b"cs_evil"), headers),
        "stale (replayed) signature": signed(completed(sid), ts=int(time.time()) - 3600),
        "garbage signature": (body, {"stripe-signature": "t=1,v1=deadbeef", "content-type": "application/json"}),
    }
    for name, (b, h) in cases.items():
        assert client.post("/api/stripe/webhook", content=b, headers=h).status_code == 400, name
    assert _user()["tier"] == "free" and _txn(sid)["payment_status"] == "pending"
