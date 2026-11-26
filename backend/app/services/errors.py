"""
Central, sanitised error recording (PostgreSQL ``error_events``).

* Messages and stack traces are redacted (``security.redact``), stripped of
  absolute paths and truncated; no request bodies, prompts or provider
  payloads are stored.
* One row per error chain: the fingerprint (trace, error type, message) is
  unique, so the same exception propagating through nested spans or retried
  handlers updates one record (occurrences, last stage status) instead of
  creating duplicates. The innermost stage, where the error arose, is kept.
* Recording never raises. When PostgreSQL is unavailable the sanitised record
  goes to the server log instead.
"""

import hashlib
import json
import logging
import re
import traceback
from datetime import datetime, timezone

logger = logging.getLogger("insightiq.errors")

_PATH = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s\"']*?[\\/](app|scripts|tests)[\\/]")
EXPECTED = {"HTTPException", "ResponseRejected", "ChatError", "ReportUnavailable", "ReportHistoryError",
            "RequestValidationError", "AuthError"}

FRIENDLY = "Something went wrong on our side. Please try again in a moment."


_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\n\"']*?([^\\/\n\"']+\.(py|json|txt|log|sqlite3))")
_ANY_ABS = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|Users|usr|opt|app|var|tmp)/)[^\n\"']*")


def sanitize(text, limit=2000):
    from app.core.security import redact

    text = redact(str(text or ""))
    # Absolute paths (which may contain user or folder names, spaces included)
    # are reduced to the file name.
    text = _WIN_PATH.sub(lambda m: m.group(1), text)
    text = _PATH.sub(lambda m: m.group(1) + "/", text)
    text = _ANY_ABS.sub("<path>", text)
    text = re.sub(r"postgres(ql)?://\S+", "[database url]", text)
    return text[:limit]


def _stack(exc):
    if exc is None:
        return None
    frames = traceback.format_exception(type(exc), exc, exc.__traceback__, limit=12)
    return sanitize("".join(frames), 6000)


def record(stage, exc=None, *, message=None, severity="error", component=None, run_id=None,
           conversation_id=None, trace_id=None, ticker=None, retries=None, duration_ms=None,
           final_status="failed"):
    """Persist one sanitised error record (deduplicated). Never raises."""
    try:
        from app.services import telemetry

        if exc is not None and type(exc).__name__ in EXPECTED:
            return None
        trace_id = trace_id or telemetry.current_trace_id()
        run_id = run_id or telemetry.current_run_id()
        error_type = type(exc).__name__ if exc is not None else "Error"
        text = sanitize(message or (str(exc) if exc is not None else "") or error_type, 1000)
        fingerprint = hashlib.sha256(
            f"{trace_id or run_id or conversation_id or ''}|{error_type}|{text}".encode()).hexdigest()
        now = datetime.now(timezone.utc)
        row = (fingerprint, now, now, severity, str(stage)[:120], (component or "")[:120] or None, run_id,
               conversation_id, trace_id, ticker, error_type[:120], text, _stack(exc),
               retries, None if duration_ms is None else round(float(duration_ms), 3), final_status)
        if not telemetry.enabled():
            raise RuntimeError("database telemetry unavailable")
        from app.database import db

        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO error_events(fingerprint, first_seen_at, last_seen_at, severity, stage, component, "
                "run_id, conversation_id, trace_id, ticker, error_type, message, stack_trace, retries, "
                "duration_ms, final_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (fingerprint) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at, "
                "occurrences = error_events.occurrences + 1, final_status = EXCLUDED.final_status, "
                "retries = COALESCE(EXCLUDED.retries, error_events.retries), "
                "run_id = COALESCE(error_events.run_id, EXCLUDED.run_id), "
                "ticker = COALESCE(error_events.ticker, EXCLUDED.ticker), "
                "conversation_id = COALESCE(error_events.conversation_id, EXCLUDED.conversation_id), "
                "component = COALESCE(error_events.component, EXCLUDED.component)", row)
        return fingerprint
    except Exception as failure:
        try:
            logger.error("error_event %s", json.dumps({
                "stage": str(stage)[:120], "severity": severity, "component": component, "run_id": run_id,
                "conversation_id": conversation_id, "ticker": ticker,
                "error_type": type(exc).__name__ if exc is not None else "Error",
                "message": sanitize(message or exc or "", 500), "final_status": final_status,
                "storage": type(failure).__name__}, default=str))
        except Exception:
            pass
        return None


def recent(limit=50, hours=24 * 7):
    """Sanitised recent errors for operators (no stack traces)."""
    from datetime import timedelta

    from app.database import db

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT severity, stage, component, error_type, final_status, occurrences, last_seen_at "
            "FROM error_events WHERE last_seen_at >= ? ORDER BY last_seen_at DESC LIMIT ?", (since, limit)).fetchall()
    return [dict(r) for r in rows]
