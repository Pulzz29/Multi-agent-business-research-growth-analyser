from app.database.sqlite_manager import connect

from app.services.trace_explorer import (
    performance_metrics,
)

from app.services.reliability_monitoring import (
    current_reliability,
)

from app.services.runtime_startup import (
    read_backup_health,
)


# Companies on the validated SEC pipeline (live set; see sec_coverage.py).
from app.services.sec_coverage import SEC_VALIDATED as SUPPORTED_COMPANIES  # noqa: E402


class DashboardDataError(RuntimeError):
    pass


def dashboard_snapshot(
    project_root,
    recent_limit=10,
):
    """
    Aggregate actual stored application data.

    This function does not invoke research agents,
    model providers, or source ingestion services.
    """

    if (
        isinstance(recent_limit, bool)
        or not isinstance(recent_limit, int)
        or not 1 <= recent_limit <= 25
    ):
        raise DashboardDataError(
            "Invalid recent-job limit."
        )

    with connect() as conn:

        sec_total = conn.execute(
            """
            SELECT COUNT(*)
            FROM sec_facts
            """
        ).fetchone()[0]

        news_total = conn.execute(
            """
            SELECT COUNT(*)
            FROM news_articles
            """
        ).fetchone()[0]

        job_rows = conn.execute(
            """
            SELECT
                run_id,
                ticker,
                status,
                created_at_utc,
                updated_at_utc
            FROM research_execution_runs
            ORDER BY created_at_utc DESC, run_id DESC
            LIMIT ?
            """,
            (recent_limit,),
        ).fetchall()

        status_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM research_execution_runs
            GROUP BY status
            """
        ).fetchall()

    job_counts = {
        "queued": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }

    for row in status_rows:
        status = row["status"]

        if status not in job_counts:
            raise DashboardDataError(
                "Unexpected research-job status."
            )

        job_counts[status] = row["count"]

    recent_jobs = []

    for row in job_rows:
        if row["ticker"] not in SUPPORTED_COMPANIES:
            from app.services.market_registry import is_research_ticker

            if not is_research_ticker(row["ticker"]):
                raise DashboardDataError(
                    "Unexpected company identifier."
                )

        recent_jobs.append({
            "run_id": row["run_id"],
            "ticker": row["ticker"],
            "status": row["status"],
            "created_at_utc": row["created_at_utc"],
            "updated_at_utc": row["updated_at_utc"],
        })

    performance = performance_metrics(
        sample_limit=1000
    )

    reliability = current_reliability(
        sample_limit=1000
    )

    backup = read_backup_health(
        project_root
    )

    return {
        "application": "InsightIQ",
        "supported_companies": list(
            SUPPORTED_COMPANIES
        ),
        "source_data": {
            "sec_fact_count": sec_total,
            "news_article_count": news_total,
        },
        "research_jobs": {
            "status_counts": job_counts,
            "total_count": sum(
                job_counts.values()
            ),
            "recent": recent_jobs,
        },
        "performance": performance,
        "reliability": {
            "status": reliability["status"],
            "policies": reliability["policies"],
            "alert_count": reliability["alert_count"],
            "insufficient_data_count": (
                reliability["insufficient_data_count"]
            ),
            "job_sample_count": reliability[
                "metrics"
            ]["jobs"]["observed_count"],
            "workflow_span_sample_count": reliability[
                "metrics"
            ]["workflow"]["observed_span_count"],
        },
        "backup_health": backup,
        "limitations": {
            "historical_development_jobs_included": True,
            "small_samples_may_be_unrepresentative": True,
            "backup_health_is_read_only": True,
        },
    }
