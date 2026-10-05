"""Single source of truth for SparkPrep's book-pass pricing.

Everything a customer can be charged, and everything the pricing page displays, comes from this file (the
frontend reads it through GET /api/pricing), so a displayed price and a charged price can never disagree.

The whole model sits behind ONE switch:
    SPARKPREP_PRICING_MODEL=book_pass   -> new model (this package)
    anything else / unset               -> legacy plans, exactly as before (instant rollback)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

PASS_WINDOW_DAYS = 7                    # unlimited exports for this long once a book is started
BOOK_PASS_PRICE_CENTS = 7499            # one book, no subscription
AUDIT_PRICE_CENTS_LEGACY = 199          # must equal server.AUDIT_PRICE_CENTS -- /api/pricing shows this in legacy mode
MAX_ADVANCED_RUNS_PER_WINDOW = 10       # cap on the heavy interior deep-check per started book

# The two audits (detection only -- fixing is the book). The standard audit checks a whole cover and page 1 of an
# interior; the Advanced Audit checks every interior page (up to ADVANCED_AUDIT_MAX_PAGES). Whatever a customer
# actually paid for an audit is credited toward their book.
AUDIT_REGULAR_CENTS = 999
ADVANCED_AUDIT_REGULAR_CENTS = 1999
ADVANCED_AUDIT_MAX_PAGES = 300

# The 99-Day Special (the "Audit Season"): special audit prices until it ends, then the regular prices above
# apply automatically.
SPECIAL_NAME = "99-Day Special"
SPECIAL_START = datetime(2026, 9, 23, tzinfo=timezone.utc)
SPECIAL_END = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
AUDIT_SPECIAL_CENTS = 199
ADVANCED_AUDIT_SPECIAL_CENTS = 999

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
    "SparkPrep Standard Check of every page included",
    "Auto-Fix with independent verification",
]


def pricing_mode() -> str:
    return os.environ.get("SPARKPREP_PRICING_MODEL", "legacy").strip().lower()


def book_pass_on() -> bool:
    return pricing_mode() == "book_pass"


def special_active(now: Optional[datetime] = None) -> bool:
    return (now or datetime.now(timezone.utc)) <= SPECIAL_END


def audit_price_cents(legacy_cents: int = AUDIT_PRICE_CENTS_LEGACY, now: Optional[datetime] = None) -> int:
    if not book_pass_on():
        return legacy_cents
    return AUDIT_SPECIAL_CENTS if special_active(now) else AUDIT_REGULAR_CENTS


def advanced_audit_price_cents(now: Optional[datetime] = None) -> int:
    return ADVANCED_AUDIT_SPECIAL_CENTS if special_active(now) else ADVANCED_AUDIT_REGULAR_CENTS


def audit_level_price_cents(level: str, legacy_cents: int = AUDIT_PRICE_CENTS_LEGACY) -> int:
    return advanced_audit_price_cents() if level == "advanced" else audit_price_cents(legacy_cents)


def public_pricing() -> dict:
    """What the pricing page (and anything else that shows a price) reads. Plans that can't be bought yet are
    listed as "coming soon" with NO price -- their pricing isn't decided, so nothing may show or quote one."""
    book_model = book_pass_on()
    return {
        "model": "book_pass" if book_model else "legacy",
        "currency": "usd",
        "audit": {"price_cents": audit_price_cents(), "credit_cents": audit_price_cents(),
                  "regular_cents": AUDIT_REGULAR_CENTS if book_model else None,
                  "note": "Credited in full toward your book if you fix it with SparkPrep."},
        "advanced_audit": ({"price_cents": advanced_audit_price_cents(), "credit_cents": advanced_audit_price_cents(),
                            "regular_cents": ADVANCED_AUDIT_REGULAR_CENTS, "max_pages": ADVANCED_AUDIT_MAX_PAGES}
                           if book_model else None),
        "special": ({"active": special_active(), "name": SPECIAL_NAME, "ends_at": SPECIAL_END.isoformat()}
                    if book_model else None),
        "book": {"price_cents": BOOK_PASS_PRICE_CENTS, "window_days": PASS_WINDOW_DAYS, "includes": BOOK_INCLUDES},
        "plans": [{"id": pid, **p} if p["available"] else
                  {"id": pid, "name": p["name"], "books_per_period": p["books_per_period"], "available": False,
                   "audience": p["audience"], "price_cents": None, "note": "Coming soon"}
                  for pid, p in PLANS.items()],
    }


def _usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


def book_offer_text() -> str:
    """The one sentence every "you need to pay for this" message uses, built from the real prices above."""
    return f"{_usd(BOOK_PASS_PRICE_CENTS)} for one book (cover, interior, or both), or {_usd(PLANS['book_1']['price_cents'])}/month"
