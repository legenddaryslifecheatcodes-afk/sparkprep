"""RQ job bodies for the long-running project operations (autofix, export,
Advanced Interior Check run). These are enqueued by server.py's /enqueue
routes and executed by a separate `rq worker` process/service.

Each job reuses server.py's own async core functions (_autofix_core,
_export_project_core, _interior_check_run_core) rather than re-implementing
their logic here, so there is exactly one copy of that business logic to
keep correct.

RQ's default Worker forks a fresh child process per job. server.py's
module-level Mongo client is constructed once, in the long-lived parent
worker process, at import time -- before any of those forks -- so it must
never be used directly inside a job body (Motor's background connection
monitoring doesn't survive fork and will misbehave). Each job instead opens
its own Mongo client after the fork, inside its own asyncio.run() loop, and
patches it into server.db for the duration of the call.
"""
import asyncio
import os

import server


async def _open_db():
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URL") or "mongodb://localhost:27017"
    db_name = os.environ.get("DB_NAME") or "sparkprep"
    client = AsyncIOMotorClient(mongo_url)
    return client, client[db_name]


async def _run_with_user(project_id: str, user_id: str, core_call):
    client, db = await _open_db()
    server.db = db
    try:
        user = await db.users.find_one({"id": user_id})
        if not user:
            raise ValueError(f"User {user_id} not found")
        return await core_call(db, user)
    finally:
        client.close()


def run_autofix_job(project_id: str, user_id: str, slot: str = None):
    return asyncio.run(_run_with_user(
        project_id, user_id,
        lambda db, user: server._autofix_core(project_id, slot, user),
    ))


def run_export_job(project_id: str, user_id: str):
    return asyncio.run(_run_with_user(
        project_id, user_id,
        lambda db, user: server._export_project_core(project_id, user),
    ))


def run_interior_check_job(project_id: str, user_id: str):
    return asyncio.run(_run_with_user(
        project_id, user_id,
        lambda db, user: server._interior_check_run_core(project_id, user),
    ))
