"""Cross-platform (Windows/macOS/Linux) compatibility helpers.

These helpers change *how* resources are handled on the host OS, never *what* the
application computes:

* closing_connection  - same commit/rollback semantics as ``with sqlite3.connect(...)``
                        but also closes the connection. Windows refuses to replace or
                        delete a SQLite file that still has an open handle.
* exclusive_file_lock - cross-platform replacement for ``fcntl.flock(LOCK_EX)``
                        (fcntl does not exist on Windows).
* localize_stored_path - absolute paths stored by earlier deployments of the project
                        (see DEFAULT_LEGACY_PROJECT_ROOTS) are rebased onto the
                        current project root by exact prefix match. A path from any
                        other copy of the project (another machine or folder) is
                        rebased by its project-relative tail when that tail starts
                        with one of PROJECT_ANCHORS (the only directories stored
                        paths point into). Any other path is returned unchanged;
                        callers still require the exact expected location.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Project root recorded by the original Colab deployment (do not remove).
# Optional extra roots: INSIGHTIQ_LEGACY_PROJECT_ROOTS (";"-separated); not
# required, since PROJECT_ANCHORS rebases paths from any earlier project folder.
DEFAULT_LEGACY_PROJECT_ROOTS = (
    "/content/drive/MyDrive/Multi-Agent-Business-Research/builds/20261122T173459146790Z",
)

# Project directories that stored absolute paths point into (report files and
# SQLite recovery points).
PROJECT_ANCHORS = ("reports/research_runs/", "data/sqlite_backups/")


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

    direct = Path(stored).resolve()
    if direct.is_relative_to(root) or not (text.startswith("/") or text[1:3] == ":/"):
        return direct
    for anchor in PROJECT_ANCHORS:
        index = text.rfind("/" + anchor)
        if index >= 0:
            relative = text[index + 1:]
            if ".." not in relative.split("/"):
                return (root / Path(*relative.split("/"))).resolve()
    return direct
