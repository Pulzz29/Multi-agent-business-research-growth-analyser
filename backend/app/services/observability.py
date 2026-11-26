import io
import json
import logging
import math
import re
import time
import uuid

from contextlib import contextmanager
from datetime import datetime, timezone

from opentelemetry import trace
from opentelemetry.trace import (
    Status,
    StatusCode,
)

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.database.sqlite_manager import connect


class ObservabilityError(RuntimeError):
    pass


VALID_COMPONENTS = frozenset({
    "api",
    "storage",
    "financial",
    "news",
    "strategic",
    "worker",
    "backup",
    "system",
})

VALID_EVENT_TYPES = frozenset({
    "step_completed",
    "step_failed",
})

ALLOWED_METADATA = frozenset({
    "http_status",
    "record_count",
    "report_available",
    "error_code",
})

VALID_STEP = re.compile(
    r"^[a-z][a-z0-9_]{0,63}$"
)

VALID_TRACE_ID = re.compile(
    r"^[0-9a-f]{32}$"
)

VALID_SPAN_ID = re.compile(
    r"^[0-9a-f]{16}$"
)

VALID_ERROR_CODE = re.compile(
    r"^[a-z][a-z0-9_]{0,63}$"
)


def utc_now():
    return datetime.now(
        timezone.utc
    ).isoformat()


def validate_run_id(run_id):
    try:
        canonical = str(
            uuid.UUID(str(run_id))
        )

        if canonical != run_id:
            raise ValueError(
                "Noncanonical research identifier."
            )

    except (TypeError, ValueError, AttributeError):
        raise ObservabilityError(
            "Invalid research run identifier."
        )

    return run_id


def validate_metadata(metadata):
    if metadata is None:
        return {}

    if not isinstance(metadata, dict):
        raise ObservabilityError(
            "Trace metadata must be a dictionary."
        )

    if not set(metadata).issubset(
        ALLOWED_METADATA
    ):
        raise ObservabilityError(
            "Trace metadata contains forbidden fields."
        )

    result = {}

    for key, value in metadata.items():

        if key == "http_status":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 100 <= value <= 599
            ):
                raise ObservabilityError(
                    "Invalid HTTP status."
                )

        elif key == "record_count":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ObservabilityError(
                    "Invalid record count."
                )

        elif key == "report_available":
            if not isinstance(value, bool):
                raise ObservabilityError(
                    "Invalid report availability."
                )

        elif key == "error_code":
            if (
                not isinstance(value, str)
                or VALID_ERROR_CODE.fullmatch(value)
                is None
            ):
                raise ObservabilityError(
                    "Invalid sanitized error code."
                )

        result[key] = value

    return result


def initialize_observability():
    """
    Create a versioned SQLite trace-event schema.

    The original database tables and research
    execution records are not modified.
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
            observability_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            research_observability_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL,
                parent_span_id TEXT,
                component TEXT NOT NULL,
                step_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms REAL NOT NULL,
                timestamp_utc TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_research_observability_run
            ON research_observability_events (
                run_id,
                id
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_research_observability_trace
            ON research_observability_events (
                trace_id,
                id
            )
            """
        )

        conn.execute(
            """
            INSERT INTO
            observability_schema_migrations (
                version,
                applied_at_utc
            )
            VALUES (?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                1,
                utc_now(),
            ),
        )

        from app.database.db import table_columns

        columns = set(table_columns(conn, "research_observability_events"))

        required = {
            "id",
            "run_id",
            "trace_id",
            "span_id",
            "parent_span_id",
            "component",
            "step_name",
            "event_type",
            "status",
            "duration_ms",
            "timestamp_utc",
            "metadata_json",
        }

        if not required.issubset(columns):
            raise ObservabilityError(
                "Trace-event table schema mismatch."
            )

    return True


def persist_trace_event(
    *,
    run_id,
    trace_id,
    span_id,
    parent_span_id,
    component,
    step_name,
    event_type,
    duration_ms,
    metadata=None,
):
    validate_run_id(run_id)

    if (
        not isinstance(trace_id, str)
        or VALID_TRACE_ID.fullmatch(trace_id)
        is None
    ):
        raise ObservabilityError(
            "Invalid trace identifier."
        )

    if (
        not isinstance(span_id, str)
        or VALID_SPAN_ID.fullmatch(span_id)
        is None
    ):
        raise ObservabilityError(
            "Invalid span identifier."
        )

    if parent_span_id is not None:
        if (
            not isinstance(parent_span_id, str)
            or VALID_SPAN_ID.fullmatch(
                parent_span_id
            ) is None
        ):
            raise ObservabilityError(
                "Invalid parent span identifier."
            )

    if component not in VALID_COMPONENTS:
        raise ObservabilityError(
            "Invalid trace component."
        )

    if (
        not isinstance(step_name, str)
        or VALID_STEP.fullmatch(step_name)
        is None
    ):
        raise ObservabilityError(
            "Invalid trace step."
        )

    if event_type not in VALID_EVENT_TYPES:
        raise ObservabilityError(
            "Invalid trace event type."
        )

    if (
        isinstance(duration_ms, bool)
        or not isinstance(
            duration_ms,
            (int, float),
        )
        or not math.isfinite(duration_ms)
        or duration_ms < 0
    ):
        raise ObservabilityError(
            "Invalid trace duration."
        )

    safe_metadata = validate_metadata(
        metadata
    )

    status = (
        "completed"
        if event_type == "step_completed"
        else "failed"
    )

    timestamp = utc_now()

    with connect() as conn:

        cursor = conn.execute(
            """
            INSERT INTO
            research_observability_events (
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
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                run_id,
                trace_id,
                span_id,
                parent_span_id,
                component,
                step_name,
                event_type,
                status,
                float(duration_ms),
                timestamp,
                json.dumps(
                    safe_metadata,
                    sort_keys=True,
                    allow_nan=False,
                ),
            ),
        )

        event_id = cursor.fetchone()[0]

    return {
        "id": event_id,
        "run_id": run_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "component": component,
        "step_name": step_name,
        "event_type": event_type,
        "status": status,
        "duration_ms": round(
            float(duration_ms),
            3,
        ),
        "timestamp_utc": timestamp,
        "metadata": safe_metadata,
    }


class SafeJSONFormatter(logging.Formatter):
    """
    Include only explicitly approved log fields.

    Raw exceptions, API keys, provider responses,
    source articles and prompts are excluded.
    """

    FIELDS = frozenset({
        "message",
        "run_id",
        "trace_id",
        "span_id",
        "component",
        "step_name",
        "event_type",
        "duration_ms",
    })

    def format(self, record):
        source = record.msg

        if not isinstance(source, dict):
            source = {
                "message": "observability_event"
            }

        safe = {
            key: value
            for key, value in source.items()
            if key in self.FIELDS
        }

        return json.dumps(
            {
                "timestamp_utc": utc_now(),
                "level": record.levelname,
                **safe,
            },
            sort_keys=True,
            allow_nan=False,
        )


logger = logging.getLogger(
    "insightiq.observability"
)

logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    handler = logging.StreamHandler()

    handler.setFormatter(
        SafeJSONFormatter()
    )

    logger.addHandler(handler)


def build_tracing():
    """
    Create an isolated SDK tracer and in-memory
    span exporter for controlled verification.

    Persistent execution events are independently
    stored in SQLite.
    """
    exporter = InMemorySpanExporter()

    provider = TracerProvider()

    provider.add_span_processor(
        SimpleSpanProcessor(exporter)
    )

    tracer = provider.get_tracer(
        "insightiq.backend",
        "0.1.0",
    )

    return tracer, exporter, provider


@contextmanager
def observed_step(
    tracer,
    run_id,
    component,
    step_name,
    metadata=None,
):
    """
    Trace a real operation and persist its result.

    Does not log exceptions, exception messages,
    provider payloads or credentials.
    """
    validate_run_id(run_id)

    if component not in VALID_COMPONENTS:
        raise ObservabilityError(
            "Invalid trace component."
        )

    if (
        not isinstance(step_name, str)
        or VALID_STEP.fullmatch(step_name)
        is None
    ):
        raise ObservabilityError(
            "Invalid trace step name."
        )

    safe_metadata = validate_metadata(
        metadata
    )

    started = time.perf_counter()
    outcome = "step_completed"

    with tracer.start_as_current_span(
        component + "." + step_name
    ) as span:

        span.set_attribute(
            "insightiq.run_id",
            run_id,
        )

        span.set_attribute(
            "insightiq.component",
            component,
        )

        span.set_attribute(
            "insightiq.step_name",
            step_name,
        )

        try:
            yield span

        except BaseException:
            outcome = "step_failed"

            span.set_status(
                Status(StatusCode.ERROR)
            )

            raise

        finally:
            elapsed_ms = (
                time.perf_counter()
                - started
            ) * 1000.0

            span_context = (
                span.get_span_context()
            )

            trace_id = format(
                span_context.trace_id,
                "032x",
            )

            span_id = format(
                span_context.span_id,
                "016x",
            )

            parent_context = getattr(
                span,
                "parent",
                None,
            )

            parent_span_id = (
                format(
                    parent_context.span_id,
                    "016x",
                )
                if parent_context is not None
                and parent_context.span_id != 0
                else None
            )

            event = persist_trace_event(
                run_id=run_id,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                component=component,
                step_name=step_name,
                event_type=outcome,
                duration_ms=elapsed_ms,
                metadata=safe_metadata,
            )

            logger.info({
                "message": "research_trace_event",
                "run_id": run_id,
                "trace_id": trace_id,
                "span_id": span_id,
                "component": component,
                "step_name": step_name,
                "event_type": outcome,
                "duration_ms": event[
                    "duration_ms"
                ],
            })


def events_for_run(run_id):
    validate_run_id(run_id)

    with connect() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM research_observability_events
            WHERE run_id = ?
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()

    events = []

    for row in rows:
        entry = dict(row)

        entry["metadata"] = json.loads(
            entry.pop("metadata_json")
        )

        events.append(entry)

    return events
