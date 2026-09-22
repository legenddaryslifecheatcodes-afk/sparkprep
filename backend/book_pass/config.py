"""Single source of truth for SparkPrep's book-pass pricing.

Everything a customer can be charged, and everything the pricing page displays, comes from this file (the
frontend reads it through GET /api/pricing), so a displayed price and a charged price can never disagree.

The whole model sits behind ONE switch:
    SPARKPREP_PRICING_MODEL=book_pass   -> new model (this package)
    anything else / unset               -> legacy plans, exactly as before (instant rollback)
"""
from __future__ import annotations

import os

PASS_WINDOW_DAYS = 7                    # unlimited exports for this long once a book is started
BOOK_PASS_PRICE_CENTS = 7499            # one book, no subscription
AUDIT_PRICE_CENTS_BOOK_PASS = 199       # the audit under the new model (legacy stays 99)
AUDIT_CREDIT_MAX_CENTS = 199            # what an audit is credited toward a purchase (never more than was paid)
MAX_ADVANCED_RUNS_PER_WINDOW = 10       # cap on the heavy interior deep-check per started book

# Subscriptions: N books per billing month, unused books do not roll over. Only "available" plans can be bought;
# the bigger ones are shown as "coming soon".
PLANS = {
    "book_1": {"name": "1 Book / month", "price_cents": 4999, "books_per_period": 1, "available": True,
               "audience": "Independent authors"},
    "book_3": {"name": "3 Books / month", "price_cents": 11999, "books_per_period": 3, "available": False,
               "audience": "Working self-publishers"},
    "book_10": {"name": "10 Books / month", "price_cents": 29999, "books_per_period": 10, "available": False,
                "audience": "Small publishing houses"},
}

NEW_PRODUCTS = ("book_pass", "subscription")

# While a customer holds a book (unused credit or an active window) every plan-locked feature (AI cover, blurb
# writer, interior composer, larger uploads ...) is unlocked. Team seats / white-label / bulk tools stay off.
PAID_EFFECTIVE_TIER = "author"

BOOK_INCLUDES = [
    "One book — cover, interior, or both — same price",
    f"Unlimited exports for {PASS_WINDOW_DAYS} days",
    "Full interior deep-check included",
    "Auto-Fix with independent verification",
]


def pricing_mode() -> str:
    return os.environ.get("SPARKPREP_PRICING_MODEL", "legacy").strip().lower()


def book_pass_on() -> bool:
    return pricing_mode() == "book_pass"


def audit_price_cents(legacy_cents: int = 99) -> int:
    return AUDIT_PRICE_CENTS_BOOK_PASS if book_pass_on() else legacy_cents


def public_pricing() -> dict:
    """What the pricing page (and anything else that shows a price) reads."""
    return {
        "model": "book_pass" if book_pass_on() else "legacy",
        "currency": "usd",
        "audit": {"price_cents": audit_price_cents(), "credit_cents": AUDIT_CREDIT_MAX_CENTS,
                  "note": "Credited in full toward your book if you fix it with SparkPrep."},
        "book": {"price_cents": BOOK_PASS_PRICE_CENTS, "window_days": PASS_WINDOW_DAYS, "includes": BOOK_INCLUDES},
        "plans": [{"id": pid, **p} for pid, p in PLANS.items()],
    }
