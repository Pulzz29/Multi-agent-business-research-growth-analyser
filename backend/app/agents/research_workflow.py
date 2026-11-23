import time
import uuid
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from app.agents import financial_agent, news_agent, strategic_agent
from app.services.news_quality import (
    assess_news_report,
    validate_article_source,
)


SUPPORTED_TICKERS = frozenset({
    "AAPL", "AMZN", "GOOGL",
    "META", "MSFT", "NVDA",
})


class ResearchWorkflowError(RuntimeError):
    pass


class ResearchState(TypedDict, total=False):
    run_id: str
    ticker: str
    status: str
    agent_statuses: dict
    agent_timings_seconds: dict
    financial_result: dict
    news_result: dict
    news_quality: dict
    strategic_result: dict


def financial_node(state: ResearchState):
    started = time.perf_counter()

    ticker = state["ticker"]

    # Run the existing Financial Agent logic once.
    result = financial_agent.financial_agent_node({
        "run_id": state["run_id"],
        "ticker": ticker,
        "status": "running",
    })

    if result.get("status") != "completed":
        raise ResearchWorkflowError(
            "Financial Agent did not complete."
        )

    claims = result["verified_financial_claims"]

    if not claims.get("verified_claims"):
        raise ResearchWorkflowError(
            "Financial Agent returned no verified claims."
        )

    if set(result["forecasts"]) != {
        "revenue", "operating_cash_flow"
    }:
        raise ResearchWorkflowError(
            "Financial forecasts are incomplete."
        )

    return {
        "financial_result": result,
        "agent_statuses": {
            **state.get("agent_statuses", {}),
            "financial": "completed",
        },
        "agent_timings_seconds": {
            **state.get("agent_timings_seconds", {}),
            "financial": round(
                time.perf_counter() - started, 3
            ),
        },
    }


def news_node(state: ResearchState):
    started = time.perf_counter()

    ticker = state["ticker"]

    # Execute the News Agent with current SQLite articles.
    # No old News Agent JSON report is reused.
    result = news_agent.news_agent_node({
        "run_id": state["run_id"],
        "ticker": ticker,
        "status": "running",
    })

    if result.get("status") != "completed":
        raise ResearchWorkflowError(
            "News Agent did not complete."
        )

    news_report = {
        **result,
        "run_id": state["run_id"],
        "ticker": ticker,
    }

    assessed = assess_news_report(
        news_report,
        ticker,
    )

    if assessed["eligible_primary_count"] == 0:
        raise ResearchWorkflowError(
            "No directly attributed company news is "
            "available for strategic research."
        )

    # Verify that approved headlines, dates, URLs and
    # publisher fields agree with live stored articles.
    current_articles, _ = news_agent.load_news_evidence(
        ticker,
        max_articles=100,
    )

    current_by_id = {
        item["evidence_id"]: item
        for item in current_articles
    }

    for article in assessed["primary_company_evidence"]:
        validate_article_source(
            article,
            current_by_id,
            ticker,
        )

    return {
        "news_result": result,
        "news_quality": assessed,
        "agent_statuses": {
            **state.get("agent_statuses", {}),
            "news": "completed",
        },
        "agent_timings_seconds": {
            **state.get("agent_timings_seconds", {}),
            "news": round(
                time.perf_counter() - started, 3
            ),
        },
    }


def strategic_node(state: ResearchState):
    started = time.perf_counter()

    ticker = state["ticker"]

    if (
        state["agent_statuses"].get("financial") != "completed"
        or state["agent_statuses"].get("news") != "completed"
    ):
        raise ResearchWorkflowError(
            "Strategic Agent dependencies are incomplete."
        )

    financial = state["financial_result"]
    quality = state["news_quality"]

    # Use current upstream results from THIS graph run,
    # not a second Financial Agent execution.
    prepared_news = {
        "news_agent_run_id": state["run_id"],
        "primary_articles": quality[
            "primary_company_evidence"
        ],
        "contextual_article_count": quality[
            "contextual_count"
        ],
        "repeated_event_themes": quality[
            "repeated_event_themes"
        ],
    }

    evidence = strategic_agent.build_strategy_evidence(
        ticker,
        financial,
        prepared_news,
    )

    priorities, groq_diagnostics = (
        strategic_agent.select_priorities_with_groq(
            ticker,
            evidence,
        )
    )

    rendered = [
        strategic_agent.render_priority(item)
        for item in priorities
    ]

    # Every output must refer to an available input source.
    allowed_ids = {
        item["evidence_id"]
        for item in evidence
    }

    for item in rendered:
        if item["evidence_id"] not in allowed_ids:
            raise ResearchWorkflowError(
                "Strategic output contains an unknown source."
            )

        if (
            item["financial_impact_claimed"]
            or item["causal_relationship_claimed"]
        ):
            raise ResearchWorkflowError(
                "Unsupported strategic causation was introduced."
            )

    result = {
        "status": "completed",
        "research_priorities": rendered,
        "source_evidence": evidence,
        "groq_diagnostics": groq_diagnostics,
        "disclosures": [
            "Strategic priorities are research judgments, "
            "not proven financial risks or investment advice.",
            "Financial claims were checked against exact "
            "SEC values, dates, metrics and evidence IDs.",
            "General industry news was excluded from "
            "company-specific strategic evidence.",
            "Related headlines may cover the same event; "
            "event independence is unverified.",
            "Forecasts are statistical projections, "
            "not company-reported financial results.",
            "Prediction intervals are not empirically "
            "calibrated; quality warnings must be displayed.",
            "Historical financial and news coverage "
            "limitations remain documented.",
        ],
    }

    return {
        "strategic_result": result,
        "agent_statuses": {
            **state["agent_statuses"],
            "strategic": "completed",
        },
        "agent_timings_seconds": {
            **state["agent_timings_seconds"],
            "strategic": round(
                time.perf_counter() - started, 3
            ),
        },
        "status": "completed",
    }


def build_research_graph():
    builder = StateGraph(ResearchState)

    builder.add_node(
        "financial_agent",
        financial_node,
    )

    builder.add_node(
        "news_agent",
        news_node,
    )

    builder.add_node(
        "strategic_agent",
        strategic_node,
    )

    builder.add_edge(
        START, "financial_agent"
    )

    builder.add_edge(
        "financial_agent", "news_agent"
    )

    builder.add_edge(
        "news_agent", "strategic_agent"
    )

    builder.add_edge(
        "strategic_agent", END
    )

    return builder.compile()


RESEARCH_GRAPH = build_research_graph()


def run_research_workflow(ticker, run_id=None):
    if ticker not in SUPPORTED_TICKERS:
        raise ResearchWorkflowError(
            "Unsupported company ticker."
        )

    run_id = run_id or str(uuid.uuid4())
    started = time.perf_counter()

    result = RESEARCH_GRAPH.invoke({
        "run_id": run_id,
        "ticker": ticker,
        "status": "running",
        "agent_statuses": {
            "financial": "pending",
            "news": "pending",
            "strategic": "pending",
        },
        "agent_timings_seconds": {},
    })

    if result.get("run_id") != run_id:
        raise ResearchWorkflowError(
            "Research run ID was not preserved."
        )

    if result.get("status") != "completed":
        raise ResearchWorkflowError(
            "Research workflow did not complete."
        )

    expected = {
        "financial": "completed",
        "news": "completed",
        "strategic": "completed",
    }

    if result.get("agent_statuses") != expected:
        raise ResearchWorkflowError(
            "Research agent statuses are inconsistent."
        )

    financial = result["financial_result"]
    news = result["news_result"]
    strategic = result["strategic_result"]

    return {
        "run_id": run_id,
        "ticker": ticker,
        "status": "completed",
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "total_elapsed_seconds": round(
            time.perf_counter() - started, 3
        ),
        "agent_statuses": result[
            "agent_statuses"
        ],
        "agent_timings_seconds": result[
            "agent_timings_seconds"
        ],
        "financial": {
            "verified_claims": financial[
                "verified_financial_claims"
            ],
            "forecasts": financial["forecasts"],
            "forecast_quality": financial[
                "forecast_quality"
            ],
            "groq_diagnostics": financial[
                "verified_financial_claims"
            ]["diagnostics"],
            "disclosures": financial["disclosures"],
        },
        "news": {
            "selected_articles": news[
                "selected_articles"
            ],
            "approved_primary_articles": result[
                "news_quality"
            ]["primary_company_evidence"],
            "contextual_articles": result[
                "news_quality"
            ]["contextual_articles"],
            "repeated_event_themes": result[
                "news_quality"
            ]["repeated_event_themes"],
            "groq_diagnostics": news[
                "groq_diagnostics"
            ],
            "disclosures": news["disclosures"],
        },
        "strategic": strategic,
        "limitations": [
            "This is a validated integration pilot, "
            "not a final production research report.",
            "The research run is not yet stored as a "
            "recoverable background job in SQLite.",
            "Provider rate limiting and cross-process "
            "coordination remain future integration tasks.",
            "News articles have limited source coverage "
            "and article-body verification.",
            "Model-based forecast intervals remain "
            "subject to quality restrictions.",
        ],
    }
