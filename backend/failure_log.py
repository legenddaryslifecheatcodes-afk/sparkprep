"""Persistent record of processing failures -- file analysis, autofix,
export, and template-detection crashes get written here with enough
context to diagnose later, instead of scrolling through raw Render stdout
logs to reconstruct what happened. Internal/ops tool only, never shown to
users.
"""
import traceback
from datetime import datetime, timezone


async def log_failure(db, stage: str, error: Exception, *, project_id: str = None,
                       user_id: str = None, context: dict = None) -> None:
    """Records a failure. Never raises -- a broken logging call must not
    take down the request that was already failing for its own reason."""
    try:
        await db.failure_log.insert_one({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "error_type": type(error).__name__,
            "message": str(error)[:2000],
            "traceback": traceback.format_exc()[-4000:],
            "project_id": project_id,
            "user_id": user_id,
            "context": context or {},
        })
    except Exception:
        pass
