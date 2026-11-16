"""Retention purge CLI.

Usage:
    python -m scripts.purge              # run the purge for real
    python -m scripts.purge --dry-run   # show what would be deleted without changing anything

This script uses the same RETENTION_DAYS / AUDIT_RETENTION_DAYS settings as the running app.
Run it from the backend/ directory with the venv activated and the same .env in scope.

A value of 0 for either setting means "keep forever" (no purge for that type).
"""
import argparse
import sys

from app.db.session import SessionLocal
from app.services.retention import purge_expired


def main() -> None:
    parser = argparse.ArgumentParser(description="Purge expired documents and audit rows")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting anything")
    args = parser.parse_args()

    with SessionLocal() as db:
        counts = purge_expired(db, dry_run=args.dry_run)

    label = "[DRY RUN] " if args.dry_run else ""
    print(f"{label}Documents deleted: {counts['documents']}")
    print(f"{label}Audit rows deleted: {counts['audit_rows']}")
    if not args.dry_run:
        print(f"Files deleted: {counts['files_deleted']}")
        if counts["files_failed"]:
            print(f"WARNING: {counts['files_failed']} file(s) could not be deleted (see server log)")
            sys.exit(1)


if __name__ == "__main__":
    main()
