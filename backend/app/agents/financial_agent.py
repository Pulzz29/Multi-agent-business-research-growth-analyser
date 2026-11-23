import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from app.core.config import get_settings
from app.database.sqlite_manager import connect
from app.services.financial_normalization import (
    normalized_quarterly_financials,
)
from app.services.financial_claims import (
    request_grounded_claims,
)
from app.services.forecast_cache import (
    forecast_financial_metric_cached,
)


SUPPORTED_TICKERS = frozenset({
    "AAPL", "AMZN", "GOOGL",
    "META", "MSFT", "NVDA",
})

FINANCIAL_METRICS = (
    "revenue",
    "operating_cash_flow",
)


class FinancialAgentError(RuntimeError):
    pass


class FinancialState(TypedDict, total=False):
    run_id: str
    ticker: str
    status: str
    financial_evidence: list[dict]
    verified_financial_claims: dict
    forecasts: dict
    forecast_quality: dict
    disclosures: list[str]


def active_root():
    return Path(
        get_settings().project_root
    ).resolve()


def load_financial_quality(ticker):
    path = (
        active_root()
        / "reports"
        / "forecast_uncertainty_quality_review.json"
    )

    if not path.is_file():
        raise FinancialAgentError(
            "Forecast quality review is missing."
        )

    document = json.loads(
        path.read_text(encoding="utf-8")
    )

    if document.get("assessment_failures"):
        raise FinancialAgentError(
            "Forecast quality review contains failures."
        )

    assessments = document["assessments"][ticker]

    return {
        metric: assessments[metric]
        for metric in FINANCIAL_METRICS
    }


def verify_financial_review(ticker):
    path = (
        active_root()
        / "reports"
        / "financial_evidence_reconciliation_final.json"
    )

    if not path.is_file():
        raise FinancialAgentError(
            "Financial SEC reconciliation is missing."
        )

    review = json.loads(
        path.read_text(encoding="utf-8")
    )

    if review.get("critical_issues"):
        raise FinancialAgentError(
            "Financial SEC reconciliation has critical issues."
        )

    if ticker not in review.get("companies", {}):
        raise FinancialAgentError(
            "Company is not covered by financial review."
        )

    return {
        "nonmaterial_differences": [
            issue
            for issue in review.get(
                "nonmaterial_differences", []
            )
            if issue.get("ticker") == ticker
        ],
        "historical_diagnostic_counts": review.get(
            "historical_diagnostic_counts", {}
        ),
    }


def build_verified_financial_evidence(ticker):
    financials = normalized_quarterly_financials(
        ticker
    )

    evidence_records = []

    for metric in FINANCIAL_METRICS:
        quarters = financials[metric][-4:]

        if len(quarters) != 4:
            raise FinancialAgentError(
                f"{ticker}: insufficient {metric} quarters."
            )

        for quarter in quarters:
            source_ids = [
                item["sec_fact_id"]
                for item in quarter["evidence"]
            ]

            if not source_ids:
                raise FinancialAgentError(
                    "Quarter lacks SEC source provenance."
                )

            evidence_records.append({
                "evidence_id": (
                    f"SEC_{metric}_"
                    f"{quarter['period_end'].replace('-', '')}"
                ),
                "metric": metric,
                "period_start": quarter["period_start"],
                "period_end": quarter["period_end"],
                "value_usd": quarter["value"],
                "normalization_method": quarter["method"],
                "sec_fact_ids": source_ids,
                "sec_sources": quarter["evidence"],
            })

    # Verify every referenced observation directly
    # against the authentic SQLite SEC source table.
    with connect() as conn:
        for record in evidence_records:
            for reference in record["sec_sources"]:
                original = conn.execute(
                    """
                    SELECT *
                    FROM sec_facts
                    WHERE id = ?
                    """,
                    (reference["sec_fact_id"],),
                ).fetchone()

                if original is None:
                    raise FinancialAgentError(
                        "SEC evidence record is missing."
                    )

                if original["company_ticker"] != ticker:
                    raise FinancialAgentError(
                        "SEC evidence company mismatch."
                    )

                for field in (
                    "concept",
                    "unit",
                    "period_start",
                    "period_end",
                    "accession",
                    "filed_at",
                    "form",
                ):
                    if original[field] != reference[field]:
                        raise FinancialAgentError(
                            "SEC evidence metadata mismatch: "
                            + field
                        )

    return evidence_records, financials


def financial_agent_node(state: FinancialState):
    ticker = state["ticker"]

    if ticker not in SUPPORTED_TICKERS:
        raise FinancialAgentError(
            "Unsupported company ticker."
        )

    review = verify_financial_review(ticker)

    evidence_records, financials = (
        build_verified_financial_evidence(ticker)
    )

    # Real Groq request with deterministic amount,
    # date, metric and evidence-ID validation.
    verified_brief = request_grounded_claims(
        ticker,
        evidence_records,
    )

    forecasts = {}
    quality = load_financial_quality(ticker)

    for metric in FINANCIAL_METRICS:
        cached = forecast_financial_metric_cached(
            ticker,
            metric,
        )

        forecast = cached["forecast"]

        latest_observed = financials[metric][-1][
            "period_end"
        ]

        if (
            forecast["last_observed_period_end"]
            != latest_observed
        ):
            raise FinancialAgentError(
                "Forecast source period is stale."
            )

        forecasts[metric] = {
            "source": "statistical_projection",
            "cache_status": cached["cache_status"],
            "last_reported_sec_period": latest_observed,
            "model": forecast["selected_model"],
            "horizon_quarters": forecast[
                "horizon_quarters"
            ],
            "predictions": forecast["forecast"],
            "backtesting": forecast["backtesting"],
            "selection_reason": forecast[
                "selection_reason"
            ],
            "interval_quality": {
                "status": quality[metric][
                    "interval_status"
                ],
                "observed_holdout_coverage": quality[
                    metric
                ]["interval_coverage"],
                "display_allowed": quality[
                    metric
                ]["interval_display_allowed"],
                "warning": quality[metric][
                    "interval_warning"
                ],
            },
        }

    disclosures = [
        "SEC observations are reported or derived "
        "quarterly financial facts; SARIMAX results "
        "are statistical estimates, not SEC facts.",
        "Every AI-selected financial claim was "
        "validated against exact source values, "
        "periods, metrics and SEC evidence IDs.",
        "Historical SEC normalization diagnostics "
        "remain documented and unresolved.",
        "Forecast prediction intervals are model-based "
        "and have not been statistically calibrated.",
        "This is a Financial Agent pilot, not a "
        "complete three-agent research report.",
    ]

    if review["nonmaterial_differences"]:
        disclosures.append(
            "Recent nonmaterial SEC reconciliation "
            "differences exist for this company."
        )

    if any(
        not quality[metric]["interval_display_allowed"]
        for metric in FINANCIAL_METRICS
    ):
        disclosures.append(
            "One or more model prediction intervals "
            "failed the configured quality display gate."
        )

    return {
        "status": "completed",
        "financial_evidence": evidence_records,
        "verified_financial_claims": verified_brief,
        "forecasts": forecasts,
        "forecast_quality": quality,
        "disclosures": disclosures,
    }


def build_financial_graph():
    graph = StateGraph(FinancialState)

    graph.add_node(
        "financial_research_agent",
        financial_agent_node,
    )

    graph.add_edge(
        START,
        "financial_research_agent",
    )

    graph.add_edge(
        "financial_research_agent",
        END,
    )

    return graph.compile()


FINANCIAL_GRAPH = build_financial_graph()


def run_financial_agent(ticker):
    if ticker not in SUPPORTED_TICKERS:
        raise FinancialAgentError(
            "Unsupported company ticker."
        )

    run_id = str(uuid.uuid4())

    result = FINANCIAL_GRAPH.invoke({
        "run_id": run_id,
        "ticker": ticker,
        "status": "running",
    })

    if result.get("status") != "completed":
        raise FinancialAgentError(
            "Financial LangGraph run did not complete."
        )

    if result.get("run_id") != run_id:
        raise FinancialAgentError(
            "Research run identifier was not preserved."
        )

    return {
        **result,
        "agent": "financial_research_agent",
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
    }
