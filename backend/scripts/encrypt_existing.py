"""Encrypt existing plaintext data at rest (gap 1).

Reads every Scan.extracted_text and PiiEntity.detected_value that is NOT already encrypted
and re-saves it through the EncryptedText column type (which calls encrypt_str).
Also re-encrypts every file in STORAGE_DIR that does not already start with the PIISENC1 magic.

Usage:
    python -m scripts.encrypt_existing            # encrypt all plaintext rows + files
    python -m scripts.encrypt_existing --rotate   # re-encrypt already-encrypted rows/files with the
                                                  # current first key (key rotation)
    python -m scripts.encrypt_existing --dry-run  # count what would be changed without touching anything

IMPORTANT: Back up the database and STORAGE_DIR before running this, especially with --rotate.
           LOSING THE KEY MEANS LOSING THE DATA.
"""
import argparse
import logging
import sys

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import PiiEntity, Scan
from app.security.crypto import (
    FILE_MAGIC,
    TEXT_PREFIX,
    decrypt_bytes,
    decrypt_str,
    encrypt_bytes,
    encrypt_str,
    enabled,
)
from app.storage.local import get_storage, StorageError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("encrypt_existing")

BATCH = 500  # rows per transaction


def _needs_text_encrypt(value: str | None, rotate: bool) -> bool:
    if value is None:
        return False
    if value.startswith(TEXT_PREFIX):
        return rotate  # already encrypted; only process if rotating
    return True  # plaintext


def _needs_file_encrypt(data: bytes, rotate: bool) -> bool:
    if data.startswith(FILE_MAGIC):
        return rotate  # already encrypted; only process if rotating
    return True  # plaintext


def encrypt_db(db: Session, *, rotate: bool, dry_run: bool) -> dict:
    counts = {"scans_updated": 0, "entities_updated": 0}

    # ── Scan.extracted_text ────────────────────────────────────────────────────
    offset = 0
    while True:
        rows = db.scalars(
            select(Scan).where(Scan.extracted_text.is_not(None)).offset(offset).limit(BATCH)
        ).all()
        if not rows:
            break
        for scan in rows:
            raw = scan.extracted_text
            if raw is None:
                continue
            if not _needs_text_encrypt(raw, rotate):
                continue
            if not dry_run:
                if raw.startswith(TEXT_PREFIX) and rotate:
                    raw = decrypt_str(raw)
                db.execute(
                    text("UPDATE scans SET extracted_text = :v WHERE id = :id"),
                    {"v": encrypt_str(raw), "id": scan.id},
                )
            counts["scans_updated"] += 1
        if not dry_run:
            db.commit()
        offset += BATCH
        logger.info("scans: processed %d so far (updated %d)", offset, counts["scans_updated"])

    # ── PiiEntity.detected_value ───────────────────────────────────────────────
    offset = 0
    while True:
        rows = db.scalars(select(PiiEntity).offset(offset).limit(BATCH)).all()
        if not rows:
            break
        for ent in rows:
            raw = ent.detected_value
            if not _needs_text_encrypt(raw, rotate):
                continue
            if not dry_run:
                if raw.startswith(TEXT_PREFIX) and rotate:
                    raw = decrypt_str(raw)
                db.execute(
                    text("UPDATE pii_entities SET detected_value = :v WHERE id = :id"),
                    {"v": encrypt_str(raw), "id": ent.id},
                )
            counts["entities_updated"] += 1
        if not dry_run:
            db.commit()
        offset += BATCH
        logger.info("entities: processed %d so far (updated %d)", offset, counts["entities_updated"])

    return counts


def encrypt_files(*, rotate: bool, dry_run: bool) -> dict:
    storage = get_storage()
    counts = {"files_updated": 0, "files_failed": 0}
    for key in storage.iter_keys():
        try:
            raw = storage.path_for(key).read_bytes()
            if not _needs_file_encrypt(raw, rotate):
                continue
            if not dry_run:
                if raw.startswith(FILE_MAGIC) and rotate:
                    raw = decrypt_bytes(raw)
                storage.save(key, raw)  # save() calls encrypt_bytes internally
            counts["files_updated"] += 1
        except (StorageError, Exception) as exc:
            logger.warning("failed to process file %s: %s", key, exc)
            counts["files_failed"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Encrypt existing plaintext data at rest")
    parser.add_argument("--rotate", action="store_true", help="Re-encrypt already-encrypted values with the current first key")
    parser.add_argument("--dry-run", action="store_true", help="Count what would change without making any changes")
    args = parser.parse_args()

    if not enabled():
        print("ERROR: encryption is not enabled (ENCRYPTION_KEYS is empty). Set a key first.")
        sys.exit(1)

    label = "[DRY-RUN] " if args.dry_run else ""
    logger.info("%sStarting encryption pass (rotate=%s)", label, args.rotate)

    with SessionLocal() as db:
        db_counts = encrypt_db(db, rotate=args.rotate, dry_run=args.dry_run)

    file_counts = encrypt_files(rotate=args.rotate, dry_run=args.dry_run)

    print(f"\n{label}Results:")
    print(f"  Scan texts updated:      {db_counts['scans_updated']}")
    print(f"  Entity values updated:   {db_counts['entities_updated']}")
    print(f"  Files updated:           {file_counts['files_updated']}")
    if file_counts["files_failed"]:
        print(f"  Files FAILED:            {file_counts['files_failed']}  (see log above)")
        sys.exit(1)


if __name__ == "__main__":
    main()
