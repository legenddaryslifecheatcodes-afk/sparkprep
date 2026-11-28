"""AI credits (owner's plan, 2026-11-27): free 50 to start + 10 a month per editor, members 500 a month,
welcome gift spent first, no roll-over, never below zero."""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_credits  # noqa: E402


class _Coll:
    def __init__(self):
        self.docs = []

    async def find_one(self, f):
        return next((dict(d) for d in self.docs if all(d.get(k) == v for k, v in f.items())), None)

    async def insert_one(self, d):
        self.docs.append(dict(d))

    async def update_one(self, f, u):
        for d in self.docs:
            if all(d.get(k) == v for k, v in f.items()):
                d.update(u["$set"])


class _DB:
    def __init__(self):
        self.ai_credits = _Coll()


NOV = datetime(2026, 11, 27, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 2, tzinfo=timezone.utc)


def test_free_account_welcome_then_monthly():
    db = _DB()
    run = asyncio.run
    assert run(ai_credits.balance(db, "u", "cover", member=False, now=NOV))["left"] == 60
    left = run(ai_credits.spend(db, "u", "cover", 55, member=False, now=NOV))
    assert left["welcome_left"] == 0 and left["month_left"] == 5         # welcome spent first
    left = run(ai_credits.spend(db, "u", "cover", 30, member=False, now=NOV))
    assert left["left"] == 0                                              # never below zero
    dec = run(ai_credits.balance(db, "u", "cover", member=False, now=DEC))
    assert dec["left"] == 10 and dec["welcome_left"] == 0                 # next month: 10 more, gift doesn't return
    assert run(ai_credits.balance(db, "u", "writing", member=False, now=NOV))["left"] == 60   # pools are separate


def test_member_gets_500_a_month_no_rollover():
    db = _DB()
    run = asyncio.run
    assert run(ai_credits.balance(db, "m", "writing", member=True, now=NOV))["left"] == 500
    run(ai_credits.spend(db, "m", "writing", 120, member=True, now=NOV))
    assert run(ai_credits.balance(db, "m", "writing", member=True, now=NOV))["left"] == 380
    assert run(ai_credits.balance(db, "m", "writing", member=True, now=DEC))["left"] == 500


def test_costs_and_messages():
    assert ai_credits.cost_of("AI Cover Designer") == ("cover", 5)
    assert ai_credits.cost_of("AI Cover Art") == ("cover", 25)
    assert ai_credits.cost_of("AI Writing Help", 4500) == ("writing", 5)
    assert "$9.99/month" in ai_credits.out_of_credits_message("cover", member=False, join_price="$9.99")
    assert "refill on the 1st" in ai_credits.out_of_credits_message("writing", member=True, join_price="$9.99")
    assert ai_credits.next_period_start(NOV).startswith("2026-12-01")
