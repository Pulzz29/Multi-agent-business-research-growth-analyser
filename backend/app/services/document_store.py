"""
Storage for InsightIQ's JSON documents: saved research reports, their
summary and insight sidecars, research-job journals and forecast caches.

PostgreSQL (DB_BACKEND=postgres): one table, ``stored_documents``, keyed by
the document's path relative to the project root (for example
``reports/research_runs/<run_id>.json``), so existing path-based code keeps
its identifiers. Each row stores the text and its SHA-256; every read checks
the hash. SQLite (rollback backend): the files on disk, as before.

``import_files`` copies existing files into the table (idempotent; existing
rows are never overwritten) so report history, journals and caches are
preserved. Files are not deleted by the import.
"""

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from app.database import db

# Folders whose JSON documents live in the database on PostgreSQL.
MANAGED_PREFIXES = (
    "reports/research_runs/",
    "reports/sector_comparisons/",
    "data/research_job_journal/",
    "data/forecast_cache_v",
    "data/chat_answer_cache/",
)


class DocumentIntegrityError(RuntimeError):
    pass


def project_root():
    from app.core.config import get_settings

    return Path(get_settings().project_root).resolve()


def key_for(path, root=None):
    """Path -> 'reports/research_runs/x.json' (relative, POSIX)."""
    root = Path(root or project_root()).resolve()
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    relative = path.resolve().relative_to(root)
    key = PurePosixPath(*relative.parts).as_posix()
    if ".." in key.split("/"):
        raise ValueError("Document path escapes the project root.")
    return key


def managed(path, root=None):
    """True when this path is stored in the database (PostgreSQL backend)."""
    if not db.is_postgres():
        return False
    try:
        key = key_for(path, root)
    except ValueError:
        return False
    return key.startswith(MANAGED_PREFIXES)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- reading

def read_text(path, root=None):
    """Document text, or None when it does not exist."""
    if not managed(path, root):
        path = Path(path)
        return path.read_text(encoding="utf-8") if path.is_file() else None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT content, sha256 FROM stored_documents WHERE path = ?", (key_for(path, root),),
        ).fetchone()
    if row is None:
        return None
    if _sha(row["content"]) != row["sha256"]:
        raise DocumentIntegrityError(f"Stored document failed its integrity check: {key_for(path, root)}")
    return row["content"]


def exists(path, root=None):
    if not managed(path, root):
        return Path(path).is_file()
    with db.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM stored_documents WHERE path = ?", (key_for(path, root),),
        ).fetchone() is not None


def list_paths(directory, suffix=".json", root=None):
    """Documents directly inside ``directory`` (not sub-folders), as Paths."""
    directory = Path(directory)
    if not managed(directory / "x.json", root):
        return sorted(directory.glob(f"*{suffix}")) if directory.is_dir() else []
    root = Path(root or project_root()).resolve()
    prefix = key_for(directory, root).rstrip("/") + "/"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT path FROM stored_documents WHERE path LIKE ? ORDER BY path",
            (prefix.replace("_", r"\_").replace("%", r"\%") + "%",),
        ).fetchall()
    out = []
    for (key,) in rows:
        rest = key[len(prefix):]
        if "/" not in rest and rest.endswith(suffix):
            out.append(root / key)
    return out


# ----------------------------------------------------------------- writing

def write_text(path, text, root=None):
    """Atomic create-or-replace of one document."""
    if not managed(path, root):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path
    now = datetime.now(timezone.utc)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO stored_documents(path, content, sha256, size_bytes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (path) DO UPDATE SET content = excluded.content, sha256 = excluded.sha256,
                size_bytes = excluded.size_bytes, updated_at = excluded.updated_at
            """,
            (key_for(path, root), text, _sha(text), len(text.encode("utf-8")), now, now),
        )
    return Path(path)


def delete(path, root=None):
    if not managed(path, root):
        Path(path).unlink(missing_ok=True)
        return
    with db.transaction() as conn:
        conn.execute("DELETE FROM stored_documents WHERE path = ?", (key_for(path, root),))


# --------------------------------------------------------------- migration

def import_files(root=None, prefixes=MANAGED_PREFIXES, overwrite=False):
    """Copy managed JSON files under ``root`` into stored_documents.

    Idempotent: an existing row is never overwritten (the database copy is
    authoritative once written). Returns counts."""
    if not db.is_postgres():
        raise RuntimeError("import_files needs DB_BACKEND=postgres.")
    root = Path(root or project_root()).resolve()
    counts = {"scanned": 0, "imported": 0, "already_present": 0, "different_in_database": 0}
    candidates = []
    for directory in sorted({(root / p).parent if not p.endswith("/") else root / p for p in prefixes}):
        if directory.is_dir():
            candidates.extend(directory.rglob("*.json"))
    now = datetime.now(timezone.utc)
    with db.transaction() as conn:
        for path in sorted(set(candidates)):
            key = key_for(path, root)
            if not key.startswith(prefixes) or ".tmp" in path.name:
                continue
            counts["scanned"] += 1
            text = path.read_text(encoding="utf-8")
            row = conn.execute("SELECT sha256 FROM stored_documents WHERE path = ?", (key,)).fetchone()
            if row is not None and not (overwrite and row[0] != _sha(text)):
                counts["already_present" if row[0] == _sha(text) else "different_in_database"] += 1
                continue
            if row is not None:  # overwrite=True (isolated test databases only)
                conn.execute("DELETE FROM stored_documents WHERE path = ?", (key,))
            conn.execute(
                "INSERT INTO stored_documents(path, content, sha256, size_bytes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (path) DO NOTHING",
                (key, text, _sha(text), len(text.encode("utf-8")), now, now),
            )
            counts["imported"] += 1
    return counts


def version(path, root=None):
    """Change marker for caching: sha256 (database) or (mtime, size) (file);
    None when the document does not exist."""
    if not managed(path, root):
        try:
            stat = Path(path).stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)
    with db.connect() as conn:
        row = conn.execute("SELECT sha256 FROM stored_documents WHERE path = ?", (key_for(path, root),)).fetchone()
    return row[0] if row else None


def write_once(path, text, root=None):
    """Create the document only if it does not exist yet (never overwritten).
    Returns True when this call created it."""
    if not managed(path, root):
        import threading

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(text, encoding="utf-8")
        try:
            os.link(temp, path)  # atomic create; fails if the file exists
            return True
        except FileExistsError:
            return False
        finally:
            temp.unlink(missing_ok=True)
    now = datetime.now(timezone.utc)
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO stored_documents(path, content, sha256, size_bytes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (path) DO NOTHING",
            (key_for(path, root), text, _sha(text), len(text.encode("utf-8")), now, now),
        )
        return cursor.rowcount == 1
