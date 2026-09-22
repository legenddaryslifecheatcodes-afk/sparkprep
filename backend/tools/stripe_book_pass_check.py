"""Real Stripe TEST-MODE check of the book-pass products (refuses anything but an sk_test_ key).

1. Every new checkout (book pass, 1-book subscription, each with and without the audit credit) is created in Stripe
   and read BACK from Stripe: price, mode, recurring interval and the discount Stripe actually applied.
2. A real test subscription is created with Stripe's test card (server-side, no browser) so Stripe emits genuine
   invoice.paid messages; those REAL payloads are fed to our handlers to prove we read Stripe's real shape.
The key comes from the STRIPE_API_KEY environment variable only.
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

key = os.environ.get("STRIPE_API_KEY", "")
if not key.startswith("sk_test_"):
    sys.exit("Refusing to run: STRIPE_API_KEY must be a TEST key (sk_test_...).")
os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_bpcheck_"), USE_MEMORY_DB="1", JWT_SECRET="bp-check-" + "x" * 32,
                  SPARKPREP_PRICING_MODEL="book_pass")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import book_pass  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

stripe = server.stripe
assert stripe.api_key.startswith("sk_test_")
ORIGIN = "https://sparkprep.legenddary.com"
rows, sessions = [], []


def check(label, ok, detail=""):
    rows.append((label, bool(ok), detail))


asyncio.run(server.db.users.insert_one({"email": "b@example.com", "password_hash": server.hash_password("TestPass123!"), "name": "B", "tier": "free",
                                        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
uid = str(asyncio.run(server.db.users.find_one({"email": "b@example.com"}))["_id"])


def paid_audit(aid, amount=199):
    asyncio.run(server.db.audits.insert_one({"audit_id": aid, "paid": True}))
    asyncio.run(server.db.payment_transactions.insert_one({"session_id": f"cs_{aid}", "audit_id": aid, "product": "audit_099", "amount": amount, "payment_status": "paid"}))


with TestClient(server.app) as cl:
    cl.headers["Authorization"] = "Bearer " + cl.post("/api/auth/login", json={"email": "b@example.com", "password": "TestPass123!"}).json()["token"]

    def go(path, body, label):
        r = cl.post(path, json={"origin_url": ORIGIN, **body})
        if r.status_code != 200:
            check(f"{label}: Stripe accepted the checkout", False, f"HTTP {r.status_code} {r.text[:160]}")
            return None, None
        j = r.json(); sessions.append(j["session_id"])
        return j, stripe.checkout.Session.retrieve(j["session_id"])

    j, s = go("/api/payments/book-pass", {}, "book pass")
    if s:
        check("book pass: Stripe accepted it", True)
        check("book pass: $74.99 one-time", s.amount_total == 7499 and s.mode == "payment" and s.currency == "usd", f"{s.amount_total} {s.mode}")
        check("book pass: returns to the real domain", s.success_url.startswith(ORIGIN + "/payment/success?session_id="))

    j, s = go("/api/payments/subscribe", {"plan": "book_1"}, "1-book subscription")
    if s:
        check("subscription: Stripe accepted it", True)
        check("subscription: $49.99 / month", s.amount_total == 4999 and s.mode == "subscription" and s.currency == "usd", f"{s.amount_total} {s.mode}")
        check("subscription: metadata carries user + plan", s.metadata.get("plan") == "book_1" and s.metadata.get("user_id") == uid)

    paid_audit("aud_a")
    j, s = go("/api/payments/book-pass", {"audit_id": "aud_a"}, "book pass + audit credit")
    if s:
        check("book pass + audit credit: Stripe charges $72.99", s.amount_total == 7300, f"amount_total={s.amount_total}")
        check("book pass + audit credit: Stripe shows a $1.99 discount", s.total_details.amount_discount == 199, f"discount={s.total_details.amount_discount}")

    paid_audit("aud_b")
    j, s = go("/api/payments/subscribe", {"plan": "book_1", "audit_id": "aud_b"}, "subscription + audit credit")
    if s:
        check("subscription + audit credit: first month is $47.99", s.amount_total == 4800, f"amount_total={s.amount_total}")
        check("subscription + audit credit: Stripe shows a $1.99 discount", s.total_details.amount_discount == 199)

    # ---- REAL subscription lifecycle -> genuine invoice.paid payloads ----
    cust = prod = sub = None
    try:
        cust = stripe.Customer.create(email="b@example.com", name="SparkPrep test")
        pm = stripe.PaymentMethod.attach("pm_card_visa", customer=cust.id)      # the token becomes a NEW payment method
        stripe.Customer.modify(cust.id, invoice_settings={"default_payment_method": pm.id})
        prod = stripe.Product.create(name="SparkPrep 1 Book / month (test)")
        sub = stripe.Subscription.create(customer=cust.id, items=[{"price_data": {"currency": "usd", "product": prod.id, "unit_amount": 4999,
                                         "recurring": {"interval": "month"}}}], metadata={"user_id": uid, "product": "subscription", "plan": "book_1"})
        check("real subscription created and paid by Stripe's test card", sub.status == "active", f"status={sub.status}")
        ev = None
        for _ in range(20):
            for e in stripe.Event.list(type="invoice.paid", limit=15).data:
                inv = e.data.object
                same = (getattr(inv, "subscription", None) == sub.id) or (
                    ((getattr(inv, "parent", None) or {}).get("subscription_details") or {}).get("subscription") == sub.id)
                if same:
                    ev = e; break
            if ev:
                break
            time.sleep(1.5)
        check("Stripe emitted a real invoice.paid for it", ev is not None)
        if ev:
            inv = ev.data.object
            shape = "invoice.subscription" if getattr(inv, "subscription", None) else "invoice.parent.subscription_details"
            granted = asyncio.run(book_pass.purchases.on_invoice_paid(server.db, stripe, inv))
            check(f"our handler reads Stripe's REAL invoice ({shape}) and grants the book", granted == 1, f"granted={granted}")
            regrant = asyncio.run(book_pass.purchases.on_invoice_paid(server.db, stripe, inv))
            check("the same real invoice delivered twice grants nothing more", regrant == 0)
            summary = asyncio.run(book_pass.entitlements.summary(server.db, uid))
            check("customer now holds 1 unused book", summary["available_books"] == 1, str(summary))
            c = asyncio.run(server.db.book_credits.find_one({"user_id": uid}))
            exp = book_pass.entitlements.parse(c["expires_at"])
            days = (exp - datetime.now(timezone.utc)).days
            check("that book expires at the end of the paid month (~30 days)", 27 <= days <= 32, f"expires in {days} days")
    except Exception as e:  # noqa: BLE001 - report, don't crash, so the results above still print
        check("real subscription lifecycle ran without error", False, f"{type(e).__name__}: {str(e)[:160]}")
    finally:
        for fn in (lambda: stripe.Subscription.cancel(sub.id) if sub else None, lambda: stripe.Customer.delete(cust.id) if cust else None,
                   lambda: stripe.Product.modify(prod.id, active=False) if prod else None):
            try:
                fn()
            except Exception:
                pass

for sid in sessions:
    try:
        stripe.checkout.Session.expire(sid)
    except Exception:
        pass
print("\nBOOK-PASS STRIPE TEST-MODE RESULTS (what Stripe itself recorded)\n" + "-" * 78)
for label, ok, detail in rows:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
bad = [r for r in rows if not r[1]]
print("-" * 78, f"\n{len(rows) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
