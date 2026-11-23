"""Local (Windows/macOS/Linux) compatibility helpers added during the Colab -> VS Code migration.

These helpers change *how* resources are handled on the host OS, never *what* the
application computes:

* closing_connection  - same commit/rollback semantics as ``with sqlite3.connect(...)``
                        but also closes the connection. Windows refuses to replace or
                        delete a SQLite file that still has an open handle.
* exclusive_file_lock - cross-platform replacement for ``fcntl.flock(LOCK_EX)``
                        (fcntl does not exist on Windows).
* localize_stored_path - stored absolute paths written while the project lived on
                        Google Drive (``/content/drive/.../builds/<id>``) are rebased onto
                        the current project root by exact prefix match. Any other path
                        is returned unchanged, so all existing path-safety checks keep
                        their meaning.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Project root used while the application ran in Google Colab.
DEFAULT_LEGACY_PROJECT_ROOTS = (
    "/content/drive/MyDrive/Multi-Agent-Business-Research/builds/20261122T173459146790Z",
    # Previous local folder of this project (before it was copied to "... (2)").
    # Research rows written there still store report paths under this root.
    "C:/Users/Administrator/Downloads/InsightIQ_Capstone_Project/InsightIQ",
)


@contextmanager
def closing_connection(*args, **kwargs):
    connection = sqlite3.connect(*args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


@contextmanager
def exclusive_file_lock(handle):
    """Hold an exclusive, blocking lock on an open binary file handle."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        # LK_LOCK retries for ~10 s before raising; loop for a fully blocking lock.
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                continue
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def legacy_project_roots():
    configured = os.getenv("INSIGHTIQ_LEGACY_PROJECT_ROOTS", "")
    extra = [item.strip() for item in configured.split(";") if item.strip()]
    return tuple(DEFAULT_LEGACY_PROJECT_ROOTS) + tuple(extra)


def localize_stored_path(stored, project_root=None):
    """Return the local Path for a path string stored by an earlier environment."""
    text = str(stored).replace("\\", "/")

    if project_root is None:
        from app.core.config import get_settings

        project_root = get_settings().project_root

    root = Path(project_root).resolve()

    for legacy in legacy_project_roots():
        legacy = legacy.replace("\\", "/").rstrip("/")
        if text.startswith(legacy + "/"):
            relative = text[len(legacy) + 1:]
            return (root / Path(*relative.split("/"))).resolve()

    return Path(stored).resolve()
