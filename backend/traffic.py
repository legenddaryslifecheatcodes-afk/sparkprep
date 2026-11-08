"""SparkPrep's own traffic tracker (owner's ask, 2026-11-08): who comes through the site, real people vs bots,
where they came from, which pages they read, and how many signed up or claimed the free audit.

Privacy-friendly on purpose, so the site needs no cookie banner:
- no cookies and nothing stored in the visitor's browser;
- the raw IP address is never saved. A visitor is a one-way hash of (secret salt + day + IP + browser), so the
  same person counts once per day and can't be followed from one day to the next.

Every page on the site (the React app and the static guide/calculator pages) loads /track.js, which sends one
small beacon per page view to POST /api/pv. The owner reads the numbers at /admin/traffic (admin only).
"""
import hashlib
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request

SITE_HOSTS = ("sparkprep.legenddary.com", "sparkprep-live.pages.dev", "sparkprepfinal.pages.dev", "localhost")
MAX_HITS_PER_IP_PER_HOUR = 300   # a real reader never gets close; stops one script filling the database
MAX_DAYS = 90

BOT_RE = re.compile(
    r"bot\b|bot/|spider|crawl|slurp|bingpreview|mediapartners|headless|phantom|puppeteer|playwright|selenium|"
    r"lighthouse|pagespeed|gtmetrix|pingdom|uptime|monitor|python-|curl/|wget/|httpx|axios/|go-http|java/|okhttp|"
    r"scrapy|facebookexternalhit|embedly|preview|validator|checker|feedfetcher|google-inspectiontool|googleother|"
    r"chatgpt|gptbot|claude|anthropic|perplexity|bytespider|petalbot|yandex|baidu|semrush|ahrefs|mj12|dotbot",
    re.I)
BOT_NAMES = [("Googlebot", r"googlebot|google-inspectiontool|googleother|adsbot-google"), ("Bing", r"bingbot|bingpreview"),
             ("OpenAI / ChatGPT", r"gptbot|chatgpt|oai-searchbot"), ("Perplexity", r"perplexity"),
             ("Claude", r"claude|anthropic"), ("Meta / Facebook", r"facebookexternalhit|meta-externalagent"),
             ("Ahrefs / Semrush", r"ahrefs|semrush"), ("Headless browser / script", r"headless|puppeteer|playwright|selenium|python-|curl/|wget/")]

SOURCE_RULES = [  # (label, kind, host pattern)
    ("Google", "Search", r"(^|\.)google\."), ("Bing", "Search", r"(^|\.)bing\.com"), ("DuckDuckGo", "Search", r"duckduckgo"),
    ("Yahoo", "Search", r"yahoo\."), ("Brave Search", "Search", r"search\.brave"), ("Ecosia", "Search", r"ecosia"),
    ("ChatGPT", "AI", r"chatgpt\.com|openai\.com"), ("Perplexity", "AI", r"perplexity"), ("Claude", "AI", r"claude\.ai"),
    ("Gemini", "AI", r"gemini\.google"), ("Copilot", "AI", r"copilot\.microsoft"),
    ("TikTok", "Social", r"tiktok"), ("YouTube", "Social", r"youtube|youtu\.be"), ("Instagram", "Social", r"instagram"),
    ("X / Twitter", "Social", r"(^|\.)t\.co$|twitter|(^|\.)x\.com"), ("LinkedIn", "Social", r"linkedin|lnkd\.in"),
    ("Facebook", "Social", r"facebook|fb\.com|(^|\.)fb\.me"), ("Pinterest", "Social", r"pinterest|pin\.it"),
    ("Gmail / email", "Email", r"mail\.google|outlook|mail\.yahoo|mailmeteor"),
    ("Legenddary.com", "Own sites", r"legenddary\.com$"),
]

_hits = defaultdict(list)   # ip -> recent timestamps (in memory only; never saved)


def _salt() -> str:
    return os.environ.get("TRAFFIC_SALT") or os.environ.get("JWT_SECRET") or "sparkprep-traffic"


def _today(now=None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def visitor_id(ip: str, ua: str, day: str) -> str:
    return hashlib.sha256(f"{_salt()}|{day}|{ip}|{ua}".encode()).hexdigest()[:16]


def is_bot(ua: str, webdriver: bool = False) -> bool:
    return bool(webdriver) or not ua or bool(BOT_RE.search(ua))


def bot_name(ua: str) -> str:
    for name, pat in BOT_NAMES:
        if re.search(pat, ua or "", re.I):
            return name
    return "Other bot"


def device(ua: str) -> str:
    ua = (ua or "").lower()
    if "ipad" in ua or "tablet" in ua:
        return "Tablet"
    if "mobi" in ua or "iphone" in ua or "android" in ua:
        return "Phone"
    return "Computer"


def classify_source(referrer: str, utm_source: str = "") -> tuple:
    """-> (label, kind). utm_source wins (e.g. ?utm_source=tiktok on a bio link)."""
    if utm_source:
        u = utm_source.strip().lower()[:40]
        for label, kind, pat in SOURCE_RULES:
            if re.search(pat, u + ".com") or u == label.lower():
                return label, kind
        return u, "Campaign"
    host = (urlparse(referrer or "").hostname or "").lower()
    if not host:
        return "Direct / typed in", "Direct"
    if host in SITE_HOSTS or host.endswith(".sparkprep-live.pages.dev"):
        return "", "Internal"
    for label, kind, pat in SOURCE_RULES:
        if re.search(pat, host):
            return label, kind
    return host[4:] if host.startswith("www.") else host, "Other websites"


def _clean_path(p: str) -> str:
    p = (p or "/").split("#")[0].split("?")[0][:200] or "/"
    p = re.sub(r"/[0-9a-f]{8,}[0-9a-f-]*", "/:id", p)   # /audit/<uuid>/report -> /audit/:id/report
    return p if p.startswith("/") else "/" + p


def build_router(*, db, require_admin, client_ip) -> APIRouter:
    router = APIRouter()

    @router.post("/pv")
    async def page_view(request: Request):
        """Public beacon from /track.js. Always answers 204-style {"ok": true} so the page never notices."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - sendBeacon text/plain or junk
            return {"ok": False}
        if not isinstance(body, dict):
            return {"ok": False}
        ip = client_ip(request)
        now_s = time.time()
        recent = [t for t in _hits[ip] if t > now_s - 3600]
        if len(recent) >= MAX_HITS_PER_IP_PER_HOUR:
            _hits[ip] = recent
            return {"ok": False}
        recent.append(now_s)
        _hits[ip] = recent
        if len(_hits) > 5000:   # keep the in-memory limiter small
            for k in list(_hits)[:2500]:
                _hits.pop(k, None)

        ua = request.headers.get("user-agent", "")[:300]
        now = datetime.now(timezone.utc)
        day = _today(now)
        event = re.sub(r"[^a-z0-9_-]", "", str(body.get("e") or "pageview").lower())[:40] or "pageview"
        ref = str(body.get("r") or "")[:300]
        source, kind = classify_source(ref, str(body.get("us") or ""))
        bot = is_bot(ua, bool(body.get("wd")))
        host = (str(body.get("h") or "")[:80]).lower()
        path = _clean_path(str(body.get("p") or "/"))
        if host and host not in SITE_HOSTS and not host.endswith(".pages.dev"):
            path = host.split(".")[0] + ":" + path   # topic subdomain sites, e.g. sparkpreplandingfix:/
        doc = {
            "day": day, "at": now.isoformat(), "event": event, "path": path,
            "host": host, "source": source, "source_kind": kind,
            "ref_host": (urlparse(ref).hostname or "")[:80], "campaign": str(body.get("uc") or "")[:60],
            "visitor": visitor_id(ip, ua, day), "bot": bot, "bot_name": bot_name(ua) if bot else "",
            "device": device(ua), "country": (request.headers.get("cf-ipcountry") or "")[:2].upper(),
            "label": str(body.get("l") or "")[:80],
        }
        await db.pageviews.insert_one(doc)
        return {"ok": True}

    @router.get("/admin/traffic")
    async def traffic_stats(days: int = 7, _: dict = Depends(require_admin)):
        days = max(1, min(int(days), MAX_DAYS))
        now = datetime.now(timezone.utc)
        day_list = [_today(now - timedelta(days=i)) for i in range(days - 1, -1, -1)]
        docs = []
        for d in day_list:
            async for doc in db.pageviews.find({"day": d}):
                doc.pop("_id", None)
                docs.append(doc)
        return await summarize(db, docs, day_list, now)

    return router


async def summarize(db, docs, day_list, now):
    humans = [d for d in docs if not d.get("bot")]
    bots = [d for d in docs if d.get("bot")]
    views = [d for d in humans if d.get("event") == "pageview"]
    per_day = {d: {"day": d, "visitors": set(), "views": 0, "bots": 0} for d in day_list}
    for d in views:
        row = per_day.get(d["day"])
        if row:
            row["views"] += 1
            row["visitors"].add(d["visitor"])
    for d in bots:
        if d["day"] in per_day and d.get("event") == "pageview":
            per_day[d["day"]]["bots"] += 1
    series = [{"day": r["day"], "visitors": len(r["visitors"]), "views": r["views"], "bots": r["bots"]} for r in per_day.values()]

    # A "visit" = one visitor's first page that day; that's where their source is known.
    first = {}
    for d in sorted(views, key=lambda x: x["at"]):
        key = (d["day"], d["visitor"])
        if key not in first and d.get("source_kind") != "Internal":
            first[key] = d
    landing = Counter(d["path"] for d in first.values())
    sources = Counter(d["source"] for d in first.values())
    kinds = Counter(d["source_kind"] for d in first.values())
    five_min_ago = (now - timedelta(minutes=5)).isoformat()

    events = Counter((d["event"], d.get("label") or "") for d in humans if d.get("event") != "pageview")
    start = day_list[0]
    signups, free_audits, emails = 0, 0, Counter()
    async for u in db.users.find({}):
        if not u.get("is_guest") and str(u.get("created_at", ""))[:10] >= start:
            signups += 1
    async for c in db.free_audit_claims.find({}):
        if str(c.get("claimed_at", ""))[:10] >= start:
            free_audits += 1
    async for lead in db.leads.find({}):
        if str(lead.get("first_seen", ""))[:10] >= start:
            emails[(lead.get("sources") or ["unknown"])[0]] += 1

    recent = sorted(views, key=lambda x: x["at"], reverse=True)[:40]
    return {
        "days": len(day_list), "from": day_list[0], "to": day_list[-1],
        "totals": {
            "visitors": sum(s["visitors"] for s in series), "page_views": len(views), "bot_views": sum(s["bots"] for s in series),
            "live_now": len({d["visitor"] for d in views if d["at"] >= five_min_ago}),
            "signups": signups, "free_audits_claimed": free_audits, "emails_collected": sum(emails.values()),
        },
        "by_day": series,
        "top_pages": Counter(d["path"] for d in views).most_common(25),
        "landing_pages": landing.most_common(15),
        "sources": sources.most_common(20),
        "source_kinds": kinds.most_common(),
        "devices": Counter(d["device"] for d in first.values()).most_common(),
        "countries": Counter(d.get("country") or "?" for d in first.values()).most_common(10),
        "bots": Counter(d.get("bot_name") or "Other bot" for d in bots).most_common(10),
        "events": [{"event": e, "label": l, "count": n} for (e, l), n in events.most_common(20)],
        "emails_by_source": emails.most_common(),
        "recent": [{"at": d["at"], "path": d["path"], "source": d["source"] or "(clicked within site)",
                    "device": d["device"], "country": d.get("country", "")} for d in recent],
    }
