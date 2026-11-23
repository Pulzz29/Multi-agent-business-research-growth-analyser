import math
import uuid

from app.database.sqlite_manager import connect
from app.services.observability import (
    validate_metadata,
)


class TraceExplorerError(RuntimeError):
    pass


def canonical_run_id(run_id):
    try:
        canonical = str(uuid.UUID(str(run_id)))
    except (ValueError, TypeError, AttributeError):
        raise TraceExplorerError(
            "Invalid research run identifier."
        )

    if canonical != run_id:
        raise TraceExplorerError(
            "Research run identifier is not canonical."
        )

    return canonical


def research_run_exists(run_id):
    canonical_run_id(run_id)

    with connect() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM research_execution_runs
            WHERE run_id = ?
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

    return row is not None


def trace_explorer_for_run(run_id, limit=100):
    canonical_run_id(run_id)

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 200
    ):
        raise TraceExplorerError(
            "Invalid trace-event limit."
        )

    if not research_run_exists(run_id):
        raise LookupError(
            "Research run not found."
        )

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                run_id,
                trace_id,
                span_id,
                parent_span_id,
                component,
                step_name,
                event_type,
                status,
                duration_ms,
                timestamp_utc,
                metadata_json
            FROM research_observability_events
            WHERE run_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()

    import json

    events = []

    for row in reversed(rows):
        metadata = validate_metadata(
            json.loads(row["metadata_json"])
        )

        events.append({
            "id": row["id"],
            "trace_id": row["trace_id"],
            "span_id": row["span_id"],
            "parent_span_id": row["parent_span_id"],
            "component": row["component"],
            "step_name": row["step_name"],
            "event_type": row["event_type"],
            "status": row["status"],
            "duration_ms": round(
                row["duration_ms"], 3
            ),
            "timestamp_utc": row["timestamp_utc"],
            "metadata": metadata,
        })

    traces = {}

    for event in events:
        trace_id = event["trace_id"]

        if trace_id not in traces:
            traces[trace_id] = {
                "trace_id": trace_id,
                "span_count": 0,
                "root_span_count": 0,
                "failed_span_count": 0,
            }

        group = traces[trace_id]

        group["span_count"] += 1

        if event["parent_span_id"] is None:
            group["root_span_count"] += 1

        if event["status"] == "failed":
            group["failed_span_count"] += 1

    return {
        "run_id": run_id,
        "event_count": len(events),
        "trace_count": len(traces),
        "limit": limit,
        "traces": list(traces.values()),
        "events": events,
    }


def nearest_rank_p95(values):
    """
    Empirical 95th-percentile latency using the
    nearest-rank method.

    For one observation, p95 equals that observation.
    """
    if not values:
        return None

    ordered = sorted(float(v) for v in values)

    index = max(
        0,
        math.ceil(0.95 * len(ordered)) - 1,
    )

    return round(ordered[index], 3)


def performance_metrics(sample_limit=1000):
    """
    Summarize actual persisted research execution
    spans, not fabricated metrics or forecasts.

    Values refer to the most recent matching spans
    stored in SQLite.
    """
    if (
        isinstance(sample_limit, bool)
        or not isinstance(sample_limit, int)
        or not 1 <= sample_limit <= 5000
    ):
        raise TraceExplorerError(
            "Invalid metric sample limit."
        )

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                run_id,
                component,
                step_name,
                status,
                duration_ms
            FROM research_observability_events
            WHERE
                (
                    component = 'worker'
                    AND step_name = 'research_workflow'
                )
                OR
                (
                    component IN (
                        'financial',
                        'news',
                        'strategic'
                    )
                    AND step_name = 'langgraph_execution'
                )
            ORDER BY id DESC
            LIMIT ?
            """,
            (sample_limit,),
        ).fetchall()

    components = (
        "worker",
        "financial",
        "news",
        "strategic",
    )

    metrics = {}

    for component in components:
        matching = [
            row
            for row in rows
            if row["component"] == component
        ]

        successful_durations = [
            float(row["duration_ms"])
            for row in matching
            if row["status"] == "completed"
        ]

        failures = sum(
            row["status"] == "failed"
            for row in matching
        )

        attempts = len(matching)

        metrics[component] = {
            "observed_span_count": attempts,
            "completed_span_count": len(
                successful_durations
            ),
            "failed_span_count": failures,
            "error_rate": (
                round(failures / attempts, 6)
                if attempts
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
        }

    run_ids = {
        row["run_id"]
        for row in rows
    }

    return {
        "metric_source": (
            "persisted_research_observability_events"
        ),
        "latency_unit": "milliseconds",
        "percentile_method": "nearest_rank",
        "sample_limit": sample_limit,
        "sampled_span_count": len(rows),
        "distinct_research_runs": len(run_ids),
        "components": metrics,
        "interpretation": (
            "Measured execution spans only. "
            "Small samples do not establish "
            "stable production latency percentiles."
        ),
    }
