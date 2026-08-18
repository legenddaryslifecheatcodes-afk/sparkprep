"""Standalone connectivity check for MONGO_URL. Prints success/fail only, never the credential."""
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

uri = os.environ.get("MONGO_URL")
if not uri:
    print("FAIL: MONGO_URL is not set in backend/.env")
    sys.exit(1)

try:
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    print("SUCCESS: connected to MongoDB Atlas")
except PyMongoError as e:
    print(f"FAIL: could not connect ({type(e).__name__})")
    sys.exit(1)
