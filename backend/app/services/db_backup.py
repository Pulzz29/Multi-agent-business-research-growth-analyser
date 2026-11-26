"""
PostgreSQL backups with retention and genuine restore verification.

backup():
1. Opens a REPEATABLE READ transaction, exports its snapshot and counts the
   rows of every table in that snapshot.
2. Runs ``pg_dump --format=custom --snapshot=<id>``, so the dump contains
   exactly the counted data.
3. Records the file, size and SHA-256 in ``backup_runs``.
4. Verification (default on): creates an isolated, uniquely named database,
   ``pg_restore``s the dump into it, compares every table's row count with
   the snapshot counts, then drops that database.
5. Retention: keeps the newest BACKUP_RETENTION_COUNT dumps and deletes dumps
   older than BACKUP_RETENTION_DAYS (only ``insightiq_*.dump`` files in the
   backup folder; never anything else).

Credentials reach pg_dump/pg_restore only through the PGPASSWORD environment
variable of the child process; they are never logged or written.
Schedule it with Windows Task Scheduler (scripts/register_backup_task.ps1)
or the Kubernetes CronJob in deploy/aks/backup-cronjob.yaml.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.database import db

logger = logging.getLogger("insightiq.backup")
FILE_PATTERN = re.compile(r"^insightiq_\d{8}T\d{6}Z\.dump$")


class BackupError(RuntimeError):
    pass


def settings():
    from app.core.config import get_settings

    s = get_settings()
    return {
        "directory": Path(os.getenv("BACKUP_DIR") or (Path(s.project_root) / "data" / "pg_backups")).resolve(),
        "retention_count": int(os.getenv("BACKUP_RETENTION_COUNT", "14")),
        "retention_days": float(os.getenv("BACKUP_RETENTION_DAYS", "30")),
        "pg_bin_dir": os.getenv("PG_BIN_DIR", "").strip(),
    }


def find_tool(name):
    """pg_dump / pg_restore from PG_BIN_DIR, PATH, or a standard install folder."""
    configured = settings()["pg_bin_dir"]
    candidates = [Path(configured) / f"{name}.exe", Path(configured) / name] if configured else []
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    for base in sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin"), reverse=True):
        candidates.append(base / f"{name}.exe")
    for path in candidates:
        if path.is_file():
            return str(path)
    raise BackupError(f"{name} was not found. Install the PostgreSQL client tools or set PG_BIN_DIR.")


def _connection_env_and_args(dbname=None):
    """Environment (password) and libpq arguments for the client tools."""
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(db.postgres_conninfo(dbname=dbname))
    env = dict(os.environ)
    if info.get("password"):
        env["PGPASSWORD"] = info["password"]
    for key, var in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT"),
                     ("connect_timeout", "PGCONNECT_TIMEOUT")):
        if info.get(key):
            env[var] = str(info[key])
    args = ["-h", str(info.get("host") or "localhost"), "-p", str(info.get("port") or 5432),
            "-U", str(info.get("user") or "postgres")]
    return env, args, info.get("dbname")


def _run(cmd, env, timeout=3600):
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        # stderr of pg tools never contains the password (it is not on the command line)
        raise BackupError(f"{Path(cmd[0]).stem} failed: {result.stderr.strip()[-400:]}")
    return result


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_counts(conn, schemas):
    counts = {}
    rows = conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_schema = ANY(?) ORDER BY 1, 2",
        (list(schemas),),
    ).fetchall()
    for schema, table in rows:
        counts[f"{schema}.{table}"] = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()[0]
    return counts


def _user_schemas(conn):
    return [r[0] for r in conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
        "AND nspname <> 'information_schema' ORDER BY 1").fetchall()]


def backup(verify=True, now=None):
    """Take one backup (and verify it). Returns the result dict."""
    cfg = settings()
    cfg["directory"].mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    name = f"insightiq_{now.strftime('%Y%m%dT%H%M%SZ')}.dump"
    target = cfg["directory"] / name
    env, args, dbname = _connection_env_and_args()
    pg_dump = find_tool("pg_dump")

    log = db.direct_connect()  # backup_runs lives in the application schema
    run_id = log.execute(
        "INSERT INTO backup_runs(started_at, file_name, status) VALUES (?, ?, 'running') RETURNING id",
        (now, name),
    ).fetchone()[0]
    log.commit()
    snap = db.direct_connect(schema="public")
    try:
        snap.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot_id = snap.execute("SELECT pg_export_snapshot()").fetchone()[0]
        schemas = _user_schemas(snap)
        expected = _table_counts(snap, schemas)
        _run([pg_dump, *args, "-d", dbname, "--format=custom", "--no-owner",
              f"--snapshot={snapshot_id}", "-f", str(target)], env)
        snap.rollback()
        digest, size = _sha256(target), target.stat().st_size
        result = {"file": name, "path": str(target), "size_bytes": size, "sha256": digest,
                  "tables": len(expected), "rows": sum(expected.values()), "restore_verified": False}
        if verify:
            result["verification"] = verify_restore(target, expected, env, args)
            result["restore_verified"] = result["verification"]["matched"]
        log.execute(
            "UPDATE backup_runs SET finished_at = ?, size_bytes = ?, sha256 = ?, status = 'succeeded', "
            "restore_verified = (? = 1), verification_json = ? WHERE id = ?",
            (datetime.now(timezone.utc), size, digest, bool(result["restore_verified"]),
             json.dumps(result.get("verification") or {}), run_id),
        )
        log.commit()
        result["retention"] = apply_retention()
        logger.info("db_backup ok file=%s size=%s verified=%s", name, size, result["restore_verified"])
        return result
    except Exception as exc:
        snap.rollback()
        log.rollback()
        log.execute("UPDATE backup_runs SET finished_at = ?, status = 'failed', error = ? WHERE id = ?",
                    (datetime.now(timezone.utc), str(exc)[:400], run_id))
        log.commit()
        target.unlink(missing_ok=True)
        raise
    finally:
        snap.close()
        log.close()


def verify_restore(dump_path, expected_counts, env=None, args=None):
    """Restore into a new, isolated database and compare row counts; the
    database is dropped afterwards. Returns {"matched", "differences", ...}."""
    from psycopg import sql

    if env is None:
        env, args, _ = _connection_env_and_args()
    scratch = f"insightiq_restore_verify_{uuid.uuid4().hex[:10]}"
    admin = db.direct_connect(dbname="postgres", schema="public", autocommit=True)
    try:
        admin.raw.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
        try:
            _run([find_tool("pg_restore"), *args, "-d", scratch, "--no-owner", "--exit-on-error",
                  str(dump_path)], env)
            restored = db.direct_connect(dbname=scratch, schema="public")
            try:
                actual = _table_counts(restored, sorted({k.split(".")[0] for k in expected_counts}))
            finally:
                restored.close()
        finally:
            admin.raw.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch)))
    finally:
        admin.close()
    differences = {t: {"expected": expected_counts.get(t), "restored": actual.get(t)}
                   for t in sorted(set(expected_counts) | set(actual))
                   if expected_counts.get(t) != actual.get(t)}
    return {"matched": not differences, "tables_compared": len(expected_counts),
            "rows_compared": sum(expected_counts.values()), "differences": differences,
            "scratch_database": scratch, "scratch_dropped": True}


def apply_retention(now=None):
    """Delete old dumps: keep the newest N, and nothing older than D days."""
    cfg = settings()
    now = now or datetime.now(timezone.utc)
    files = sorted((p for p in cfg["directory"].glob("insightiq_*.dump") if FILE_PATTERN.match(p.name)),
                   key=lambda p: p.name, reverse=True)
    cutoff = now - timedelta(days=cfg["retention_days"])
    protected = _newest_verified_file(files)
    deleted = []
    for index, path in enumerate(files):
        stamp = datetime.strptime(path.name[10:26], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if index >= cfg["retention_count"] or stamp < cutoff:
            if index == 0 or path.name == protected:
                continue  # never delete the newest backup or the newest restore-verified one
            path.unlink(missing_ok=True)
            deleted.append(path.name)
    return {"kept": len(files) - len(deleted), "deleted": deleted, "protected_verified": protected}


EXPORT_PATTERN = re.compile(r"^insightiq_export_\d{8}T\d{6}Z\.sqlite3$")


def apply_export_retention():
    """Keep the newest BACKUP_EXPORT_RETENTION_COUNT (default 3, minimum 1) SQLite exports."""
    keep = max(1, int(os.getenv("BACKUP_EXPORT_RETENTION_COUNT", "3")))
    files = sorted((p for p in settings()["directory"].glob("insightiq_export_*.sqlite3")
                    if EXPORT_PATTERN.match(p.name)), key=lambda p: p.name, reverse=True)
    deleted = []
    for path in files[keep:]:
        path.unlink(missing_ok=True)
        deleted.append(path.name)
    return {"kept": len(files) - len(deleted), "deleted": deleted}


def _newest_verified_file(files):
    """Name of the newest dump on disk whose restore was verified (backup_runs)."""
    names = {p.name for p in files}
    try:
        with db.connect() as conn:
            if not db.table_exists(conn, "backup_runs"):
                return None
            rows = conn.execute(
                "SELECT file_name FROM backup_runs WHERE status = 'succeeded' AND restore_verified "
                "ORDER BY id DESC").fetchall()
    except Exception:
        return None
    return next((r[0] for r in rows if r[0] in names), None)


def verify_files():
    """Re-hash every retained dump and compare with the SHA-256 recorded when it
    was taken; also checks that pg_restore can read its table of contents."""
    cfg = settings()
    env, args, _ = _connection_env_and_args()
    with db.connect() as conn:
        recorded = {r[0]: r[1] for r in conn.execute(
            "SELECT file_name, sha256 FROM backup_runs WHERE status = 'succeeded'").fetchall()}
    results = []
    for path in sorted(cfg["directory"].glob("insightiq_*.dump")):
        if not FILE_PATTERN.match(path.name):
            continue
        expected = recorded.get(path.name)
        readable = True
        try:
            _run([find_tool("pg_restore"), "--list", str(path)], env, timeout=600)
        except BackupError:
            readable = False
        results.append({"file": path.name, "recorded": expected is not None,
                        "sha256_matches": (expected == _sha256(path)) if expected else None,
                        "readable": readable})
    return results


# ------------------------------------------------------------ SQLite export

def export_sqlite(target, schema_source=None):
    """Consistent, offline SQLite copy of the PostgreSQL data: an additional
    recovery option, not a substitute for pg_dump backups.

    All tables are read in one REPEATABLE READ snapshot. The 21 original
    tables use the schema of the original SQLite database (``schema_source``,
    default data/insightiq.sqlite3), so the export is usable by the SQLite
    rollback backend; tables added on PostgreSQL get INTEGER/REAL/TEXT columns.
    Verified before the file is kept: PRAGMA integrity_check and
    foreign_key_check, per-table row counts, and the per-row SHA-256
    comparison used by the migration (sqlite_to_postgres.validate) in the
    same snapshot."""
    import sqlite3

    from app.core.config import get_settings
    from app.database.sqlite_to_postgres import TABLE_ORDER, validate

    target = Path(target).resolve()
    if target.exists():
        raise BackupError(f"Refusing to overwrite an existing file: {target.name}")
    schema_source = Path(schema_source or (Path(get_settings().project_root) / "data" / "insightiq.sqlite3"))
    temporary = target.with_name(target.name + ".partial")
    temporary.unlink(missing_ok=True)
    src = sqlite3.connect(schema_source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        ddl = dict(src.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall())
        indexes = [r[0] for r in src.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL").fetchall()]
    finally:
        src.close()

    affinity = {"bigint": "INTEGER", "integer": "INTEGER", "boolean": "INTEGER", "double precision": "REAL"}
    pg = db.direct_connect()
    out = sqlite3.connect(str(temporary))
    try:
        pg.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        tables = [t for t in db.table_names(pg) if t != "db_schema_migrations"]
        ordered = [t for t in TABLE_ORDER if t in tables] + sorted(t for t in tables if t not in TABLE_ORDER)
        counts = {}
        for table in ordered:
            cols = pg.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ? ORDER BY ordinal_position",
                (table,)).fetchall()
            names = [c[0] for c in cols]
            if table in ddl:
                out.execute(ddl[table])
            else:
                out.execute(f'CREATE TABLE "{table}" (' + ", ".join(
                    f'"{n}" {affinity.get(kind, "TEXT")}' for n, kind in cols) + ")")
            quoted = ", ".join(f'"{n}"' for n in names)
            rows = pg.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
            out.executemany(
                f'INSERT INTO "{table}" ({quoted}) VALUES ({", ".join("?" for _ in names)})',
                [tuple(int(v) if isinstance(v, bool) else (v.isoformat() if hasattr(v, "isoformat") else v)
                       for v in row) for row in rows])
            counts[table] = len(rows)
        for sql in indexes:
            out.execute(sql)
        out.commit()
        integrity = out.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = len(out.execute("PRAGMA foreign_key_check").fetchall())
        exported = {t: out.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in ordered}
        out.close()
        checksums = validate(temporary, pg, tables=[t for t in TABLE_ORDER if t in ordered])
        pg.rollback()
    except Exception:
        out.close()
        pg.rollback()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        pg.close()
    mismatched = [t for t, r in checksums.items() if not r["match"]]
    if integrity != "ok" or fk_violations or exported != counts or mismatched:
        temporary.unlink(missing_ok=True)
        raise BackupError(f"SQLite export failed verification (integrity={integrity}, "
                          f"fk={fk_violations}, checksum mismatches={mismatched}); no file was kept.")
    temporary.replace(target)
    return {"file": str(target), "tables": len(counts), "rows": sum(counts.values()), "integrity": integrity,
            "foreign_key_violations": fk_violations, "counts_match": True, "checksum_tables": len(checksums),
            "checksums_match": True, "sha256": _sha256(target)}


def latest_backup_status():
    with db.connect() as conn:
        if not db.table_exists(conn, "backup_runs"):
            return None
        row = conn.execute(
            "SELECT started_at, finished_at, file_name, size_bytes, status, restore_verified "
            "FROM backup_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
