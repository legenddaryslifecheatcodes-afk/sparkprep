"""Peak memory of the WHOLE cover loop (upload+scan, verified auto-fix, confirm, export) at a given size.
Usage: python backend/tools/profile_pipeline_memory.py WIDTH HEIGHT   (run each size in a fresh process)"""
import io, os, sys, tempfile, time, asyncio
from datetime import datetime, timezone
from pathlib import Path
os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_mem_"), USE_MEMORY_DB="1", JWT_SECRET="m" * 40)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent)); sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_cmyk_scale import peak_mb  # noqa: E402  (also runs its CLI body only when executed directly)
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
Image.MAX_IMAGE_PIXELS = None
server.ANTHROPIC_API_KEY = ""

if __name__ == "__main__":
    W, H = int(sys.argv[1]), int(sys.argv[2])
    img = Image.new("RGB", (W, H), (6, 4, 40)); d = ImageDraw.Draw(img)
    for i in range(0, W, 300): d.rectangle([i, 0, i + 150, H], fill=(0, 10, 60))
    d.text((55, 60), "LAST LIGHT", font=ImageFont.load_default(size=130), fill=(255, 255, 255))
    buf = io.BytesIO(); img.save(buf, "JPEG", dpi=(300, 300), quality=90); data = buf.getvalue(); del img, d
    asyncio.run(server.db.users.insert_one({"email": "m@example.com", "password_hash": server.hash_password("x"), "name": "M", "tier": "studio",
                                            "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
    base = peak_mb(); marks = {}
    with TestClient(server.app) as cl:
        cl.headers["Authorization"] = "Bearer " + cl.post("/api/auth/login", json={"email": "m@example.com", "password": "x"}).json()["token"]
        pid = cl.post("/api/projects", json={"name": "M", "platform": "kdp", "trim_size": "6x9", "paper_type": "white_50lb", "binding": "paperback", "page_count": 200, "project_type": "cover"}).json()["id"]
        t = time.time(); r = cl.post(f"/api/projects/{pid}/slot-upload/full_wrap", files={"file": ("c.jpg", data, "image/jpeg")}); marks["upload+scan"] = (round(time.time() - t, 1), round(peak_mb()), r.status_code)
        t = time.time(); r = cl.post(f"/api/projects/{pid}/autofix/verified", params={"slot": "full_wrap", "stream": "false"}); pl = r.json()["pipeline"]; st = pl["status"]; print("   pipeline timings:", pl["timings"], "| remaining:", [x["id"] for x in pl["remaining"]]); marks[f"verified pipeline ({st})"] = (round(time.time() - t, 1), round(peak_mb()), r.status_code)
        t = time.time(); r = cl.post(f"/api/projects/{pid}/autofix/confirm", params={"slot": "full_wrap"}); marks["confirm rescan"] = (round(time.time() - t, 1), round(peak_mb()), r.status_code)
        cl.patch(f"/api/projects/{pid}", json={"name": "M Book"})
        t = time.time(); r = cl.post(f"/api/projects/{pid}/export"); marks["export"] = (round(time.time() - t, 1), round(peak_mb()), r.status_code)
    print(f"{W}x{H} = {W*H/1e6:.1f} MP | memory before loop {base:.0f} MB")
    for k, (sec, pk, code) in marks.items():
        print(f"   {k:34s} {sec:6.1f}s   peak so far {pk:5d} MB   http {code}")
