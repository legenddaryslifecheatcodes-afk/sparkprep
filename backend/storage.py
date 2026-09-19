"""Shared object storage abstraction.

The web service and the RQ worker run as separate Render instances with
separate local disks, so a file written to UPLOAD_DIR by one process is not
visible to the other. This module makes Cloudflare R2 (S3-compatible) the
shared backing store for everything under UPLOAD_DIR, while keeping the rest
of the codebase working with plain local Path objects:

- Every write to UPLOAD_DIR is followed by upload_file(path) so R2 always
  has a copy.
- Every read of a path that might have been written by the *other* process
  calls ensure_local(path) first, which downloads from R2 on a local miss.

When R2 credentials aren't configured (e.g. local dev), both functions are
no-ops and everything behaves exactly as it did before this module existed
(single-process, local-disk-only).

Callers pass the local UPLOAD_DIR Path; the R2 object key is always just
that path's filename, mirroring the existing flat layout under UPLOAD_DIR.
"""
import os
from pathlib import Path

_R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
_R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
_R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
_R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")

REMOTE_ENABLED = bool(
    _R2_ACCOUNT_ID and _R2_ACCESS_KEY_ID and _R2_SECRET_ACCESS_KEY and _R2_BUCKET_NAME
)

_client = None
if REMOTE_ENABLED:
    import boto3
    from botocore.config import Config

    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{_R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=_R2_ACCESS_KEY_ID,
        aws_secret_access_key=_R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        region_name="auto",
    )


def is_remote_enabled() -> bool:
    return REMOTE_ENABLED


def upload_file(local_path) -> None:
    """Push a local file up to R2 under its own filename as the key.

    Call this immediately after writing/rewriting any file under
    UPLOAD_DIR, so the other process (web <-> worker) can fetch it via
    ensure_local(). No-op when R2 isn't configured or the file is missing.
    """
    if not REMOTE_ENABLED:
        return
    local_path = Path(local_path)
    if not local_path.is_file():
        return
    _client.upload_file(str(local_path), _R2_BUCKET_NAME, local_path.name)


def ensure_local(local_path) -> Path:
    """Guarantee local_path exists on this process's disk, downloading it
    from R2 first if it's missing locally (i.e. it was written by the other
    process). Returns local_path unchanged either way. No-op download when
    R2 isn't configured -- the caller gets back whatever was already there.
    """
    local_path = Path(local_path)
    if not REMOTE_ENABLED or local_path.is_file():
        return local_path
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _client.download_file(_R2_BUCKET_NAME, local_path.name, str(local_path))
    except Exception:
        pass
    return local_path


def delete_file(local_path) -> None:
    """Remove a file both locally and from R2 (best-effort on the R2 side)."""
    local_path = Path(local_path)
    if local_path.is_file():
        os.remove(local_path)
    if REMOTE_ENABLED:
        try:
            _client.delete_object(Bucket=_R2_BUCKET_NAME, Key=local_path.name)
        except Exception:
            pass
