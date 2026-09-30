"""Restore a SparkPrep backup file into a MongoDB database.

    python tools/restore_backup.py sparkprep-backup-2026-09-30.json.gz            (look only: shows what's inside)
    python tools/restore_backup.py sparkprep-backup-2026-09-30.json.gz --restore  (restore into empty collections)

The database comes from the MONGO_URL environment variable (and DB_NAME, default "sparkprep") -- set it in
your own terminal, never paste it into a chat. Collections that already have data are skipped unless you also
pass --replace, so running this against the live database by mistake can't wipe current customers."""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backup_restore import read_backup, restore_into  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backup_file")
    ap.add_argument("--restore", action="store_true", help="actually write to the database")
    ap.add_argument("--replace", action="store_true", help="overwrite collections that already have data")
    args = ap.parse_args()

    payload = read_backup(Path(args.backup_file).read_bytes())
    print(f"Backup taken {payload['exported_at']}")
    for name, n in sorted(payload["counts"].items()):
        print(f"  {name}: {n} documents")
    if not args.restore:
        print("\nNothing written (add --restore to put this backup into the database).")
        return

    url = os.environ.get("MONGO_URL")
    if not url:
        sys.exit("Set MONGO_URL in this terminal first (never paste it into a chat).")
    from motor.motor_asyncio import AsyncIOMotorClient
    db = AsyncIOMotorClient(url)[os.environ.get("DB_NAME") or "sparkprep"]
    report = asyncio.run(restore_into(db, payload, replace=args.replace))
    for name, result in sorted(report.items()):
        print(f"  {name}: {result}")


if __name__ == "__main__":
    main()
