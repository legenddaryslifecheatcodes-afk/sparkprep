"""Run the real SparkPrep API locally (in-memory DB, throwaway upload folder) for load/memory testing.
Usage: python backend/tools/serve_local.py PORT DATA_DIR"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

port, data_dir = int(sys.argv[1]), sys.argv[2]
os.makedirs(data_dir, exist_ok=True)
os.environ.update(DATA_DIR=data_dir, USE_MEMORY_DB="1", JWT_SECRET="serve-local-" + "x" * 32)
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

server.ANTHROPIC_API_KEY = ""
asyncio.run(server.db.users.insert_one({
    "email": "load@example.com", "password_hash": server.hash_password("TestPass123!"), "name": "Load", "tier": "studio",
    "created_at": datetime.now(timezone.utc).isoformat(), "exports_this_month": 0, "books_this_month": 0}))
import uvicorn  # noqa: E402

uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")
