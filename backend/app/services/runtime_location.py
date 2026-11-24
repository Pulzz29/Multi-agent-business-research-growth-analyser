"""
Keep the runtime SQLite location consistent across Windows sessions.

The working database lives under the per-session temp folder
(%TEMP%\\insightiq_sqlite\\<project>). On Windows Server / Remote Desktop each
login session gets a different temp folder (Temp\\2, Temp\\4, ...), and a
session's folder can be removed at sign-out. The startup configuration then
points at an old path while the application uses the new one.

``align_runtime_location`` runs before startup recovery and:

1. moves aside (never deletes) an empty, table-less database file at the
   current path, so normal restoration can run;
2. if the configured path differs from the current session path, compares
   every intact candidate - the database already at the current path, the
   configured database and the latest verified backup - by its latest
   recorded activity (research runs, news retrievals, source refreshes,
   market refreshes) and uses the newest. A stale database already at the
   current path is moved aside (never deleted) before seeding, so a session
   folder reused after sign-out can no longer hide newer data;
3. records the current path in data/server_startup_config.json and creates a
   verified backup for it, so the startup source-identity check matches.

It never overwrites an existing database that contains tables.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class RuntimeLocationError(RuntimeError):
    pass


def _tables(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        con.close()


def _integrity_ok(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def _usable(path):
    path = Path(path)
    if not path.is_file():
        return False
    try:
        return "research_execution_runs" in _tables(path) and _integrity_ok(path)
    except sqlite3.Error:
        return False


ACTIVITY_COLUMNS = (
    ("research_execution_runs", "updated_at_utc"),
    ("news_articles", "retrieved_at"),
    ("source_refreshes", "completed_at"),
    ("source_refreshes", "started_at"),
    ("market_companies", "last_refreshed_at"),
    ("market_quotes", "fetched_at"),
)


def latest_activity(path):
    """Newest activity timestamp recorded in a database (ISO text), or ''."""
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return ""
    try:
        tables = {r[0]: {c[1] for c in con.execute(f"PRAGMA table_info({r[0]})")}
                  for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        newest = ""
        for table, column in ACTIVITY_COLUMNS:
            if column in tables.get(table, set()):
                value = con.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
                if value and str(value) > newest:
                    newest = str(value)
        return newest
    except sqlite3.Error:
        return ""
    finally:
        con.close()


def _move_aside(path, label):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.{label}-{stamp}")
    os.replace(path, target)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            os.replace(side, target.with_name(target.name + suffix))
    return target


def _copy_database(source, target):
    """Consistent copy with SQLite's backup API (safe with WAL files)."""
    temporary = target.with_name(target.name + ".seed-" + uuid.uuid4().hex)
    src = sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(str(temporary))
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    if not _usable(temporary):
        temporary.unlink(missing_ok=True)
        raise RuntimeLocationError("The copied runtime database failed verification.")
    os.replace(temporary, target)


def _write_config(config_path, config):
    temporary = config_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, config_path)


def align_runtime_location(root, config_path, config):
    """Returns {"config": <possibly updated config>, "actions": [...]}."""
    from app.database.sqlite_manager import working_database_path

    root = Path(root).resolve()
    config_path = Path(config_path)
    current = working_database_path().resolve()
    configured = Path(config["runtime_database_path"]).resolve()
    actions = []

    # 1. A database file without InsightIQ's core tables (for example an empty
    #    file created by an early connection) blocks restoration: move it aside.
    if current.is_file():
        try:
            incomplete = not ({"research_execution_runs", "sec_facts"} & _tables(current))
        except sqlite3.Error:
            incomplete = False
        if incomplete:
            moved = _move_aside(current, "incomplete")
            actions.append(f"moved incomplete runtime database aside: {moved.name}")

    if configured == current:
        return {"config": config, "actions": actions}

    # 2. Session temp folder changed: use the newest intact candidate.
    from app.services.server_bootstrap import latest_backup_pair

    candidates = []
    if _usable(current):
        candidates.append(("current", current, latest_activity(current)))
    if _usable(configured):
        candidates.append(("configured", configured, latest_activity(configured)))
    backup = None
    try:
        backup = latest_backup_pair(root / "data" / "sqlite_backups")
        if _usable(backup[0]):
            candidates.append(("backup", backup[0], latest_activity(backup[0])))
    except Exception:
        backup = None

    # Newest activity wins; on a tie prefer current, then configured, then backup.
    order = {"current": 0, "configured": 1, "backup": 2}
    chosen = max(candidates, key=lambda c: (c[2], -order[c[0]]), default=None)
    actions.append("activity: " + ", ".join(f"{kind}={stamp or 'none'}" for kind, _, stamp in candidates))

    if chosen is None or chosen[0] == "current":
        if current.exists():
            actions.append("current session database is the newest (not modified)")
        elif backup is not None:
            from app.services.startup_recovery import prepare_runtime_database

            current.parent.mkdir(parents=True, exist_ok=True)
            prepare_runtime_database(current, backup[0], backup[1], reconcile_jobs=False)
            actions.append(f"restored runtime database from verified backup {backup[0].name}")
    else:
        if current.exists():
            moved = _move_aside(current, "superseded")
            actions.append(f"older database at the current path moved aside: {moved.name}")
        current.parent.mkdir(parents=True, exist_ok=True)
        if chosen[0] == "configured":
            _copy_database(configured, current)
            actions.append("copied runtime database from the previous session path")
        else:
            from app.services.startup_recovery import prepare_runtime_database

            prepare_runtime_database(current, backup[0], backup[1], reconcile_jobs=False)
            actions.append(f"restored runtime database from verified backup {backup[0].name}")

    # 3. Record the current path and a verified backup that matches it.
    updated = {**config, "runtime_database_path": str(current)}
    _write_config(config_path, updated)
    actions.append("startup configuration updated to the current session path")

    from app.services.sqlite_recovery import create_verified_backup

    _, manifest_path = create_verified_backup(root)
    actions.append(f"verified backup recorded for the current path: {manifest_path.name}")
    return {"config": updated, "actions": actions}
