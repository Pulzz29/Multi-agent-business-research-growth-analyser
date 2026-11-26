import json
import math
from datetime import datetime, timezone

from app.database.sqlite_manager import connect
from app.services.trace_explorer import nearest_rank_p95


class ReliabilityMonitoringError(RuntimeError):
    pass


MINIMUM_SAMPLE_SIZE = 10

JOB_FAILURE_THRESHOLD = 0.20
WORKFLOW_FAILURE_THRESHOLD = 0.20
WORKFLOW_P95_THRESHOLD_MS = 10000.0


def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat()


def initialize_monitoring():
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
            reliability_monitor_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluated_at_utc TEXT NOT NULL,
                sample_limit INTEGER NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_reliability_monitor_evaluated
            ON reliability_monitor_evaluations (
                id
            )
            """
        )

    return True


def validate_sample_limit(sample_limit):
    if (
        isinstance(sample_limit, bool)
        or not isinstance(sample_limit, int)
        or not 1 <= sample_limit <= 5000
    ):
        raise ReliabilityMonitoringError(
            "Invalid monitoring sample limit."
        )

    return sample_limit


def collect_reliability_metrics(sample_limit=1000):
    """
    Read actual research-job and workflow-span rows.

    Some historical jobs were created during
    development testing. These measurements are
    not yet production service-level statistics.
    """
    validate_sample_limit(sample_limit)

    with connect() as conn:

        job_rows = conn.execute(
            """
            SELECT
                run_id,
                status,
                error_code
            FROM research_execution_runs
            ORDER BY created_at_utc DESC, run_id DESC
            LIMIT ?
            """,
            (sample_limit,),
        ).fetchall()

        span_rows = conn.execute(
            """
            SELECT
                run_id,
                status,
                duration_ms
            FROM research_observability_events
            WHERE component = 'worker'
              AND step_name = 'research_workflow'
            ORDER BY id DESC
            LIMIT ?
            """,
            (sample_limit,),
        ).fetchall()

    statuses = {
        "queued": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }

    for row in job_rows:
        if row["status"] not in statuses:
            raise ReliabilityMonitoringError(
                "Unexpected stored research-job status."
            )

        statuses[row["status"]] += 1

    terminal_count = (
        statuses["completed"]
        + statuses["failed"]
    )

    job_failure_rate = (
        statuses["failed"] / terminal_count
        if terminal_count
        else None
    )

    successful_durations = []

    failed_workflow_spans = 0

    for row in span_rows:
        if row["status"] == "completed":
            duration = float(
                row["duration_ms"]
            )

            if (
                not math.isfinite(duration)
                or duration < 0
            ):
                raise ReliabilityMonitoringError(
                    "Invalid stored workflow duration."
                )

            successful_durations.append(
                duration
            )

        elif row["status"] == "failed":
            failed_workflow_spans += 1

        else:
            raise ReliabilityMonitoringError(
                "Unexpected workflow-span status."
            )

    workflow_count = len(span_rows)

    workflow_failure_rate = (
        failed_workflow_spans / workflow_count
        if workflow_count
        else None
    )

    return {
        "metric_source": "actual_sqlite_records",
        "sample_limit": sample_limit,
        "jobs": {
            "observed_count": len(job_rows),
            "queued_count": statuses["queued"],
            "running_count": statuses["running"],
            "completed_count": statuses["completed"],
            "failed_count": statuses["failed"],
            "terminal_count": terminal_count,
            "terminal_failure_rate": (
                round(job_failure_rate, 6)
                if job_failure_rate is not None
                else None
            ),
        },
        "workflow": {
            "observed_span_count": workflow_count,
            "completed_span_count": len(
                successful_durations
            ),
            "failed_span_count": failed_workflow_spans,
            "failure_rate": (
                round(workflow_failure_rate, 6)
                if workflow_failure_rate is not None
                else None
            ),
            "mean_success_latency_ms": (
                round(
                    sum(successful_durations)
                    / len(successful_durations),
                    3,
                )
                if successful_durations
                else None
            ),
            "p95_success_latency_ms": (
                nearest_rank_p95(
                    successful_durations
                )
            ),
        },
    }


def evaluate_policy(
    name,
    observed_count,
    value,
    threshold,
    minimum_samples=MINIMUM_SAMPLE_SIZE,
):
    """
    Deterministic numeric policy evaluation.

    Insufficient observations do not establish
    a healthy or alerting production condition.
    """
    if (
        isinstance(observed_count, bool)
        or not isinstance(observed_count, int)
        or observed_count < 0
    ):
        raise ReliabilityMonitoringError(
            "Invalid policy sample count."
        )

    if (
        isinstance(minimum_samples, bool)
        or not isinstance(minimum_samples, int)
        or minimum_samples < 1
    ):
        raise ReliabilityMonitoringError(
            "Invalid minimum sample size."
        )

    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not math.isfinite(threshold)
        or threshold < 0
    ):
        raise ReliabilityMonitoringError(
            "Invalid policy threshold."
        )

    if value is not None:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ReliabilityMonitoringError(
                "Invalid policy metric."
            )

    if (
        observed_count < minimum_samples
        or value is None
    ):
        state = "insufficient_data"

    elif value > threshold:
        state = "alerting"

    else:
        state = "normal"

    return {
        "policy": name,
        "state": state,
        "observed_count": observed_count,
        "minimum_samples": minimum_samples,
        "observed_value": value,
        "threshold": threshold,
    }


def evaluate_reliability(metrics):
    jobs = metrics["jobs"]
    workflow = metrics["workflow"]

    policies = [
        evaluate_policy(
            "research_job_failure_rate",
            jobs["terminal_count"],
            jobs["terminal_failure_rate"],
            JOB_FAILURE_THRESHOLD,
        ),
        evaluate_policy(
            "workflow_span_failure_rate",
            workflow["observed_span_count"],
            workflow["failure_rate"],
            WORKFLOW_FAILURE_THRESHOLD,
        ),
        evaluate_policy(
            "workflow_p95_latency_ms",
            workflow["completed_span_count"],
            workflow["p95_success_latency_ms"],
            WORKFLOW_P95_THRESHOLD_MS,
        ),
    ]

    states = {
        policy["state"]
        for policy in policies
    }

    if "alerting" in states:
        overall = "alerting"

    elif "insufficient_data" in states:
        overall = "insufficient_data"

    else:
        overall = "normal"

    return {
        "status": overall,
        "evaluated_at_utc": utc_now(),
        "metrics": metrics,
        "policies": policies,
        "alert_count": sum(
            policy["state"] == "alerting"
            for policy in policies
        ),
        "insufficient_data_count": sum(
            policy["state"] == "insufficient_data"
            for policy in policies
        ),
        "interpretation": (
            "Research measurements include development "
            "test activity and may not represent "
            "production traffic."
        ),
    }


def current_reliability(sample_limit=1000):
    metrics = collect_reliability_metrics(
        sample_limit=sample_limit
    )

    return evaluate_reliability(
        metrics
    )


def save_monitoring_evaluation(
    sample_limit=1000
):
    """
    Persist one actual monitoring evaluation.

    Does not create synthetic research jobs,
    spans, business facts, or model responses.
    """
    result = current_reliability(
        sample_limit=sample_limit
    )

    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO reliability_monitor_evaluations (
                evaluated_at_utc,
                sample_limit,
                status,
                result_json
            )
            VALUES (?, ?, ?, ?)
            RETURNING id
            """,
            (
                result["evaluated_at_utc"],
                sample_limit,
                result["status"],
                json.dumps(
                    result,
                    sort_keys=True,
                    allow_nan=False,
                ),
            ),
        )

        evaluation_id = cursor.fetchone()[0]

    return {
        "evaluation_id": evaluation_id,
        **result,
    }


def latest_monitoring_evaluation():
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, result_json
            FROM reliability_monitor_evaluations
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        return None

    result = json.loads(
        row["result_json"]
    )

    return {
        "evaluation_id": row["id"],
        **result,
    }
