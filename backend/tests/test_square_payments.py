"""Square as the payment provider (PAYMENTS_PROVIDER=square). Square is SIMULATED: payments._sq is replaced by
an in-memory fake, so no keys or network are needed. Each test checks a purchase unlocks exactly as on Stripe."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_sq_"), USE_MEMORY_DB="1", JWT_SECRET="sq-test-" + "x" * 32,
                  ADMIN_EMAIL="root-admin@example.com")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import payments  # noqa: E402
from book_pass.config import BOOK_PASS_PRICE_CENTS, PLANS  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

EMAIL = "square-buyer@example.com"
SIG_KEY = "sq_sig_key_test"
HOOK_URL = "https://sparkprep-v9fp.onrender.com/api/square/webhook"


class FakeSquare:
    def __init__(self):
        self.links, self.orders, self.calls, self.subs, self.catalog = {}, {}, [], [], []

    def __call__(self, method, path, body=None, *, allow_404=False):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/v2/online-checkout/payment-links":
            n = len(self.links) + 1
            lid, oid = f"PL{n}", f"ORD{n}"
            order = body.get("order") or {"line_items": [{"base_price_money": body["quick_pay"]["price_money"]}]}
            self.orders[oid] = {"id": oid, "metadata": order.get("metadata"), "state": "OPEN", "tenders": [],
                                "discounts": order.get("discounts"), "created_at": "2026-11-01T10:00:00Z"}
            link = {"id": lid, "order_id": oid, "version": 1, "url": f"https://square.link/u/{lid}",
                    "long_url": f"https://checkout.square.site/{lid}", "checkout_options": body["checkout_options"]}
            self.links[lid] = link
            return {"payment_link": link}
        if method == "PUT" and path.startswith("/v2/online-checkout/payment-links/"):
            link = self.links[path.rsplit("/", 1)[1]]
            link["checkout_options"] = body["payment_link"]["checkout_options"]
            link["version"] += 1
            return {"payment_link": link}
        if method == "GET" and path.startswith("/v2/online-checkout/payment-links/"):
            link = self.links.get(path.rsplit("/", 1)[1])
            return {"payment_link": link} if link else None
        if method == "DELETE" and path.startswith("/v2/online-checkout/payment-links/"):
            self.links.pop(path.rsplit("/", 1)[1], None)
            return {}
        if method == "GET" and path.startswith("/v2/orders/"):
            return {"order": self.orders[path.rsplit("/", 1)[1]]}
        if path == "/v2/catalog/search":
            return {"objects": []}
        if path == "/v2/catalog/object":
            self.catalog.append(body)
            return {"catalog_object": {"id": f"CAT{len(self.catalog)}"}}
        if path == "/v2/subscriptions/search":
            return {"subscriptions": self.subs}
        raise AssertionError(f"unexpected Square call {method} {path}")

    def pay(self, lid, customer="CUST1"):
        o = self.orders[self.links[lid]["order_id"]]
        o.update(state="COMPLETED", tenders=[{"id": "T1"}], customer_id=customer,
                 net_amount_due_money={"amount": 0, "currency": "USD"})
        return o["id"]


@pytest.fixture(autouse=True)
def square(monkeypatch):
    fake = FakeSquare()
    monkeypatch.setenv("PAYMENTS_PROVIDER", "square")
    monkeypatch.setenv("SQUARE_ACCESS_TOKEN", "EAAA-test")
    monkeypatch.setenv("SQUARE_LOCATION_ID", "LOC1")
    monkeypatch.setenv("SQUARE_WEBHOOK_SIGNATURE_KEY", SIG_KEY)
    monkeypatch.setenv("SQUARE_WEBHOOK_URL", HOOK_URL)
    monkeypatch.setenv("SPARKPREP_PRICING_MODEL", "book_pass")
    monkeypatch.setattr(payments, "_sq", fake)
    payments._plan_variation_cache.clear()
    for coll in ("book_credits", "book_subscriptions", "audits", "payment_transactions"):
        getattr(server.db, coll)._docs.clear()
    return fake


@pytest.fixture(scope="module")
def client():
    asyncio.run(server.db.users.insert_one({
        "email": EMAIL, "password_hash": server.hash_password("TestPass123!"), "name": "S", "tier": "free",
        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    with TestClient(server.app) as c:
        r = c.post("/api/auth/login", json={"email": EMAIL, "password": "TestPass123!"})
        c.headers["Authorization"] = "Bearer " + r.json()["token"]
        yield c


def uid():
    return str(asyncio.run(server.db.users.find_one({"email": EMAIL}))["_id"])


def signed(event):
    body = json.dumps(event).encode()
    sig = base64.b64encode(hmac.new(SIG_KEY.encode(), HOOK_URL.encode() + body, hashlib.sha256).digest()).decode()
    return body, {"x-square-hmacsha256-signature": sig, "content-type": "application/json"}


def payment_event(order_id):
    return {"type": "payment.updated", "data": {"type": "payment", "object": {"payment": {
        "id": "PAY1", "status": "COMPLETED", "order_id": order_id}}}}


def credits():
    return [c for c in server.db.book_credits._docs if c["user_id"] == uid()]


def test_book_pass_checkout_is_a_square_link_with_our_price_and_return_address(client, square):
    r = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com"})
    assert r.status_code == 200, r.text
    lid = r.json()["session_id"]
    assert r.json()["checkout_url"] == f"https://checkout.square.site/{lid}"
    create = next(b for m, p, b in square.calls if m == "POST" and p.endswith("payment-links"))
    assert create["order"]["line_items"][0]["base_price_money"]["amount"] == BOOK_PASS_PRICE_CENTS
    assert create["pre_populated_data"]["buyer_email"] == EMAIL
    assert square.links[lid]["checkout_options"]["redirect_url"] == \
        f"https://sparkprep.legenddary.com/payment/success?session_id={lid}"


def test_signed_square_confirmation_grants_the_book_once_even_if_buyer_never_returns(client, square):
    lid = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com"}).json()["session_id"]
    body, h = signed(payment_event(square.pay(lid)))
    for _ in range(2):  # Square retries; the book must be granted once
        assert client.post("/api/square/webhook", content=body, headers=h).status_code == 200
    assert len(credits()) == 1
    assert client.get(f"/api/payments/status/{lid}").json()["payment_status"] == "paid"


def test_return_page_confirms_payment_without_the_webhook(client, square):
    lid = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com"}).json()["session_id"]
    assert client.get(f"/api/payments/status/{lid}").json()["payment_status"] == "pending"
    square.pay(lid)
    assert client.get(f"/api/payments/status/{lid}").json()["payment_status"] == "paid"
    assert len(credits()) == 1


def test_unsigned_or_forged_square_webhooks_are_refused(client, square):
    lid = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com"}).json()["session_id"]
    body, _ = signed(payment_event(square.pay(lid)))
    for h in ({}, {"x-square-hmacsha256-signature": "bm9wZQ=="}):
        assert client.post("/api/square/webhook", content=body, headers={**h, "content-type": "application/json"}).status_code == 400
    assert credits() == []


def test_audit_unlocks_and_its_credit_comes_off_the_book_as_a_square_discount(client, square):
    aid = "aud_sq_1"
    asyncio.run(server.db.audits.insert_one({"audit_id": aid, "paid": False, "file_type": "cover",
                                             "created_at": datetime.now(timezone.utc).isoformat()}))
    r = client.post(f"/api/audit/{aid}/checkout", json={"origin_url": "https://sparkprep.legenddary.com"})
    assert r.status_code == 200, r.text
    alid = r.json()["session_id"]
    square.pay(alid)
    assert client.get(f"/api/audit/{aid}/verify", params={"session_id": alid}).json() == {"paid": True}
    paid = r.json()["amount_cents"]

    r = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com", "audit_id": aid})
    assert r.status_code == 200, r.text
    assert r.json()["audit_credit_cents"] == paid
    order = square.orders[square.links[r.json()["session_id"]]["order_id"]]
    assert order["discounts"][0]["amount_money"]["amount"] == paid


def test_subscription_registers_and_grants_the_month_once_across_checkout_and_invoice(client, square):
    r = client.post("/api/payments/subscribe", json={"origin_url": "https://sparkprep.legenddary.com", "plan": "book_1"})
    assert r.status_code == 200, r.text
    lid = r.json()["session_id"]
    var_id = square.links[lid]["checkout_options"]["subscription_plan_id"]
    assert var_id and square.links[lid]["checkout_options"]["redirect_url"].endswith(f"session_id={lid}")
    square.subs.append({"id": "SUB1", "plan_variation_id": var_id, "start_date": "2026-11-01",
                        "created_at": "2026-11-01T10:00:00Z"})
    body, h = signed(payment_event(square.pay(lid)))
    assert client.post("/api/square/webhook", content=body, headers=h).status_code == 200
    assert asyncio.run(server.db.book_subscriptions.find_one({"subscription_id": "SUB1"}))["user_id"] == uid()
    n = len(credits())
    assert n == PLANS["book_1"]["books_per_period"]

    inv = {"type": "invoice.payment_made", "data": {"object": {"invoice": {
        "id": "INV1", "subscription_id": "SUB1", "sale_or_service_date": "2026-11-01"}}}}
    client.post("/api/square/webhook", content=signed(inv)[0], headers=signed(inv)[1])
    assert len(credits()) == n  # same month: no extra books
    inv["data"]["object"]["invoice"].update(id="INV2", sale_or_service_date="2026-12-01")
    client.post("/api/square/webhook", content=signed(inv)[0], headers=signed(inv)[1])
    assert len(credits()) == 2 * n  # next month's renewal

    cancel = {"type": "subscription.updated", "data": {"object": {"subscription": {"id": "SUB1", "status": "CANCELED"}}}}
    client.post("/api/square/webhook", content=signed(cancel)[0], headers=signed(cancel)[1])
    assert asyncio.run(server.db.book_subscriptions.find_one({"subscription_id": "SUB1"}))["status"] == "canceled"


def test_checkout_refuses_when_square_is_not_set_up(client, monkeypatch):
    monkeypatch.delenv("SQUARE_ACCESS_TOKEN")
    r = client.post("/api/payments/book-pass", json={"origin_url": "https://sparkprep.legenddary.com"})
    assert r.status_code == 503
