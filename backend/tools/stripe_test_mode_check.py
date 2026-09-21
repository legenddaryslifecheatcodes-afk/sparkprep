"""Real Stripe TEST-MODE check (no real money: refuses to run with anything but an sk_test_ key).

Asks Stripe to create every kind of checkout the app can create, then reads each session BACK from Stripe and
compares what Stripe recorded (amount, currency, mode, payment methods, metadata) with the server's own prices.
The key is read from the STRIPE_API_KEY environment variable only; it is never printed or written anywhere.

Usage:  STRIPE_API_KEY=sk_test_... python backend/tools/stripe_test_mode_check.py [--keep-one]
"""
import asyncio
import io
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

key = os.environ.get("STRIPE_API_KEY", "")
if not key.startswith("sk_test_"):
    sys.exit("Refusing to run: STRIPE_API_KEY must be a TEST key (sk_test_...).")
os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_stripe_"), USE_MEMORY_DB="1", JWT_SECRET="stripe-check-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

assert server.stripe.api_key.startswith("sk_test_")
KEEP = "--keep-one" in sys.argv
ORIGIN = "https://sparkprep.legenddary.com"
rows, failures, sessions = [], [], []


def check(label, ok, detail=""):
    rows.append((label, ok, detail))
    if not ok:
        failures.append(label)


def stripe_says(sid):
    return server.stripe.checkout.Session.retrieve(sid)


asyncio.run(server.db.users.insert_one({"email": "t@example.com", "password_hash": server.hash_password("TestPass123!"), "name": "T", "tier": "free",
                                        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
with TestClient(server.app) as cl:
    cl.headers["Authorization"] = "Bearer " + cl.post("/api/auth/login", json={"email": "t@example.com", "password": "TestPass123!"}).json()["token"]

    for tier in ("author", "creator_pro", "publisher", "studio"):
        r = cl.post("/api/payments/checkout", json={"tier": tier, "origin_url": ORIGIN})
        if r.status_code != 200:
            check(f"plan {tier}: Stripe accepted the checkout", False, f"HTTP {r.status_code}: {r.text[:200]}")
            continue
        sid = r.json()["session_id"]; sessions.append((f"plan {tier}", sid, r.json()["checkout_url"]))
        s = stripe_says(sid)
        want = server.TIERS[tier]["price_cents"]
        check(f"plan {tier}: Stripe accepted the checkout", True, sid[:22] + "…")
        check(f"plan {tier}: Stripe recorded ${want/100:.2f}/month", s.amount_total == want and s.currency == "usd" and s.mode == "subscription",
              f"amount_total={s.amount_total} currency={s.currency} mode={s.mode}")
        check(f"plan {tier}: card payments offered (methods come from your Stripe dashboard)", "card" in s.payment_method_types, str(s.payment_method_types))
        check(f"plan {tier}: metadata carries user + tier", s.metadata.get("tier") == tier and bool(s.metadata.get("user_id")), str(dict(s.metadata)))
        check(f"plan {tier}: returns to the real domain", s.success_url.startswith(ORIGIN + "/payment/success?session_id="), s.success_url[:60])

    asyncio.run(server.db.audits.insert_one({"audit_id": "aud_stripe_1", "paid": False}))
    r = cl.post("/api/audit/aud_stripe_1/checkout", json={"origin_url": ORIGIN})
    if r.status_code == 200:
        sid = r.json()["session_id"]; sessions.append(("audit", sid, r.json()["checkout_url"])); s = stripe_says(sid)
        check("audit: Stripe recorded $0.99 one-time", s.amount_total == 99 and s.mode == "payment" and s.currency == "usd", f"amount_total={s.amount_total} mode={s.mode}")
    else:
        check("audit: Stripe accepted the checkout", False, f"HTTP {r.status_code}: {r.text[:200]}")

    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=(6 * inch, 9 * inch)); c.drawString(72, 400, "x"); c.showPage(); c.save()
    pid = cl.post("/api/projects", json={"name": "P", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb", "binding": "paperback",
                                          "page_count": 100, "project_type": "interior"}).json()["id"]
    cl.post(f"/api/projects/{pid}/slot-upload/interior", files={"file": ("b.pdf", buf.getvalue(), "application/pdf")})
    r = cl.post(f"/api/projects/{pid}/interior-check/checkout", json={"origin_url": ORIGIN})
    if r.status_code == 200:
        sid = r.json()["session_id"]; sessions.append(("interior check", sid, r.json()["checkout_url"])); s = stripe_says(sid)
        want = server.ADVANCED_INTERIOR_PRICE_CENTS["free"]
        check(f"interior check: Stripe recorded ${want/100:.2f} one-time", s.amount_total == want and s.mode == "payment", f"amount_total={s.amount_total} mode={s.mode}")
    else:
        check("interior check: Stripe accepted the checkout", False, f"HTTP {r.status_code}: {r.text[:200]}")

print("\nSTRIPE TEST-MODE RESULTS (what Stripe itself recorded vs our prices)\n" + "-" * 72)
for label, ok, detail in rows:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and (not ok or 'accepted' in label) else ""))
print("-" * 72, f"\n{len(rows) - len(failures)} passed, {len(failures)} failed")

kept = None
for label, sid, url in sessions:
    if KEEP and label == "audit":
        kept = (sid, url); continue
    try:
        server.stripe.checkout.Session.expire(sid)
    except Exception:
        pass
if kept:
    print("\nKEPT one unpaid $0.99 test checkout for a manual test-card payment:\n", kept[1])
sys.exit(1 if failures else 0)
