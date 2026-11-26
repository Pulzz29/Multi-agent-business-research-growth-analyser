"""
Phase B: AI research for provider-sourced companies (offline, isolated DB).

Provider data comes from test-only fakes (tests/market_fixtures.py); Groq is
the deterministic test fake. No network is used; the six SEC-validated
companies' pipeline is covered by the existing tests.
"""
import json
import os

import pytest

from market_fixtures import india_clients, us_clients
from research_v2_support import (  # noqa: F401
    db_rows,
    install_fake_ai,
    isolated_runtime,
)


def seed(market, symbol, clients):
    from app.services.market_ingestion import refresh_company

    return refresh_company(market, symbol, ("profile", "quote", "statements", "news"), clients=clients)


def run(symbol):
    from app.agents.research_workflow import run_research_workflow

    return run_research_workflow(symbol, options={"refresh": "skip"})


# ------------------------------------------------------------- registry

def test_research_profiles(isolated_runtime):
    from app.services import market_registry

    assert market_registry.research_profile("MSFT") == "sec_validated"
    assert market_registry.research_profile("ORCL") is None  # not loaded yet
    seed("US", "ORCL", us_clients())
    assert market_registry.research_profile("ORCL") == "provider"
    assert market_registry.research_profile("<bad>") is None
    assert market_registry.evidence_safe("M&M.NS") == "M_M_NS"


# ------------------------------------------------------- US (annual FMP)

def test_us_provider_research_end_to_end(isolated_runtime, monkeypatch):
    seed("US", "ORCL", us_clients())
    install_fake_ai(monkeypatch)
    report = run("ORCL")

    assert report["research_profile"] == "provider" and report["currency"] == "USD"
    assert report["market"] == "US"
    assert report["agent_statuses"] == {"financial": "completed", "news": "completed", "strategic": "completed"}

    claims = report["financial"]["verified_claims"]["verified_claims"]
    assert claims and all(c["evidence_id"].startswith("FMP_") for c in claims)
    assert {c["metric"] for c in claims} == {"revenue", "operating_cash_flow"}
    assert all(c["period_type"] == "annual" for c in claims)  # quarterly refused by the fake plan
    assert all("USD" in s for s in report["financial"]["verified_claims"]["verified_statements"])

    analysis = report["financial"]["analysis"]
    assert analysis["status"] == "completed", analysis["withheld"]
    assert "as reported by the data provider" in analysis["findings"][0]["verified_observation"]

    unavailable = report["financial"]["forecasts_unavailable"]
    assert set(unavailable) == {"revenue", "operating_cash_flow"}
    assert "annual" in unavailable["revenue"]
    assert report["financial"]["forecasts"] == {}

    fresh = report["source_freshness"]
    assert fresh["research_profile"] == "provider"
    assert fresh["statements"]["provider"] == "FMP"
    assert fresh["provider_sources"]["statements"]["refresh_status"] == "skipped_by_request"

    priorities = report["strategic"]["research_priorities"]
    assert priorities and all(p["analysis"] for p in priorities)
    financial_priority = next(p for p in priorities if p["source_metadata"]["kind"] == "verified_sec_financial_fact")
    assert financial_priority["source_type"] == "Financial statement evidence (FMP)"
    assert any("not SEC-validated" in text for text in report["limitations"])
    json.dumps(report, allow_nan=False)


# ------------------------------------------------- India (quarterly yfinance)

def test_india_provider_research_without_news(isolated_runtime, monkeypatch):
    clients = india_clients(quarters=6)
    outcome = seed("IN", "RELIANCE.NS", clients)
    # NewsData.io is unreachable offline: the failure is reported, not hidden.
    assert outcome["results"]["news"]["status"] == "failed"
    install_fake_ai(monkeypatch)
    report = run("RELIANCE.NS")

    assert report["currency"] == "INR" and report["market"] == "IN"
    claims = report["financial"]["verified_claims"]["verified_claims"]
    by_metric = {c["metric"]: c["period_type"] for c in claims}
    assert by_metric == {"revenue": "quarterly", "operating_cash_flow": "annual"}
    assert all(c["evidence_id"].startswith("YF_") for c in claims)
    assert all("INR" in s for s in report["financial"]["verified_claims"]["verified_statements"])
    assert report["financial"]["analysis"]["findings"], report["financial"]["analysis"]["withheld"]

    news = report["news"]
    assert news["selected_articles"] == [] and news["analysis"]["status"] == "unavailable"
    assert any("financial evidence only" in d for d in news["disclosures"])
    kinds = {p["source_metadata"]["kind"] for p in report["strategic"]["research_priorities"]}
    assert kinds == {"verified_sec_financial_fact"}

    # Quarterly comparisons computed by Python from adjacent / year-apart quarters.
    comparisons = {c["comparison_id"]: c for c in report["financial"]["evidence_package"]["comparisons"]}
    yoy = comparisons["CMP_revenue_20250630_yoy"]
    assert yoy["basis_period_end"] == "2024-06-30" and yoy["currency"] == "INR"
    assert "Only 6 consecutive quarters" in report["financial"]["forecasts_unavailable"]["revenue"]


def test_india_news_attribution_uses_registry_names(isolated_runtime):
    from app.services.news_quality import explicit_company_mention

    seed("IN", "TCS.NS", india_clients())
    assert explicit_company_mention("Tata Consultancy Services wins a large deal", "TCS.NS")
    assert explicit_company_mention("TCS shares rise after results", "TCS.NS")
    assert not explicit_company_mention("Indian IT stocks rally", "TCS.NS")


def test_provider_forecast_is_unavailable_without_point_in_time_validation(isolated_runtime):
    """Provider statements carry no filing dates, so a point-in-time backtest
    cannot be built: no forecast is fitted, and the reason says why."""
    from app.agents.provider_financial import provider_forecast

    seed("IN", "ONGC.NS", india_clients(quarters=24))
    forecast, reason = provider_forecast("ONGC.NS", "revenue")
    assert forecast is None
    assert "28" in reason or "filing dates" in reason


# ---------------------------------------------------------- validation

def test_claim_validation_is_exact(isolated_runtime):
    from types import SimpleNamespace

    from app.agents.provider_financial import select_records, validate_claims
    from app.services.ai_provider import ResponseRejected

    seed("US", "ORCL", us_clients())
    records, choice, currency = select_records("ORCL")
    assert currency == "USD" and choice == {"revenue": "annual", "operating_cash_flow": "annual"}
    good = [SimpleNamespace(**{k: r[k] for k in ("evidence_id", "metric", "period_start", "period_end", "value_usd")})
            for r in records if r["period_end"].startswith("2025")]
    assert len(validate_claims(SimpleNamespace(claims=good), records)) == 2

    wrong_amount = [SimpleNamespace(**{**vars(good[0]), "value_usd": "1"}), good[1]]
    with pytest.raises(ResponseRejected, match="amount"):
        validate_claims(SimpleNamespace(claims=wrong_amount), records)
    unknown = [SimpleNamespace(**{**vars(good[0]), "evidence_id": "FMP_revenue_FY19990101"}), good[1]]
    with pytest.raises(ResponseRejected, match="Unknown"):
        validate_claims(SimpleNamespace(claims=unknown), records)
    one_metric = [g for g in good if g.metric == "revenue"] * 2
    with pytest.raises(ResponseRejected):
        validate_claims(SimpleNamespace(claims=one_metric), records)


def test_tampered_provider_row_is_detected(isolated_runtime):
    from research_v2_support import open_db

    from app.agents.provider_financial import ProviderFinancialError, select_records

    seed("US", "ORCL", us_clients())
    records, _, _ = select_records("ORCL")
    con = open_db(isolated_runtime.db)
    con.execute("UPDATE market_statements SET metric='net_income' WHERE id=?",
                (records[0]["provider_fact_ids"][0],))
    con.commit()
    con.close()
    with pytest.raises(ProviderFinancialError):
        select_records("ORCL")


def test_company_without_statements_is_refused(isolated_runtime, monkeypatch):
    from app.agents.provider_financial import ProviderFinancialError
    from app.services.market_ingestion import refresh_company

    refresh_company("US", "ADBE", ("profile",), clients=us_clients())
    install_fake_ai(monkeypatch)
    with pytest.raises(ProviderFinancialError, match="No stored revenue"):
        run("ADBE")


# ---------------------------------------------------- jobs, history, API

def test_job_report_history_and_api(isolated_runtime, monkeypatch):
    from fastapi.testclient import TestClient

    from app.agents.research_workflow import run_research_workflow
    from app.main import app
    from app.services import research_jobs

    seed("US", "ORCL", us_clients())
    install_fake_ai(monkeypatch)
    monkeypatch.setenv("INSIGHTIQ_AUTO_EXECUTIVE_SUMMARY", "0")
    monkeypatch.setattr("app.services.research_backup.backup_completed_research_job",
                        lambda root, run_id: {"backup_status": "verified"})
    root = isolated_runtime.root
    research_jobs.initialize_job_storage()
    job = research_jobs.create_job(root, "ORCL")
    research_jobs.execute_queued_research_job(
        root, job["run_id"],
        lambda t, run_id: run_research_workflow(t, run_id=run_id, options={"refresh": "skip"}),
    )
    assert research_jobs.read_job(job["run_id"])["status"] == "completed"

    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as client:
        runs = client.get("/api/v1/research/runs?ticker=ORCL", headers=headers)
        assert runs.status_code == 200, runs.text
        assert runs.json()["runs"][0]["run_id"] == job["run_id"]
        report = client.get(f"/api/v1/companies/ORCL/reports/{job['run_id']}", headers=headers)
        assert report.status_code == 200
        assert report.json()["report"]["research_profile"] == "provider"
        # Unknown or unloaded companies cannot start research.
        assert client.post("/api/v1/jobs", json={"ticker": "ZZZZ"}, headers=headers).status_code == 422
        assert client.post("/api/v1/jobs", json={"ticker": "<bad>"}, headers=headers).status_code == 422
        # The overview still loads with a provider-company job in history.
        assert client.get("/api/v1/dashboard/overview", headers=headers).status_code == 200


def test_executive_summary_context_uses_report_currency(isolated_runtime, monkeypatch):
    from app.services import executive_summary as es

    seed("IN", "RELIANCE.NS", india_clients())
    install_fake_ai(monkeypatch)
    report = run("RELIANCE.NS")
    context = es.build_context(report)
    messages = es.build_messages(context)
    assert context["currency"] == "INR"
    assert context["data_source"] == "provider-reported financial statements"
    observation = context["sec_financial_observations"][0]
    assert observation["computed_comparisons"] is None or observation["computed_comparisons"]
    assert "INR" in messages[0]["content"]
