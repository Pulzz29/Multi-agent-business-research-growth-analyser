"""One-time (idempotent) preparation of the local runtime after moving InsightIQ off Colab.
SQLITE-ONLY TOOL: works on the SQLite database file (DB_BACKEND=sqlite, rollback and
file recovery). With PostgreSQL use scripts/setup_postgres.py, pg_dump/pg_restore and
scripts/migrate_sqlite_to_postgres.py instead (docs/POSTGRES_MIGRATION.md).


What it does, using only the application's own functions:
1. Writes data/server_startup_config.json for this computer (the Colab version is kept
   as data/server_startup_config.colab.json).
2. If the runtime working database does not exist yet, seeds it from
   data/insightiq.sqlite3 with app.database.sqlite_manager.restore_database_from_drive()
   (the newest snapshot, which includes the later news refresh) and verifies row counts.
3. If the newest backup manifest was recorded for a different runtime path (Colab /tmp),
   creates a new verified backup + manifest with app.services.sqlite_recovery.create_verified_backup()
   so the server's source-identity check matches this computer.

Never deletes or overwrites research data. Makes no network calls.
Usage: .venv\\Scripts\\python.exe scripts\\prepare_local_runtime.py [--env-file PATH]
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def table_counts(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(tables)}
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    args = parser.parse_args()

    from run_backend import load_environment

    load_environment(Path(args.env_file).resolve())

    from app.core.config import get_settings
    from app.database import sqlite_manager

    root = Path(get_settings().project_root).resolve()
    assert root == PROJECT_ROOT, f"Project root mismatch: {root}"
    data = root / "data"
    runtime_db = sqlite_manager.working_database_path().resolve()

    # 1. Startup configuration
    config_path = data / "server_startup_config.json"
    colab_copy = data / "server_startup_config.colab.json"
    current = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    if current and not colab_copy.exists() and current.get("project_root") != str(root):
        shutil.copy2(config_path, colab_copy)
    new_config = {
        "version": 1,
        "project_root": str(root),
        "runtime_database_path": str(runtime_db),
        "server_mode": current.get("server_mode", "single_worker"),
        "startup_recovery_required": current.get("startup_recovery_required", True),
    }
    if current != new_config:
        tmp = config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(new_config, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, config_path)
        print("Startup configuration: written for this computer")
    else:
        print("Startup configuration: already local")

    # 2. Runtime working database
    persistent = sqlite_manager.drive_database_path().resolve()
    if runtime_db.exists():
        print("Runtime database: already present (not modified)")
    else:
        before = table_counts(persistent)
        result = sqlite_manager.restore_database_from_drive()
        assert result.get("restored"), result
        after = table_counts(runtime_db)
        assert after == before, ("Seeded counts differ", before, after)
        print("Runtime database: seeded from data/insightiq.sqlite3, counts verified")

    # 3. Recovery point recorded for this runtime path
    backups = data / "sqlite_backups"
    manifests = sorted(backups.glob("insightiq_*.manifest.json"), key=lambda p: p.name, reverse=True)
    newest = json.loads(manifests[0].read_text(encoding="utf-8")) if manifests else {}
    recorded = newest.get("source_database_path")
    if recorded and Path(recorded).resolve() == runtime_db:
        print("Latest backup manifest: already matches local runtime path")
    else:
        from app.services.sqlite_recovery import create_verified_backup

        backup_path, manifest_path = create_verified_backup(root)
        print("Latest backup manifest: created", manifest_path.name)

    print("Runtime counts:", json.dumps(table_counts(runtime_db)))


if __name__ == "__main__":
    main()
