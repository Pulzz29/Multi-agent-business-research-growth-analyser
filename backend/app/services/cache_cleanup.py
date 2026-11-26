"""
Periodic deletion of expired company-cache rows.

A daemon thread started with the API deletes expired rows of
``company_data_cache`` every CACHE_CLEANUP_INTERVAL_MINUTES (default 5). It
needs no external scheduler, and running it in several instances at once is
safe: the DELETE is idempotent and only removes rows that are already
expired (reads never serve them in the meantime).

Each pass also runs maintenance: telemetry/audit retention
(TELEMETRY_RETENTION_DAYS, AUDIT_RETENTION_DAYS), resolved alerts older than
90 days, abandoned temporary files older than an hour, and alert evaluation.
Financial history, saved reports, journals, forecast caches and backups are
never deleted here.
"""

import logging
import re
import time
from pathlib import Path
import threading

logger = logging.getLogger("insightiq.cache_cleanup")

_STATE = {"thread": None, "stop": None}
_GUARD = threading.Lock()


def interval_seconds():
    from app.core.config import get_settings

    return max(1.0, float(get_settings().cache_cleanup_interval_minutes) * 60.0)


def run_once():
    """One cleanup pass; never raises (a database outage is logged).
    Returns the number of expired company-cache rows deleted."""
    from app.services import company_cache

    try:
        deleted = company_cache.cleanup_expired()
    except Exception as exc:
        logger.warning("cache_cleanup failed: %s", type(exc).__name__)
        return None
    run_maintenance()
    return deleted


# Temporary files left by interrupted atomic writes (never data files).
TEMP_NAME = re.compile(r"(\.tmp(-[0-9a-f]{32})?$)|(\.\d+\.\d+\.tmp$)|(^\..+\.\d+\.\d+\.tmp$)|(\.restore\.tmp$)|(\.snapshot\.tmp$)")
TEMP_FOLDERS = ("reports", "data/research_job_journal", "data/forecast_cache_v1", "data/forecast_cache_v2")


def cleanup_temporary_files(root=None, older_than_seconds=3600, now=None):
    """Delete abandoned temporary files (older than an hour) in the report,
    journal and forecast-cache folders. Saved reports, financial data,
    database files and backups are never matched."""
    from app.core.config import get_settings

    root = Path(root or get_settings().project_root).resolve()
    now = now or time.time()
    deleted = []
    for folder in TEMP_FOLDERS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and TEMP_NAME.search(path.name) and now - path.stat().st_mtime > older_than_seconds:
                path.unlink(missing_ok=True)
                deleted.append(str(path.relative_to(root)))
    return deleted


def run_maintenance():
    """Telemetry retention, temporary files and alert evaluation (best effort)."""
    from app.services import telemetry

    for name, action in (("telemetry", telemetry.cleanup), ("temporary_files", cleanup_temporary_files),
                         ("alerts", telemetry.evaluate_alerts)):
        try:
            result = action()
            if result:
                logger.info("maintenance %s %s", name, result)
        except Exception as exc:
            logger.warning("maintenance %s failed: %s", name, type(exc).__name__)


def _loop(stop, interval):
    while not stop.wait(interval):
        run_once()


def start():
    """Start the cleanup thread once per process (first pass immediately)."""
    with _GUARD:
        thread = _STATE["thread"]
        if thread is not None and thread.is_alive():
            return thread
        stop = threading.Event()
        interval = interval_seconds()
        run_once()
        thread = threading.Thread(target=_loop, args=(stop, interval),
                                  name="insightiq-cache-cleanup", daemon=True)
        thread.start()
        _STATE.update(thread=thread, stop=stop)
        logger.info("cache_cleanup started interval_seconds=%s", interval)
        return thread


def stop(timeout=5.0):
    with _GUARD:
        thread, event = _STATE["thread"], _STATE["stop"]
        _STATE.update(thread=None, stop=None)
    if event is not None:
        event.set()
    if thread is not None:
        thread.join(timeout)
