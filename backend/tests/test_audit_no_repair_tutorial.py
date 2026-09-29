"""Owner's rule: the $1.99 audit says what failed, where, why, and the publisher requirement -- it never
hands out a repair tutorial (fix steps, tool lists, fix times). Doing the work is SparkPrep's main service,
so the tutorial must not exist anywhere the customer can reach: page data, network response, or PDF."""
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ.update(DATA_DIR=tempfile.mkdtemp(prefix="sp_ant_"), USE_MEMORY_DB="1", JWT_SECRET="ant-test-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

REPAIR_KEYS = ("fix_steps", "fix_tools", "est_fix_minutes", "estimated_fix_minutes")


def test_paid_audit_shows_what_where_and_rule_but_no_repair_tutorial():
    with TestClient(server.app) as c:
        aid = c.post("/api/audit/start", json={"platform": "ingramspark", "trim_size": "6x9", "file_type": "interior"}).json()["audit_id"]
        buf = io.BytesIO()
        Image.new("RGB", (900, 1350), (200, 40, 40)).save(buf, "JPEG")          # RGB + low resolution: real findings
        up = c.post(f"/api/audit/{aid}/upload", files={"file": ("page.jpg", buf.getvalue(), "image/jpeg")})
        assert up.status_code == 200, up.text
        assert not any(k in up.json()["summary"] for k in REPAIR_KEYS)

        stored = asyncio.run(server.db.audits.find_one({"audit_id": aid}))
        assert any(f.get("fix_steps") for f in stored["full_findings"])          # the engine still knows how...
        asyncio.run(server.db.audits.update_one({"audit_id": aid}, {"$set": {"paid": True}}))

        report = c.get(f"/api/audit/{aid}").json()
        assert report["full_report"], "paid audit should show its findings"
        for f in report["full_report"]:                                         # ...but the customer never gets it
            assert not any(k in f for k in REPAIR_KEYS), f
            assert f["title"] and f["why_it_fails"] and f["publisher_rule"]
        assert not any(k in report["summary"] for k in REPAIR_KEYS)

        pdf = c.get(f"/api/audit/{aid}/report")
        assert pdf.status_code == 200
        import pymupdf
        text = " ".join(p.get_text() for p in pymupdf.open(stream=pdf.content, filetype="pdf"))
        assert "Publisher requirement" in text and "can fix these issues" in text
        for step in stored["full_findings"][0]["fix_steps"]:
            assert step[:40] not in text
        assert "fix time" not in text.lower()
