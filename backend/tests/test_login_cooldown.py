"""Login guessing gets a cool-down, never a lockout (owner's rule, 2026-09-29): after 10 wrong passwords for
one account in 15 minutes that account waits; nothing needs a phone or codes, and it clears on its own."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_login_"), USE_MEMORY_DB="1", JWT_SECRET="login-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PW = "RightPass123!"


def make_user(email):
    asyncio.run(server.db.users.insert_one({
        "email": email, "password_hash": server.hash_password(PW), "name": "L", "tier": "free",
        "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))


def login(c, email, pw):
    return c.post("/api/auth/login", json={"email": email, "password": pw})


def test_guessing_gets_a_cool_down_then_clears_on_its_own(monkeypatch):
    make_user("guessed@example.com")
    with TestClient(server.app) as c:
        for _ in range(server.LOGIN_MAX_FAILS):
            assert login(c, "guessed@example.com", "wrong-guess").status_code == 401
        r = login(c, "guessed@example.com", PW)                        # even the right password waits
        assert r.status_code == 429 and "Nothing is locked" in r.json()["detail"]
        assert login(c, "someone-else@example.com", "x").status_code == 401   # other accounts unaffected

        later = datetime.now(timezone.utc).timestamp() + server.LOGIN_WINDOW_S + 1
        monkeypatch.setattr(server, "datetime", type("D", (), {"now": staticmethod(
            lambda tz=None: datetime.fromtimestamp(later, tz=timezone.utc))}))
        assert login(c, "guessed@example.com", PW).status_code == 200  # after the wait: straight back in


def test_a_correct_password_clears_the_count():
    make_user("forgetful@example.com")
    with TestClient(server.app) as c:
        for _ in range(server.LOGIN_MAX_FAILS - 1):
            login(c, "forgetful@example.com", "typo")
        assert login(c, "forgetful@example.com", PW).status_code == 200
        for _ in range(server.LOGIN_MAX_FAILS - 1):                    # the count started over
            login(c, "forgetful@example.com", "typo")
        assert login(c, "forgetful@example.com", PW).status_code == 200


def test_new_passwords_need_8_characters():
    with TestClient(server.app) as c:
        assert c.post("/api/auth/register", json={"email": "short@example.com", "password": "abc1234"}).status_code == 422
        assert c.post("/api/auth/register", json={"email": "ok@example.com", "password": "abcd1234"}).status_code == 200
