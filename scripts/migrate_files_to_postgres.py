"""
Move saved research reports (with summary/insight sidecars), research-job
journals and forecast caches into PostgreSQL (table stored_documents).

Idempotent: a document already in the database is never overwritten. After
the import every file is compared with its database copy (SHA-256). Files are
left on disk as the pre-migration archive; delete them only after you have a
verified PostgreSQL backup (scripts\\backup_postgres.py).

Usage: .venv\\Scripts\\python.exe scripts\\migrate_files_to_postgres.py [--env-file PATH]
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    from app.database.migrations import run_migrations
    from app.services import document_store

    run_migrations()
    counts = document_store.import_files(PROJECT_ROOT)
    print("Import:", counts)

    mismatched = []
    checked = 0
    paths = set()
    for prefix in document_store.MANAGED_PREFIXES:
        base = PROJECT_ROOT / prefix if prefix.endswith("/") else (PROJECT_ROOT / prefix).parent
        if base.is_dir():
            paths.update(base.rglob("*.json"))
    for path in sorted(paths):
        if True:
            key = document_store.key_for(path, PROJECT_ROOT)
            if not key.startswith(document_store.MANAGED_PREFIXES) or ".tmp" in path.name:
                continue
            checked += 1
            stored = document_store.read_text(path, root=PROJECT_ROOT)
            on_disk = path.read_text(encoding="utf-8")
            if stored is None:
                mismatched.append((key, "missing"))
            elif hashlib.sha256(stored.encode()).digest() != hashlib.sha256(on_disk.encode()).digest():
                mismatched.append((key, "database copy differs (newer database version kept)"))
    print(f"Verified {checked} files against the database.")
    for key, why in mismatched:
        print("  ", key, "-", why)
    missing = [k for k, why in mismatched if why == "missing"]
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
