"""Putting a SparkPrep backup (server._export_database / the admin page's "Download backup") back into a
database. Kept separate from server.py so it can run on its own -- see tools/restore_backup.py.

Safe by default: a collection that already has documents is left alone unless replace=True, so restoring
into the live database by mistake can't wipe current customers."""
import gzip

from bson import json_util

BACKUP_FORMAT = "sparkprep-backup-v1"


def read_backup(data: bytes) -> dict:
    payload = json_util.loads(gzip.decompress(data).decode("utf-8"))
    if payload.get("format") != BACKUP_FORMAT:
        raise ValueError(f"Not a SparkPrep backup (format {payload.get('format')!r})")
    return payload


async def restore_into(db, payload: dict, *, replace: bool = False) -> dict:
    """-> {collection: "restored N" | "skipped: already has N documents"}."""
    report = {}
    for name, docs in payload["collections"].items():
        coll = getattr(db, name)
        existing = await coll.count_documents({})
        if existing and not replace:
            report[name] = f"skipped: already has {existing} documents"
            continue
        if existing and replace:
            await coll.delete_many({})
        if docs:
            await coll.insert_many(docs)
        report[name] = f"restored {len(docs)}"
    return report
