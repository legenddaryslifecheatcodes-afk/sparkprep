"""AI credits (owner's plan, 2026-11-27). The cover editor and the book editor are free to use for everyone, signed
in or not. The AI helpers inside them run on credits, so free accounts get a real taste and the $9.99 membership
gives real value, while one person can't run up the AI bill.

Two separate credit pools, one per editor:
  "cover"   -- AI cover designer (plan: 5 credits) and AI cover artwork (25 credits)
  "writing" -- AI writing help in the book editor: 1 credit per 1,000 characters checked (about a page), at least 1

How many credits:
  free account : a one-time welcome gift of FREE_WELCOME per pool, plus FREE_MONTHLY more per pool every month
  member       : MEMBER_MONTHLY per pool every month (the $9.99/month membership)
  a customer with a book in progress, admins and beta testers: unlimited, exactly as before (nothing they paid for
  is taken away).

Unused monthly credits don't roll over. The welcome gift is spent first and never comes back. A use is only charged
after it worked, so an error never costs credits. Owner's numbers: members 500, free 50 to start then 10 a month.
"""
import math
from datetime import datetime, timezone

FREE_WELCOME = 50
FREE_MONTHLY = 10
MEMBER_MONTHLY = 500
POOLS = ("cover", "writing")

FEATURES = {   # feature name used by the endpoints -> (pool, fixed cost or None when it depends on size)
    "AI Cover Designer": ("cover", 5),
    "AI Cover Art": ("cover", 25),
    "AI Writing Help": ("writing", None),
}
POOL_NAMES = {"cover": "cover editor", "writing": "book editor"}


def writing_cost(chars: int) -> int:
    return max(1, math.ceil(max(0, int(chars or 0)) / 1000))


def cost_of(feature: str, chars: int = 0) -> tuple:
    pool, fixed = FEATURES[feature]
    return pool, (fixed if fixed is not None else writing_cost(chars))


def period(now=None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def next_period_start(now=None) -> str:
    now = now or datetime.now(timezone.utc)
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc).isoformat()


async def _doc(db, user_id: str, pool: str):
    return await db.ai_credits.find_one({"user_id": user_id, "pool": pool}) or {}


def _left_from(doc: dict, member: bool, now=None) -> dict:
    this = period(now)
    month_used = int(doc.get("month_used", 0)) if doc.get("period") == this else 0
    monthly = MEMBER_MONTHLY if member else FREE_MONTHLY
    welcome_left = 0 if member else max(0, FREE_WELCOME - int(doc.get("welcome_used", 0)))
    month_left = max(0, monthly - month_used)
    return {"left": welcome_left + month_left, "welcome_left": welcome_left, "month_left": month_left,
            "monthly": monthly}


async def balance(db, user_id: str, pool: str, *, member: bool, now=None) -> dict:
    return _left_from(await _doc(db, user_id, pool), member, now)


async def spend(db, user_id: str, pool: str, amount: int, *, member: bool, now=None) -> dict:
    """Takes `amount` credits (welcome gift first, then this month's). Never goes below zero."""
    doc = await _doc(db, user_id, pool)
    this = period(now)
    state = _left_from(doc, member, now)
    take_welcome = min(state["welcome_left"], amount)
    take_month = min(state["month_left"], amount - take_welcome)
    month_used = (int(doc.get("month_used", 0)) if doc.get("period") == this else 0) + take_month
    fields = {"period": this, "month_used": month_used,
              "welcome_used": int(doc.get("welcome_used", 0)) + take_welcome,
              "updated_at": (now or datetime.now(timezone.utc)).isoformat()}
    if doc:
        await db.ai_credits.update_one({"user_id": user_id, "pool": pool}, {"$set": fields})
    else:
        await db.ai_credits.insert_one({"user_id": user_id, "pool": pool, **fields})
    return _left_from({**doc, **fields}, member, now)


def out_of_credits_message(pool: str, *, member: bool, join_price: str) -> str:
    where = POOL_NAMES[pool]
    if member:
        return (f"You've used this month's AI credits for the {where}. They refill on the 1st. "
                f"Everything else in the {where} still works.")
    return (f"You're out of free AI credits for the {where}. Join the {join_price}/month membership to get "
            f"{MEMBER_MONTHLY} credits every month for the book editor and {MEMBER_MONTHLY} for the cover editor. "
            f"Everything else in the {where} stays free.")
