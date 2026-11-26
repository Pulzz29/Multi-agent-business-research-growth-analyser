import time
import uuid
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from app.agents import financial_agent, news_agent, strategic_agent
from app.services import evidence_package as packages
from app.services.ai_provider import (
    AICallFailed,
    PROVIDER as AI_PROVIDER,
    check_job_continuation,
    configured_deployment,
)
from app.services.news_quality import (
    assess_news_report,
    validate_article_source,
)
from app.services.source_refresh import (
    EVIDENCE_STATE,
    SourceRefreshError,
    prepare_sources_for_research,
)
from app.services import market_registry


# Companies on the validated SEC pipeline (the original six plus every US
# universe company whose SEC filings reconcile); see sec_coverage.py.
from app.services.sec_coverage import SEC_VALIDATED as SUPPORTED_TICKERS  # noqa: E402

REPORT_SCHEMA_VERSION = 2

DEFAULT_OPTIONS = {
    "refresh": "if_stale",
    "on_refresh_failure": "continue_with_cached",
}


class ResearchWorkflowError(RuntimeError):
    pass


class ResearchState(TypedDict, total=False):
    run_id: str
    ticker: str
    status: str
    options: dict
    source_preparation: dict
    agent_statuses: dict
    agent_timings_seconds: dict
    financial_result: dict
    news_result: dict
    news_quality: dict
    news_analysis: dict
    news_analysis_diagnostics: dict
    strategic_result: dict


def source_preparation_node(state: ResearchState):
    check_job_continuation()
    """
    Lightweight orchestration step (not an agent): check freshness and
    refresh stale SEC / news evidence through the real ingestion
    pipelines before any agent reads stored evidence.
    """
    started = time.perf_counter()
    options = {**DEFAULT_OPTIONS, **(state.get("options") or {})}

    if market_registry.research_profile(state["ticker"]) == "provider":
        preparation = prepare_provider_sources(
            state["ticker"],
            mode=options["refresh"],
            on_failure=options["on_refresh_failure"],
        )
    else:
        preparation = prepare_sources_for_research(
            state["ticker"],
            mode=options["refresh"],
            on_failure=options["on_refresh_failure"],
        )
    preparation["elapsed_seconds"] = round(time.perf_counter() - started, 3)

    return {"source_preparation": preparation}


PROVIDER_EVIDENCE_STATE = {
    **EVIDENCE_STATE,
    "not_configured": "provider_not_configured_using_cached",
}


def prepare_provider_sources(ticker, mode="if_stale", on_failure="continue_with_cached"):
    """Refresh a provider company's stale data through its market providers."""
    from app.services import market_ingestion

    market = market_registry.market_of(ticker)
    before = market_ingestion.source_status(market, ticker)

    if mode == "skip":
        outcomes = {
            kind: {"source": info["source"], "status": "skipped_by_request", "provider_called": False}
            for kind, info in before.items()
        }
    else:
        outcomes = market_ingestion.refresh_company(
            market, ticker, ("profile", "quote", "statements", "news"),
            force=False, wait_seconds=300.0,
        )["results"]

    failures = [
        kind for kind, outcome in outcomes.items()
        if outcome.get("status") in ("failed", "busy", "not_configured")
    ]
    if failures and on_failure == "fail":
        raise SourceRefreshError(
            "Source refresh did not complete for: " + ", ".join(failures)
        )

    for outcome in outcomes.values():
        outcome["evidence_state"] = (
            "cached_skipped_by_request" if outcome.get("status") == "skipped_by_request"
            else PROVIDER_EVIDENCE_STATE.get(outcome.get("status"), "cached")
        )

    return {
        "profile": "provider",
        "market": market,
        "mode": mode,
        "on_failure": on_failure,
        "before": before,
        "after": market_ingestion.source_status(market, ticker),
        "outcomes": outcomes,
        "failures": failures,
    }


def financial_node(state: ResearchState):
    check_job_continuation()
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

    # Provider companies report unavailable forecasts explicitly instead.
    if result.get("research_profile") != "provider" and set(result["forecasts"]) != {
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
    check_job_continuation()
    started = time.perf_counter()

    ticker = state["ticker"]

    # Execute the News Agent with current SQLite articles.
    # No old News Agent JSON report is reused.
    try:
        result = news_agent.news_agent_node({
            "run_id": state["run_id"],
            "ticker": ticker,
            "status": "running",
        })
    except news_agent.NewsAgentError as exc:
        # No stored company-specific news with a valid source link, or the AI
        # review found none of it material: continue on financial evidence
        # only, and say so (never substitute other news).
        none_material = isinstance(exc, news_agent.NoMaterialNews)
        return {
            "news_result": {
                "status": "completed",
                "available_articles": exc.available_articles if none_material else [],
                "selected_articles": [],
                "selection_method": "ai_selected_none" if none_material else None,
                "ai_diagnostics": exc.diagnostics if none_material else None,
                "disclosures": [
                    str(exc) if none_material else
                    "No verified company-specific news is available for the selected period; "
                    "the research uses financial evidence only.",
                ],
            },
            "news_quality": _empty_news_quality(ticker),
            "news_analysis": {
                "schema_version": 1,
                "status": "unavailable",
                "status_reason": ("No company news was selected as material for analysis." if none_material
                                  else "No company news was available for analysis."),
                "items": [],
                "withheld": [],
            },
            "news_analysis_diagnostics": None,
            "agent_statuses": {
                **state.get("agent_statuses", {}),
                "news": "completed",
            },
            "agent_timings_seconds": {
                **state.get("agent_timings_seconds", {}),
                "news": round(time.perf_counter() - started, 3),
            },
        }

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
        # No article names the company as its subject: research continues on
        # financial evidence only and says so (no unrelated news is used).
        result = {
            **result,
            "disclosures": list(result.get("disclosures") or []) + [
                "No verified company-specific news is available for the selected period; "
                "the strategic analysis uses financial evidence only.",
            ],
        }

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

    for article in (
        assessed["primary_company_evidence"]
        + assessed["contextual_articles"]
    ):
        validate_article_source(
            article,
            current_by_id,
            ticker,
        )

    # Second, bounded AI request: source-grounded news
    # intelligence on the validated selection only.
    analysis, analysis_diagnostics = news_agent.write_news_analysis(
        ticker,
        assessed["primary_company_evidence"] + assessed["contextual_articles"],
    )

    return {
        "news_result": result,
        "news_quality": assessed,
        "news_analysis": analysis,
        "news_analysis_diagnostics": analysis_diagnostics,
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


def _empty_news_quality(ticker):
    return {
        "ticker": ticker,
        "status": "validated",
        "primary_company_evidence": [],
        "contextual_articles": [],
        "repeated_event_themes": {},
        "eligible_primary_count": 0,
        "contextual_count": 0,
    }


def strategic_node(state: ResearchState):
    check_job_continuation()
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

    package = strategic_agent.build_strategic_package(
        ticker,
        evidence,
        financial,
        quality["primary_company_evidence"],
        state.get("news_analysis"),
    )

    strategic_status_reason = None
    try:
        priorities, analyses, withheld, strategic_diagnostics = (
            strategic_agent.analyze_priorities(
                ticker,
                evidence,
                package,
            )
        )
    except AICallFailed as failure:
        # No priorities are invented when the AI provider fails: the
        # section is reported as unavailable with the reason.
        priorities, analyses, withheld = [], {}, []
        strategic_diagnostics = failure.diagnostics
        strategic_status_reason = (
            f"Strategic analysis was unavailable ({failure.reason}); no research "
            "priorities were generated for this run."
        )

    rendered = [
        strategic_agent.render_priority_with_analysis(
            item, analyses.get(item["evidence_id"]),
        )
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

    with_analysis = sum(1 for item in rendered if item.get("analysis"))
    analysis_status = (
        "unavailable" if not rendered
        else "completed" if with_analysis == len(rendered)
        else "partial" if with_analysis
        else "unavailable"
    )

    result = {
        "status": "completed",
        "research_priorities": rendered,
        "source_evidence": evidence,
        "analysis_status": analysis_status,
        "status_reason": strategic_status_reason,
        "withheld_analysis": withheld,
        "ai_diagnostics": strategic_diagnostics,
        "disclosures": [
            "Strategic priorities are research judgments, "
            "not proven financial risks or investment advice.",
            "An AI-assigned priority level is not a measured "
            "risk rating; potential risks and opportunities are "
            "AI interpretations of the cited evidence.",
            "AI-written strategic interpretations passed automated "
            "citation, number, period and language checks; they are "
            "not independently verified analysis.",
            "Financial claims were checked against exact "
            "SEC values, dates, metrics and evidence IDs.",
            "General industry news was excluded from "
            "company-specific strategic evidence.",
            "Related headlines may cover the same event; "
            "event independence is unverified.",
            "No causal link between news developments and "
            "financial results is established.",
            "Forecasts are statistical projections, "
            "not company-reported financial results.",
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
        "source_preparation",
        source_preparation_node,
    )

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
        START, "source_preparation"
    )

    builder.add_edge(
        "source_preparation", "financial_agent"
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


def _latest(values):
    values = [v for v in values if v]
    return max(values) if values else None


def build_provider_freshness(preparation, financial, news_quality, generated_at):
    after = preparation["after"]
    records = financial.get("financial_evidence") or []
    cited = news_quality["primary_company_evidence"] + news_quality["contextual_articles"]
    return {
        "research_profile": "provider",
        "market": preparation["market"],
        "currency": financial.get("currency"),
        "research_generated_at_utc": generated_at,
        "refresh_mode": preparation["mode"],
        "on_refresh_failure": preparation["on_failure"],
        "refresh_failures": preparation["failures"],
        "provider_sources": {
            kind: {
                "source": outcome.get("source"),
                "refresh_status": outcome.get("status"),
                "evidence_state": outcome.get("evidence_state"),
                "provider_called": bool(outcome.get("provider_called")),
                "records_inserted": outcome.get("records_inserted"),
                "message": outcome.get("message"),
                "last_successful_refresh_at": (after.get(kind) or {}).get("last_successful_refresh_at"),
                "policy_max_age_hours": (after.get(kind) or {}).get("policy_max_age_hours"),
            }
            for kind, outcome in preparation["outcomes"].items()
        },
        "statements": {
            "provider": financial.get("provider"),
            "period_types": financial.get("period_types"),
            "data_cutoff_period_end": _latest(r["period_end"] for r in records),
        },
        "news": {
            "latest_cited_publication_date": _latest(a.get("published_at") for a in cited),
            "cited_article_count": len(cited),
        },
        "forecasts": {
            **{m: {"available": True, "data_cutoff_period_end": f.get("last_reported_period")}
               for m, f in (financial.get("forecasts") or {}).items()},
            **{m: {"available": False, "reason": r}
               for m, r in (financial.get("forecasts_unavailable") or {}).items()},
        },
    }


def build_source_freshness(preparation, financial, news_quality, generated_at):
    if preparation.get("profile") == "provider":
        return build_provider_freshness(preparation, financial, news_quality, generated_at)
    after = preparation["after"]
    outcomes = preparation["outcomes"]
    records = (financial.get("evidence_package") or {}).get("records") or []
    cited_articles = (
        news_quality["primary_company_evidence"]
        + news_quality["contextual_articles"]
    )

    def outcome_view(source):
        outcome = outcomes[source]
        return {
            "refresh_status": outcome["status"],
            "evidence_state": outcome["evidence_state"],
            "provider_called": bool(outcome.get("provider_called")),
            "records_inserted": outcome.get("records_inserted"),
            "error_category": outcome.get("error_category"),
            "message": outcome.get("message"),
        }

    return {
        "research_generated_at_utc": generated_at,
        "refresh_mode": preparation["mode"],
        "on_refresh_failure": preparation["on_failure"],
        "refresh_failures": preparation["failures"],
        "sec": {
            **outcome_view("sec"),
            "policy_max_age_hours": after["sec"]["policy_max_age_hours"],
            "last_successful_refresh_at": after["sec"]["last_successful_refresh_at"],
            "latest_filed_at": after["sec"]["latest_filed_at"],
            "latest_periodic_filing": after["sec"]["latest_periodic_filing"],
            "data_cutoff_period_end": _latest(r["period_end"] for r in records),
            "note": after["sec"]["note"],
        },
        "news": {
            **outcome_view("news"),
            "policy_max_age_hours": after["news"]["policy_max_age_hours"],
            "last_successful_refresh_at": after["news"]["last_successful_refresh_at"],
            "latest_cited_publication_date": _latest(
                a.get("published_at") for a in cited_articles
            ),
            "latest_stored_publication_date": after["news"]["latest_published_at"],
            "stored_article_count": after["news"]["article_count"],
        },
        "forecasts": {
            metric: {
                "cache_status": item.get("cache_status"),
                "cache_reason": item.get("cache_reason"),
                "data_cutoff_period_end": item.get("last_reported_sec_period"),
            }
            for metric, item in (financial.get("forecasts") or {}).items()
        },
        "sec_facts_changed_during_preparation": preparation["sec_facts_changed"],
    }


CALL_SUMMARY_KEYS = (
    "provider", "deployment", "model", "status", "profile_used", "provider_requests",
    "response_id", "response_status", "incomplete_reason",
    "input_tokens", "output_tokens", "reasoning_tokens", "cached_input_tokens", "total_tokens",
    "token_usage_reported_by_provider", "estimated_input_tokens",
    "max_output_tokens", "input_token_budget", "elapsed_seconds",
    "rate_limit_retries", "fallback_reason", "failure_reason", "attempts",
    "reasoning_effort", "selection_method", "request_budget", "fallback_from",
)


def _call_summary(diagnostics, agent, call):
    if not diagnostics:
        return {"agent": agent, "call": call, "status": "not_run"}
    return {"agent": agent, "call": call,
            **{k: diagnostics.get(k) for k in CALL_SUMMARY_KEYS if k in diagnostics}}


def _served_models(calls):
    return sorted({c.get("model") for c in calls if c.get("model")})


def _providers_used(calls):
    """Providers that served this run's AI calls (Azure, or Groq as the key/config fallback)."""
    used = sorted({c.get("provider") for c in calls if c.get("provider") and c.get("model")})
    return used or [AI_PROVIDER]


def run_research_workflow(ticker, run_id=None, options=None):
    if ticker not in SUPPORTED_TICKERS and not market_registry.is_research_ticker(ticker):
        raise ResearchWorkflowError(
            "Unsupported company ticker."
        )

    options = {**DEFAULT_OPTIONS, **(options or {})}
    run_id = run_id or str(uuid.uuid4())
    started = time.perf_counter()

    result = RESEARCH_GRAPH.invoke({
        "run_id": run_id,
        "ticker": ticker,
        "status": "running",
        "options": options,
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
    news_analysis = result.get("news_analysis") or {}
    generated_at = datetime.now(timezone.utc).isoformat()
    package = financial.get("evidence_package") or {}

    financial_analysis = financial.get("financial_analysis") or {}
    provider_company = financial.get("research_profile") == "provider"
    calls = [
        _call_summary(financial.get("ai_call"), "financial", "financial_analysis"),
        _call_summary(news.get("ai_diagnostics"), "news", "news_selection"),
        _call_summary(result.get("news_analysis_diagnostics"), "news", "news_analysis"),
        _call_summary(strategic.get("ai_diagnostics"), "strategic", "strategic_analysis"),
    ]
    currency = financial.get("currency") or "USD"

    return {
        "run_id": run_id,
        "ticker": ticker,
        "status": "completed",
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "research_profile": "provider" if provider_company else "sec_validated",
        "market": market_registry.market_of(ticker),
        "currency": currency,
        "generated_at_utc": generated_at,
        "total_elapsed_seconds": round(
            time.perf_counter() - started, 3
        ),
        "agent_statuses": result[
            "agent_statuses"
        ],
        "agent_timings_seconds": result[
            "agent_timings_seconds"
        ],
        "refresh_request": {
            "refresh": options["refresh"],
            "on_refresh_failure": options["on_refresh_failure"],
        },
        "source_freshness": build_source_freshness(
            result["source_preparation"],
            financial,
            result["news_quality"],
            generated_at,
        ),
        "generation": {
            "provider": ", ".join(_providers_used(calls)),
            "providers_used": _providers_used(calls),
            "deployment": configured_deployment(),
            "model": ", ".join(_served_models(calls)) or None,
            "models_served": _served_models(calls),
            "ai_analysis_status": {
                "financial": financial_analysis.get("status"),
                "news": news_analysis.get("status"),
                "strategic": strategic.get("analysis_status"),
            },
            "calls": calls,
            "executive_summary": (
                "Generated after the report is saved, as a separate "
                "sidecar file; see the report viewer."
            ),
            "previous_reports_used_as_evidence": False,
        },
        "financial": {
            "verified_claims": financial[
                "verified_financial_claims"
            ],
            "forecasts": financial["forecasts"],
            "forecast_quality": financial[
                "forecast_quality"
            ],
            "forecast_quality_provenance": financial.get(
                "forecast_quality_provenance"
            ),
            "forecasts_unavailable": financial.get("forecasts_unavailable") or {},
            "currency": currency,
            "data_provider": financial.get("provider") or "SEC_EDGAR",
            "ai_diagnostics": financial[
                "verified_financial_claims"
            ]["diagnostics"],
            "analysis": financial_analysis,
            "evidence_package": {
                "package_version": package.get("package_version"),
                "records": [packages.public(r) for r in package.get("records") or []],
                "comparisons": [packages.public(c) for c in package.get("comparisons") or []],
                "forecasts": [packages.public(f) for f in package.get("forecasts") or []],
                "limitations": package.get("limitations") or [],
            },
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
            "ai_diagnostics": news.get("ai_diagnostics"),
            "selection_method": news.get("selection_method", "ai"),
            "analysis": news_analysis,
            "disclosures": news["disclosures"] + [
                "News intelligence text is AI-written from stored headlines "
                "and description excerpts; it attributes developments to "
                "publishers and does not independently verify them.",
            ],
        },
        "strategic": strategic,
        "limitations": [
            (
                "Financial figures are provider-reported statement values "
                f"({financial.get('provider')}, {currency}) up to the data cutoff "
                "shown in Source Freshness; they are not SEC-validated."
                if provider_company else
                "SEC figures are the latest periodic filings available at the "
                "data cutoff shown in Source Freshness, not real-time results."
            ),
            "AI-written interpretations passed automated evidence, number, "
            "period and language checks; they are not independently "
            "verified analysis and can still be incomplete.",
            "News articles are publisher reports with limited source "
            "coverage; article bodies are not independently verified and "
            "some records contain only a headline or short description.",
            "Model-based forecast intervals are not statistically "
            "calibrated; interval-quality ratings come from a stored "
            "historical review, not recomputed for this run.",
            "Research jobs are persisted in SQLite with a JSON journal; "
            "a job interrupted by a restart is marked failed, not resumed.",
            "AI requests are limited to a bounded number of concurrent "
            "Azure OpenAI calls within one backend process.",
            *([
                "Some AI text was generated by the Groq fallback because the "
                "Azure OpenAI key or deployment was unavailable; it passed the "
                "same automated checks."
            ] if any(c.get("fallback_from") for c in calls) else []),
        ],
    }
