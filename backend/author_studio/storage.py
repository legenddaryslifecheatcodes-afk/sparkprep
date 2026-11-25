"""Where Author Studio files are kept.

Two places, chosen by environment variables (trey picked "store files on the site", 2026-11-25):
- Cloudflare R2 when STUDIO_R2_ACCOUNT_ID, STUDIO_R2_ACCESS_KEY_ID, STUDIO_R2_SECRET_ACCESS_KEY and
  STUDIO_R2_BUCKET are all set (about $0.015 per GB a month, first 10 GB free, no download fees).
- Otherwise the server's own disk under DATA_DIR/studio (the 10 GB disk the server already pays for).

Every file version is its own object and is never overwritten, which is what makes version history safe.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


class LocalStorage:
    kind = "local"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("bad key")
        return p

    def put_file(self, key: str, src: Path, content_type: str) -> None:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

    def local_path(self, key: str) -> Optional[Path]:
        p = self._path(key)
        return p if p.exists() else None

    def download_url(self, key: str, filename: str) -> Optional[str]:
        return None                       # served by the API itself

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except (OSError, ValueError):
            pass

    def has_room(self, size_bytes: int, min_free_mb: int) -> bool:
        try:
            free = shutil.disk_usage(self.root).free
        except OSError:
            return True
        return free - size_bytes > min_free_mb * 1024 * 1024


class R2Storage:
    kind = "r2"

    def __init__(self, account_id: str, key_id: str, secret: str, bucket: str):
        import boto3                       # already in requirements.txt
        from botocore.config import Config
        self.bucket = bucket
        self.s3 = boto3.client("s3", endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                               aws_access_key_id=key_id, aws_secret_access_key=secret, region_name="auto",
                               config=Config(signature_version="s3v4", retries={"max_attempts": 3}))

    def put_file(self, key: str, src: Path, content_type: str) -> None:
        self.s3.upload_file(str(src), self.bucket, key, ExtraArgs={"ContentType": content_type})
        try:
            Path(src).unlink()
        except OSError:
            pass

    def local_path(self, key: str) -> Optional[Path]:
        return None

    def download_url(self, key: str, filename: str) -> Optional[str]:
        safe = filename.replace('"', "")
        return self.s3.generate_presigned_url("get_object", ExpiresIn=600, Params={
            "Bucket": self.bucket, "Key": key, "ResponseContentDisposition": f'attachment; filename="{safe}"'})

    def delete(self, key: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001
            pass

    def has_room(self, size_bytes: int, min_free_mb: int) -> bool:
        return True


def make_storage(data_dir: Path):
    env = os.environ
    names = ("STUDIO_R2_ACCOUNT_ID", "STUDIO_R2_ACCESS_KEY_ID", "STUDIO_R2_SECRET_ACCESS_KEY", "STUDIO_R2_BUCKET")
    if all(env.get(n) for n in names):
        return R2Storage(*(env[n] for n in names))
    return LocalStorage(Path(data_dir) / "studio")
