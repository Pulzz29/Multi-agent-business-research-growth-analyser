import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.database.sqlite_manager import connect


VALID_STATUSES = {
    "queued",
    "running",
    "completed",
    "failed",
}

ALLOWED_TRANSITIONS = {
    "queued": {"running", "failed"},
    "running": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}


class ResearchJobError(RuntimeError):
    pass


# Cancellation requests for running jobs (in this backend process). A
# cancelled job is recorded as status "failed" with error_code "cancelled"
# (the jobs table's status set is unchanged), and no report is saved.
import threading as _threading

_CANCEL_REQUESTS = set()
_CANCEL_LOCK = _threading.Lock()


def request_cancel(root, run_id):
    """Cancel a queued job immediately or ask a running job to stop."""
    validate_run_id(run_id)
    job = read_job(run_id)
    if job["status"] == "queued":
        transition_job(root, run_id, "failed", error_type="JobCancelled", error_code="cancelled")
        return read_job(run_id)
    if job["status"] == "running":
        with _CANCEL_LOCK:
            _CANCEL_REQUESTS.add(run_id)
        return {**job, "cancel_requested": True}
    raise ResearchJobError("Only queued or running research jobs can be cancelled.")


def cancel_requested(run_id):
    with _CANCEL_LOCK:
        return run_id in _CANCEL_REQUESTS


def _clear_cancel(run_id):
    with _CANCEL_LOCK:
        _CANCEL_REQUESTS.discard(run_id)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_run_id(run_id):
    try:
        canonical = str(uuid.UUID(str(run_id)))
    except (ValueError, TypeError, AttributeError):
        raise ResearchJobError(
            "Invalid research run identifier."
        )

    if canonical != str(run_id):
        raise ResearchJobError(
            "Research run ID must be canonical UUID."
        )

    return canonical


def mirror_directory(root):
    return Path(root) / "data" / "research_job_journal"


def mirror_path(root, run_id):
    validate_run_id(run_id)

    return mirror_directory(root) / (
        run_id + ".json"
    )


def read_job(run_id):
    validate_run_id(run_id)

    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM research_execution_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    if row is None:
        raise ResearchJobError(
            "Research job was not found."
        )

    result = dict(row)

    result["agent_statuses"] = json.loads(
        result.pop("agent_statuses_json")
    )

    return result


def write_drive_mirror(root, record):
    """
    Mirror job lifecycle metadata, not provider
    credentials or sensitive exception messages.
    """
    from app.services import document_store

    path = mirror_path(
        root,
        record["run_id"],
    )

    # PostgreSQL: stored_documents row; SQLite: atomic file write.
    document_store.write_text(
        path,
        json.dumps(
            record,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ),
        root=root,
    )

    return path


class _SkipFileBackup(Exception):
    """PostgreSQL backend: no SQLite file backup after a completed run."""


def initialize_job_storage():
    """
    Independent versioned migration for this new
    execution-journal table. Existing SEC and news
    schemas are not altered.
    """
    from app.database import db

    if db.is_postgres():
        # Created by the versioned PostgreSQL migrations (app/database/migrations).
        from app.database.migrations import run_migrations

        run_migrations()
        return True

    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            research_execution_migrations (
                version INTEGER PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            )
            """
        )

        version = conn.execute(
            """
            SELECT version
            FROM research_execution_migrations
            WHERE version = 1
            """
        ).fetchone()

        if version is None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS
                research_execution_runs (
                    run_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (
                            status IN (
                                'queued',
                                'running',
                                'completed',
                                'failed'
                            )
                        ),
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    started_at_utc TEXT,
                    finished_at_utc TEXT,
                    agent_statuses_json TEXT NOT NULL,
                    report_path TEXT,
                    error_type TEXT,
                    error_code TEXT
                )
                """
            )

            conn.execute(
                """
                INSERT INTO
                research_execution_migrations (
                    version, applied_at_utc
                )
                VALUES (1, ?)
                """,
                (utc_now(),),
            )

        required = {
            "run_id",
            "ticker",
            "status",
            "created_at_utc",
            "updated_at_utc",
            "started_at_utc",
            "finished_at_utc",
            "agent_statuses_json",
            "report_path",
            "error_type",
            "error_code",
        }

        from app.database.db import table_columns

        columns = set(table_columns(conn, "research_execution_runs"))

        if not required.issubset(columns):
            raise ResearchJobError(
                "Research execution table schema is incomplete."
            )

    return True


def active_job_for_ticker(ticker):
    """Queued or running job for a company, if any (read-only)."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT run_id, status
            FROM research_execution_runs
            WHERE ticker = ?
              AND status IN ('queued', 'running')
            ORDER BY created_at_utc DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()

    return dict(row) if row else None


def create_job(root, ticker, run_id=None):
    from app.services.market_registry import is_research_ticker

    # The six SEC-validated companies, or any registered market company.
    if not is_research_ticker(ticker):
        raise ResearchJobError(
            "Unsupported company ticker."
        )

    run_id = validate_run_id(
        run_id or str(uuid.uuid4())
    )

    now = utc_now()

    statuses = {
        "financial": "pending",
        "news": "pending",
        "strategic": "pending",
    }

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO research_execution_runs (
                run_id, ticker, status,
                created_at_utc, updated_at_utc,
                agent_statuses_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ticker,
                "queued",
                now,
                now,
                json.dumps(statuses, sort_keys=True),
            ),
        )

    record = read_job(run_id)

    write_drive_mirror(root, record)

    return record


def transition_job(
    root,
    run_id,
    next_status,
    agent_statuses=None,
    report_path=None,
    error_type=None,
    error_code=None,
):
    if next_status not in VALID_STATUSES:
        raise ResearchJobError(
            "Unsupported research job status."
        )

    current = read_job(run_id)

    if next_status not in ALLOWED_TRANSITIONS[
        current["status"]
    ]:
        raise ResearchJobError(
            "Invalid research job status transition: "
            + current["status"]
            + " -> "
            + next_status
        )

    if next_status == "completed":
        if not report_path:
            raise ResearchJobError(
                "Completed job requires saved report."
            )

        from app.services import document_store

        if not document_store.exists(report_path, root=root):
            raise ResearchJobError(
                "Completed job report does not exist."
            )

        required_completed = {
            "financial": "completed",
            "news": "completed",
            "strategic": "completed",
        }

        if agent_statuses != required_completed:
            raise ResearchJobError(
                "Completed job requires all agents completed."
            )

    statuses = (
        agent_statuses
        if agent_statuses is not None
        else current["agent_statuses"]
    )

    if set(statuses) != {
        "financial", "news", "strategic"
    }:
        raise ResearchJobError(
            "Research agent status keys do not match."
        )

    if not set(statuses.values()).issubset({
        "pending",
        "running",
        "completed",
        "failed",
    }):
        raise ResearchJobError(
            "Invalid research agent status."
        )

    now = utc_now()

    started = (
        now if next_status == "running"
        else current["started_at_utc"]
    )

    finished = (
        now if next_status in {"completed", "failed"}
        else None
    )

    # Conditional update prevents a second writer
    # from silently replacing a newer job status.
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE research_execution_runs
            SET
                status = ?,
                updated_at_utc = ?,
                started_at_utc = ?,
                finished_at_utc = ?,
                agent_statuses_json = ?,
                report_path = ?,
                error_type = ?,
                error_code = ?
            WHERE run_id = ?
              AND status = ?
            """,
            (
                next_status,
                now,
                started,
                finished,
                json.dumps(statuses, sort_keys=True),
                report_path,
                error_type,
                error_code,
                run_id,
                current["status"],
            ),
        )

        if cursor.rowcount != 1:
            raise ResearchJobError(
                "Research job was changed concurrently."
            )

    record = read_job(run_id)

    write_drive_mirror(root, record)

    return record


def recover_interrupted_jobs(root):
    """
    Call during a controlled single-process restart.

    Any job still marked running is treated as
    interrupted. Never mark it completed without
    its validated final report.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id
            FROM research_execution_runs
            WHERE status = 'running'
            """
        ).fetchall()

    recovered = []

    for row in rows:
        run_id = row["run_id"]
        previous = read_job(run_id)

        statuses = {
            name: (
                "failed"
                if status == "running"
                else status
            )
            for name, status in previous[
                "agent_statuses"
            ].items()
        }

        # Pending agents remain pending because they
        # were never confirmed to have executed.
        updated = transition_job(
            root,
            run_id,
            "failed",
            agent_statuses=statuses,
            error_type="InterruptedExecution",
            error_code="runtime_interrupted",
        )

        recovered.append(updated["run_id"])

    return recovered


def execute_research_job(root, ticker, graph_runner):
    """
    Run a real LangGraph workflow and persist its
    complete result only after validation.

    Never persist raw exceptions, provider keys, or
    credential-bearing request payloads.
    """
    created = create_job(root, ticker)
    run_id = created["run_id"]

    transition_job(
        root,
        run_id,
        "running",
    )

    try:
        result = graph_runner(
            ticker,
            run_id=run_id,
        )

        if result.get("run_id") != run_id:
            raise ResearchJobError(
                "Graph and journal run IDs differ."
            )

        if result.get("status") != "completed":
            raise ResearchJobError(
                "LangGraph run did not complete."
            )

        expected = {
            "financial": "completed",
            "news": "completed",
            "strategic": "completed",
        }

        if result.get("agent_statuses") != expected:
            raise ResearchJobError(
                "LangGraph agent statuses are incomplete."
            )

        path = (
            Path(root)
            / "reports"
            / "research_runs"
            / (run_id + ".json")
        )

        from app.services import document_store

        document_store.write_text(
            path,
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ),
            root=root,
        )

        transition_job(
            root,
            run_id,
            "completed",
            agent_statuses=expected,
            report_path=str(path),
        )

        return result, read_job(run_id)

    except Exception as exc:
        # No raw error message: it might contain
        # provider details or private information.
        current = read_job(run_id)

        if current["status"] == "running":
            transition_job(
                root,
                run_id,
                "failed",
                error_type=type(exc).__name__,
                error_code="research_execution_failed",
            )

        raise

AGENT_NAMES_IN_ORDER = ("financial", "news", "strategic")


def observed_agent_statuses(run_id):
    """
    Per-agent outcome of a failed run, derived from the
    LangGraph trace events persisted by agent_tracing
    (component + step_completed / step_failed).

    Agents with no recorded execution stay "pending",
    matching recover_interrupted_jobs(). Returns None when
    no agent trace is available.
    """
    try:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT component, event_type
                FROM research_observability_events
                WHERE run_id = ?
                  AND step_name = 'langgraph_execution'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
    except Exception:
        return None

    statuses = {name: "pending" for name in AGENT_NAMES_IN_ORDER}
    observed = False

    for row in rows:
        component = row["component"]

        if component not in statuses:
            continue

        observed = True

        if row["event_type"] == "step_completed":
            statuses[component] = "completed"
        elif row["event_type"] == "step_failed":
            statuses[component] = "failed"

    if not observed:
        return None

    return statuses


def execute_queued_research_job(root, run_id, graph_runner):
    """
    Execute an existing queued research job.

    A completed report is published only after its
    identity, status and agent results are validated.
    """
    validate_run_id(run_id)

    job = read_job(run_id)

    if job["status"] != "queued":
        raise ResearchJobError(
            "Only queued research jobs can be executed."
        )

    ticker = job["ticker"]

    transition_job(
        root,
        run_id,
        "running",
        agent_statuses={
            "financial": "running",
            "news": "pending",
            "strategic": "pending",
        },
    )

    try:
        import time as _time

        from app.core.config import get_settings
        from app.services.agent_tracing import (
            traced_research_runner,
        )
        from app.services.ai_provider import (
            JobCancelled,
            check_job_continuation,
            job_context,
        )

        deadline = _time.monotonic() + get_settings().research_job_deadline_seconds
        with job_context(cancel_check=lambda: cancel_requested(run_id), deadline_monotonic=deadline):
            result = traced_research_runner(
                graph_runner,
                ticker,
                run_id,
            )
            # Agents may fall back after a cancelled AI call; a cancelled or
            # overdue job never publishes a report.
            check_job_continuation()

        expected_statuses = {
            "financial": "completed",
            "news": "completed",
            "strategic": "completed",
        }

        if result.get("run_id") != run_id:
            raise ResearchJobError(
                "Research run ID mismatch."
            )

        if result.get("ticker") != ticker:
            raise ResearchJobError(
                "Research company mismatch."
            )

        if result.get("status") != "completed":
            raise ResearchJobError(
                "Research workflow did not complete."
            )

        if result.get("agent_statuses") != expected_statuses:
            raise ResearchJobError(
                "Research agents did not all complete."
            )

        report_path = (
            Path(root)
            / "reports"
            / "research_runs"
            / (run_id + ".json")
        )

        from app.services import document_store

        if document_store.exists(report_path, root=root):
            # Reports are immutable: never overwrite a saved report.
            raise ResearchJobError(
                "A report already exists for this research run."
            )

        document_store.write_text(
            report_path,
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ),
            root=root,
        )

        transition_job(
            root,
            run_id,
            "completed",
            agent_statuses=expected_statuses,
            report_path=str(report_path),
        )

        # Backup is separate from research success.
        # A failed backup must not turn a completed
        # research job into a failed research job.
        from app.database import db
        from app.services.research_backup import (
            backup_completed_research_job,
        )

        try:
            if db.is_postgres():
                # The completed run is committed in PostgreSQL; SQLite file
                # backups do not apply (database backups: pg_dump).
                raise _SkipFileBackup()
            backup_outcome = backup_completed_research_job(
                root,
                run_id,
            )

            if backup_outcome['backup_status'] != 'verified':
                print(
                    '[InsightIQ] Research completed, '
                    'but database backup needs attention.'
                )

        except _SkipFileBackup:
            pass
        except Exception:
            print(
                '[InsightIQ] Research completed, '
                'but backup verification was unavailable.'
            )

        # Executive summary is also separate from research
        # success: it is written to a sidecar file and never
        # modifies the saved report or the job status.
        if os.getenv(
            "INSIGHTIQ_AUTO_EXECUTIVE_SUMMARY", "1"
        ).strip() != "0":
            try:
                # Same path as the report page and the button (one
                # implementation, one per-run registry for dedupe).
                from app.services.executive_summary import (
                    start_brief,
                )

                start_brief(
                    root,
                    run_id,
                    ticker,
                    trigger="research_job",
                    background=False,
                )
            except Exception:
                print(
                    '[InsightIQ] Research completed, '
                    'but the executive summary is unavailable.'
                )

        return result

    except Exception as exc:
        # Never save raw provider error messages
        # or credential-bearing request details.
        try:
            current = read_job(run_id)

            if current["status"] == "running":
                # Record what actually happened to each agent
                # (e.g. financial completed, news failed,
                # strategic never ran). Without trace evidence,
                # keep the original conservative marking.
                refresh_failure = (
                    type(exc).__name__ == "SourceRefreshError"
                )
                error_code = (
                    "source_refresh_failed" if refresh_failure
                    else "cancelled" if type(exc).__name__ == "JobCancelled"
                    else "deadline_exceeded" if type(exc).__name__ == "JobDeadlineExceeded"
                    else "research_execution_failed"
                )

                if refresh_failure:
                    # The run stopped before any agent started.
                    agent_statuses = {
                        "financial": "pending",
                        "news": "pending",
                        "strategic": "pending",
                    }
                else:
                    agent_statuses = observed_agent_statuses(run_id) or {
                        "financial": "failed",
                        "news": "failed",
                        "strategic": "failed",
                    }

                transition_job(
                    root,
                    run_id,
                    "failed",
                    agent_statuses=agent_statuses,
                    error_type=type(exc).__name__,
                    error_code=error_code,
                )
        except Exception:
            # A storage failure must not disguise
            # the original execution failure.
            pass

        raise

    finally:
        _clear_cancel(run_id)
