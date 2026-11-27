"""Site Insights (owner's ask, 2026-11-27): see, right on the site, how far visitors get, where they stop,
what trouble they hit and what's working -- without opening Stripe, Render or Cloudflare.

Built on top of the traffic counter in traffic.py (same privacy rules: no cookies, no saved IP address; a visitor
is a one-way hash of salt + day + IP + browser, so one person's day can be followed but never their identity or
their next day). It adds three things:

1. Steps: the server notes when a visitor reaches a step that matters (uploads a file to the free check, unlocks
   results, signs up, starts a book, opens a payment page). Rules live in STEPS below. Nothing in the existing
   endpoints changes: a small ASGI wrapper only *watches* each /api response's status.
2. Trouble: every error or "blocked" answer a real visitor gets from the API (with the message they were shown),
   plus page crashes and "couldn't reach the server" reported by /track.js through POST /api/trouble.
3. GET /api/admin/insights (owner only): the step-by-step funnel, the biggest drop, where people leave, which
   sources and first pages bring people who go further, the trouble list, and recent visitor journeys.

Kept self-contained on purpose so it can be reused on another site: change SITE_NAME, STEPS, FUNNEL and
FRIENDLY_API, then call install(app, api_router, db=..., require_admin=..., client_ip=...).
"""
import asyncio
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request

import traffic

SITE_NAME = "Legenddary SparkPrep"
MAX_DAYS = 90
MAX_RECORDS_PER_IP_PER_HOUR = 200      # steps + trouble together; a real person never gets near this
MAX_PAGE_TROUBLE_PER_IP_PER_HOUR = 30

# (step key, HTTP method, path pattern). A step counts when the API answers 2xx.
STEPS = [
    ("uploaded", "POST", r"^/api/audit/[^/]+/upload$"),
    ("saw_results", "POST", r"^/api/audit/[^/]+/claim-free(-email)?$"),
    ("signed_up", "POST", r"^/api/auth/register$"),
    ("signed_up", "POST", r"^/api/auth/guest/claim$"),
    ("started_book", "POST", r"^/api/projects$"),
    ("opened_checkout", "POST", r"^/api/payments/(checkout|book-pass|subscribe)$"),
    ("opened_checkout", "POST", r"^/api/audit/[^/]+/checkout$"),
    ("opened_checkout", "POST", r"^/api/projects/[^/]+/(results-unlock|interior-check)/checkout$"),
    ("used_ai", "POST", r"^/api/ai/"),
]

# The funnel, in order. "visited" and "opened_check" come from page views; the rest from STEPS; "paid" is
# counted from completed payments (Stripe tells the server, not the visitor's browser).
FUNNEL = [
    ("visited", "Visited the site"),
    ("opened_check", "Opened the free check page"),
    ("uploaded", "Uploaded a file to check"),
    ("saw_results", "Unlocked their results"),
    ("signed_up", "Made an account"),
    ("started_book", "Started a book"),
    ("opened_checkout", "Opened a payment page"),
    ("paid", "Paid"),
]
STEP_LABELS = dict(FUNNEL) | {"used_ai": "Used an AI helper"}
FURTHER_STEPS = {"uploaded", "saw_results", "signed_up", "started_book", "opened_checkout", "used_ai"}

# Plain names for API paths, so the trouble list reads like "AI cover designer" instead of a URL.
FRIENDLY_API = [
    (r"^/api/ai/cover-plan", "AI cover designer"), (r"^/api/ai/cover-art", "AI cover art"),
    (r"^/api/ai/writing-help", "AI writing help"), (r"^/api/ai/", "AI helper"),
    (r"^/api/audit/start", "Free check: start"), (r"^/api/audit/[^/]+/upload", "Free check: upload"),
    (r"^/api/audit/[^/]+/claim-free", "Free check: unlock results"), (r"^/api/audit/[^/]+/checkout", "Audit payment page"),
    (r"^/api/audit/", "Free check"), (r"^/api/auth/register", "Sign up"), (r"^/api/auth/login", "Log in"),
    (r"^/api/auth/", "Account"), (r"^/api/payments/", "Payment"), (r"^/api/projects/[^/]+/export", "Export / download"),
    (r"^/api/projects/[^/]+/upload", "Book upload"), (r"^/api/projects", "Book editor"),
    (r"^/api/interior", "Interior Studio"), (r"^/api/specs", "Printer specs"), (r"^/api/leads", "Email sign-up"),
]

# Answers that are normal and not trouble (e.g. "not logged in" when the site just checks who you are).
NOT_TROUBLE = [
    ("GET", r"^/api/auth/me$", {401}),
    ("GET", r"^/api/me/", {401}),
    ("GET", r"^/api/free-audit/status$", {401, 404}),
    ("GET", r"^/api/payments/status/", {404}),
]
SKIP_PATHS = re.compile(r"^/api/(pv|trouble|health|admin/)")

_STEP_RES = [(k, m, re.compile(p)) for k, m, p in STEPS]
_FRIENDLY_RES = [(re.compile(p), n) for p, n in FRIENDLY_API]
_NOT_TROUBLE_RES = [(m, re.compile(p), s) for m, p, s in NOT_TROUBLE]
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")

_hits = defaultdict(list)       # ip -> timestamps (memory only, never saved)
_page_hits = defaultdict(list)
_tasks = set()


def _now():
    return datetime.now(timezone.utc)


def _allow(store, ip, cap):
    now_s = time.time()
    recent = [t for t in store[ip] if t > now_s - 3600]
    if len(recent) >= cap:
        store[ip] = recent
        return False
    recent.append(now_s)
    store[ip] = recent
    if len(store) > 5000:
        for k in list(store)[:2500]:
            store.pop(k, None)
    return True


def friendly_api(path: str) -> str:
    for rx, name in _FRIENDLY_RES:
        if rx.search(path):
            return name
    return path


def step_for(method: str, path: str, status: int):
    if not (200 <= status < 300):
        return None
    for key, m, rx in _STEP_RES:
        if m == method and rx.search(path):
            return key
    return None


def is_trouble(method: str, path: str, status: int) -> bool:
    if status < 400:
        return False
    for m, rx, codes in _NOT_TROUBLE_RES:
        if m == method and rx.search(path) and status in codes:
            return False
    return True


def scrub(text: str, limit: int = 220) -> str:
    """Error text can carry an email or a token; keep the words, drop anything that identifies someone."""
    text = _EMAIL_RE.sub("[email]", str(text or ""))
    text = _TOKEN_RE.sub("[…]", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _detail_from_body(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "ignore") or "{}")
    except Exception:  # noqa: BLE001 - not JSON, or cut short
        return ""
    d = data.get("detail") if isinstance(data, dict) else ""
    if isinstance(d, list):
        d = " ".join(str(x.get("msg", x)) if isinstance(x, dict) else str(x) for x in d)
    elif isinstance(d, dict):
        d = d.get("message") or d.get("msg") or d.get("detail") or json.dumps(d)[:200]
    return scrub(d or "")


class InsightsMiddleware:
    """Pure ASGI wrapper: passes every request through untouched and only looks at the response status (and,
    for an error, the first 2 KB of its body to read the message the visitor saw). Never raises its own errors."""

    def __init__(self, app, recorder):
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if not path.startswith("/api/") or SKIP_PATHS.search(path) or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)
        seen = {"status": 0, "body": b""}

        async def watching_send(message):
            try:
                if message["type"] == "http.response.start":
                    seen["status"] = int(message.get("status", 0))
                elif message["type"] == "http.response.body" and seen["status"] >= 400 and len(seen["body"]) < 2048:
                    seen["body"] += (message.get("body") or b"")[: 2048 - len(seen["body"])]
            except Exception:  # noqa: BLE001
                pass
            await send(message)

        try:
            await self.app(scope, receive, watching_send)
        except Exception:
            seen["status"] = seen["status"] or 500
            raise
        finally:
            try:
                self.recorder(scope, seen["status"] or 500, seen["body"])
            except Exception:  # noqa: BLE001 - watching must never break a request
                pass


def _headers(scope) -> dict:
    out = {}
    for k, v in scope.get("headers") or []:
        try:
            out[k.decode("latin-1").lower()] = v.decode("latin-1")
        except Exception:  # noqa: BLE001
            pass
    return out


def build(*, db, require_admin, client_ip, paid_counter=None):
    """-> (router, record_fn). paid_counter(start_day) -> number of completed payments since that day."""
    router = APIRouter()

    async def _insert(collection, doc):
        try:
            await getattr(db, collection).insert_one(doc)
        except Exception:  # noqa: BLE001
            pass

    def _spawn(coro):
        try:
            task = asyncio.get_running_loop().create_task(coro)
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
        except RuntimeError:
            pass

    def record(scope, status, body):
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        step = step_for(method, path, status)
        trouble = is_trouble(method, path, status)
        if not step and not trouble:
            return
        h = _headers(scope)
        ua = h.get("user-agent", "")[:300]
        if traffic.is_bot(ua):
            return
        fwd = h.get("x-forwarded-for", "")
        client = scope.get("client") or ("", 0)
        ip = (fwd.split(",")[0].strip() if fwd else "") or (client[0] if client else "") or "unknown"
        if not _allow(_hits, ip, MAX_RECORDS_PER_IP_PER_HOUR):
            return
        now = _now()
        day = traffic._today(now)
        base = {"day": day, "at": now.isoformat(), "visitor": traffic.visitor_id(ip, ua, day),
                "path": traffic._clean_path(path), "device": traffic.device(ua),
                "country": (h.get("cf-ipcountry") or "")[:2].upper()}
        if step:
            _spawn(_insert("site_steps", {**base, "step": step}))
        if trouble:
            _spawn(_insert("site_trouble", {**base, "kind": "api", "method": method, "status": status,
                                            "where": friendly_api(path), "message": _detail_from_body(body),
                                            "page": scrub(h.get("referer", ""), 200)}))

    @router.post("/trouble")
    async def page_trouble(request: Request):
        """Public beacon from /track.js: a page crashed, or the browser couldn't reach the server."""
        try:
            body = json.loads((await request.body()).decode("utf-8", "ignore") or "{}")
        except Exception:  # noqa: BLE001
            return {"ok": False}
        if not isinstance(body, dict):
            return {"ok": False}
        ua = request.headers.get("user-agent", "")[:300]
        if traffic.is_bot(ua, bool(body.get("wd"))):
            return {"ok": True}
        ip = client_ip(request)
        if not _allow(_page_hits, ip, MAX_PAGE_TROUBLE_PER_IP_PER_HOUR):
            return {"ok": False}
        now = _now()
        day = traffic._today(now)
        kind = re.sub(r"[^a-z_]", "", str(body.get("k") or "page_error").lower())[:20] or "page_error"
        await _insert("site_trouble", {
            "day": day, "at": now.isoformat(), "visitor": traffic.visitor_id(ip, ua, day), "kind": kind,
            "status": 0, "method": "", "where": {"offline": "Couldn't reach the server", "crash": "Page crashed"}.get(kind, "Page error"),
            "message": scrub(body.get("m")), "source": scrub(body.get("s"), 160),
            "path": traffic._clean_path(str(body.get("p") or "/")), "page": "",
            "device": traffic.device(ua), "country": (request.headers.get("cf-ipcountry") or "")[:2].upper(),
        })
        return {"ok": True}

    @router.get("/admin/insights")
    async def insights(days: int = 7, _: dict = Depends(require_admin)):
        days = max(1, min(int(days), MAX_DAYS))
        now = _now()
        day_list = [traffic._today(now - timedelta(days=i)) for i in range(days - 1, -1, -1)]
        views, steps, troubles = [], [], []
        for d in day_list:
            async for doc in db.pageviews.find({"day": d}):
                if not doc.get("bot"):
                    doc.pop("_id", None)
                    views.append(doc)
            async for doc in db.site_steps.find({"day": d}):
                doc.pop("_id", None)
                steps.append(doc)
            async for doc in db.site_trouble.find({"day": d}):
                doc.pop("_id", None)
                troubles.append(doc)
        paid = 0
        if paid_counter:
            try:
                paid = await paid_counter(day_list[0])
            except Exception:  # noqa: BLE001
                paid = 0
        return summarize(views, steps, troubles, paid=paid, now=now, day_list=day_list)

    return router, record


def _page_name(p: str) -> str:
    names = {"/": "Home page", "/audit": "Free check page", "/pricing": "Pricing", "/register": "Sign up page",
             "/login": "Log in page", "/cover-studio": "Cover Studio", "/interior-studio": "Interior Studio",
             "/dashboard": "My books", "/guides/": "Guides list", "/tools/": "Free tools list"}
    if p in names:
        return names[p]
    if p.startswith("/audit/:id"):
        return "Free check results"
    if p.startswith("/editor/"):
        return "Book editor"
    return p


def summarize(views, steps, troubles, *, paid=0, now=None, day_list=None):
    """Pure function (easy to test): turns raw page views, steps and trouble into the dashboard's numbers."""
    now = now or _now()
    journeys = {}

    def j(day, visitor):
        key = (day, visitor)
        if key not in journeys:
            journeys[key] = {"day": day, "visitor": visitor, "events": [], "steps": set(), "pages": [],
                             "source": "", "kind": "", "landing": "", "device": "", "country": "", "trouble": 0}
        return journeys[key]

    for v in sorted(views, key=lambda x: x.get("at", "")):
        jr = j(v["day"], v["visitor"])
        jr["device"] = jr["device"] or v.get("device", "")
        jr["country"] = jr["country"] or v.get("country", "")
        if v.get("event") == "pageview":
            if not jr["landing"] and v.get("source_kind") != "Internal":
                jr["landing"], jr["source"], jr["kind"] = v.get("path", "/"), v.get("source") or "", v.get("source_kind", "")
            jr["pages"].append(v.get("path", "/"))
            jr["steps"].add("visited")
            if (v.get("path") or "").startswith("/audit"):
                jr["steps"].add("opened_check")
            jr["events"].append({"at": v["at"], "type": "page", "text": _page_name(v.get("path", "/"))})
        else:
            label = v.get("label") or v.get("event")
            jr["events"].append({"at": v["at"], "type": "click", "text": f"Clicked: {label}"})
    for s in steps:
        jr = j(s["day"], s["visitor"])
        jr["steps"].update({"visited", s["step"]})
        jr["device"] = jr["device"] or s.get("device", "")
        jr["events"].append({"at": s["at"], "type": "step", "text": STEP_LABELS.get(s["step"], s["step"])})
    for t in troubles:
        jr = j(t["day"], t["visitor"])
        jr["steps"].add("visited")
        jr["trouble"] += 1
        msg = f"{t.get('where')}: {t.get('message')}" if t.get("message") else t.get("where", "Trouble")
        jr["events"].append({"at": t["at"], "type": "trouble", "text": f"Trouble — {msg}"})

    all_j = list(journeys.values())
    for jr in all_j:
        jr["events"].sort(key=lambda e: e["at"])
        if not jr["source"]:
            jr["source"], jr["kind"] = "Direct / typed in", jr["kind"] or "Direct"

    # Funnel: how many visitor-days really did each step. Counted one by one (not "a later step implies the
    # earlier ones"), because the site has two roads: free check -> results, and sign up -> book -> payment.
    order = [k for k, _ in FUNNEL]
    reached = Counter()
    for jr in all_j:
        for s in jr["steps"]:
            if s in order:
                reached[s] += 1
    reached["paid"] = max(reached["paid"], paid)
    funnel, biggest = [], None
    for i, (key, label) in enumerate(FUNNEL):
        n = reached[key]
        prev = reached[order[i - 1]] if i else n
        lost = max(0, prev - n) if i else 0
        row = {"key": key, "label": label, "count": n, "lost": lost,
               "kept_pct": round(100 * n / prev) if i and prev else (100 if n else 0)}
        funnel.append(row)
        if i and prev and (biggest is None or lost > biggest["lost"]):
            biggest = row

    # Where people leave: the last page of visits that never took a step.
    exits = Counter()
    for jr in all_j:
        if jr["pages"] and not (jr["steps"] & FURTHER_STEPS):
            exits[_page_name(jr["pages"][-1])] += 1

    def table(key_fn):
        rows = defaultdict(lambda: {"visitors": 0, "read_3": 0, "took_step": 0, "signed_up": 0, "checkout": 0})
        for jr in all_j:
            k = key_fn(jr)
            if not k:
                continue
            r = rows[k]
            r["visitors"] += 1
            r["read_3"] += len(set(jr["pages"])) >= 3
            r["took_step"] += bool(jr["steps"] & FURTHER_STEPS)
            r["signed_up"] += "signed_up" in jr["steps"]
            r["checkout"] += "opened_checkout" in jr["steps"]
        out = [{"name": k, **v, "step_pct": round(100 * v["took_step"] / v["visitors"]) if v["visitors"] else 0}
               for k, v in rows.items()]
        return sorted(out, key=lambda r: (-r["took_step"], -r["visitors"]))[:15]

    by_source = table(lambda jr: jr["source"])
    by_landing = table(lambda jr: _page_name(jr["landing"]) if jr["landing"] else "")

    groups = {}
    for t in troubles:
        key = (t.get("where", ""), t.get("status", 0), t.get("message", ""))
        g = groups.setdefault(key, {"where": key[0], "status": key[1], "message": key[2], "count": 0,
                                    "visitors": set(), "last": "", "pages": Counter()})
        g["count"] += 1
        g["visitors"].add((t["day"], t["visitor"]))
        g["last"] = max(g["last"], t.get("at", ""))
        g["pages"][_page_name(t.get("path", "")) if t.get("kind") != "api" else (t.get("page") or "")] += 1
    trouble = [{**g, "visitors": len(g["visitors"]), "pages": [p for p, _ in g["pages"].most_common(3) if p]}
               for g in groups.values()]
    trouble.sort(key=lambda g: g["last"], reverse=True)     # newest first ...
    trouble.sort(key=lambda g: g["visitors"], reverse=True)  # ... within "hurt the most people" first (stable sort)

    five = (now - timedelta(minutes=5)).isoformat()
    day_ago = (now - timedelta(hours=24)).isoformat()
    live = {jr["visitor"] for jr in all_j if jr["events"] and jr["events"][-1]["at"] >= five}

    recent = sorted(all_j, key=lambda jr: jr["events"][-1]["at"] if jr["events"] else "", reverse=True)
    journeys_out = []
    for jr in recent[:30]:
        if not jr["events"]:
            continue
        first, last = jr["events"][0]["at"], jr["events"][-1]["at"]
        try:
            secs = int((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds())
        except Exception:  # noqa: BLE001
            secs = 0
        furthest = next((label for key, label in reversed(FUNNEL) if key in jr["steps"]), "Visited the site")
        journeys_out.append({"at": last, "started": first, "seconds": secs, "source": jr["source"], "device": jr["device"],
                             "country": jr["country"], "furthest": furthest, "trouble": jr["trouble"],
                             "events": jr["events"][:25], "more": max(0, len(jr["events"]) - 25)})

    total = len(all_j)
    headlines = []
    if biggest and biggest["lost"]:
        headlines.append(f"Most people stop between \"{FUNNEL[order.index(biggest['key']) - 1][1]}\" and "
                         f"\"{biggest['label']}\": {biggest['lost']} of {biggest['lost'] + biggest['count']} didn't go on.")
    hurt = sum(1 for jr in all_j if jr["trouble"])
    if hurt:
        headlines.append(f"{hurt} visitor{'s' if hurt != 1 else ''} hit trouble (an error or a blocked step). See the trouble list.")
    best_src = next((r for r in by_source if r["took_step"]), None)
    if best_src:
        headlines.append(f"Best source for people who take a step: {best_src['name']} "
                         f"({best_src['took_step']} of {best_src['visitors']}).")
    if exits:
        page, n = exits.most_common(1)[0]
        headlines.append(f"Where people leave most without doing anything: {page} ({n}).")

    return {
        "site": SITE_NAME, "days": len(day_list or []), "from": (day_list or [""])[0], "to": (day_list or [""])[-1],
        "generated_at": now.isoformat(),
        "totals": {"visitors": total, "live_now": len(live),
                   "last_24h": sum(1 for jr in all_j if jr["events"] and jr["events"][-1]["at"] >= day_ago),
                   "took_a_step": sum(1 for jr in all_j if jr["steps"] & FURTHER_STEPS),
                   "hit_trouble": hurt, "trouble_events": len(troubles), "paid": reached["paid"]},
        "headlines": headlines,
        "funnel": funnel,
        "biggest_drop": biggest["key"] if biggest and biggest["lost"] else None,
        "exit_pages": exits.most_common(10),
        "by_source": by_source,
        "by_landing": by_landing,
        "trouble": trouble[:40],
        "journeys": journeys_out,
    }


def install_routes(api_router, *, db, require_admin, client_ip, paid_counter=None):
    """Adds the routes to api_router (call BEFORE app.include_router(api_router)) and returns the recorder that
    the watcher needs: app.add_middleware(InsightsMiddleware, recorder=record), added last so it sees every answer."""
    router, record = build(db=db, require_admin=require_admin, client_ip=client_ip, paid_counter=paid_counter)
    api_router.include_router(router)
    return record


async def ensure_indexes(db):
    for name in ("site_steps", "site_trouble"):
        try:
            await getattr(db, name).create_index("day")
        except Exception:  # noqa: BLE001
            pass
