"""
End-to-end research workflow (report schema v2) on an isolated runtime.

Groq is replaced by a deterministic test-only fake; SEC and NewsData.io are
unreachable (network guard) or replaced by test-only runners. No real
provider request is made and the real database / reports are untouched.
"""
import json

import pytest

from research_v2_support import (  # noqa: F401  (fixture import)
    completion,
    db_rows,
    default_handler,
    install_fake_groq,
    isolated_runtime,
    schema_name,
)

TICKERS = ("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA")


def run(ticker, options=None):
    from app.agents.research_workflow import run_research_workflow

    return run_research_workflow(ticker, options=options or {"refresh": "skip"})


@pytest.mark.parametrize("ticker", TICKERS)
def test_v2_report_for_every_company(isolated_runtime, monkeypatch, ticker):
    client = install_fake_groq(monkeypatch)
    report = run(ticker)

    assert report["status"] == "completed"
    assert report["report_schema_version"] == 2
    assert report["agent_statuses"] == {
        "financial": "completed", "news": "completed", "strategic": "completed",
    }
    # Backward-compatible keys still present.
    assert {"verified_claims", "forecasts", "forecast_quality"} <= set(report["financial"])
    assert {"selected_articles", "approved_primary_articles"} <= set(report["news"])
    assert {"research_priorities", "source_evidence"} <= set(report["strategic"])

    fin = report["financial"]["analysis"]
    assert fin["claims_selection"] == "groq"
    assert fin["findings"], fin["withheld"]
    for finding in fin["findings"]:
        assert finding["validation"]["status"] == "automated_checks_passed"
        assert finding["validation"]["independently_verified"] is False
        assert finding["verified_observation"]

    news = report["news"]["analysis"]
    assert news["status"] in ("completed", "partial"), news
    for item in news["items"]:
        for source in item["sources"]:
            assert source["source_url"].startswith("https://")

    priorities = report["strategic"]["research_priorities"]
    assert priorities and all(p["analysis"] for p in priorities)
    assert report["strategic"]["analysis_status"] == "completed"

    calls = {c["call"]: c for c in report["generation"]["calls"]}
    assert calls["financial_analysis"]["status"] == "completed"
    assert calls["financial_analysis"]["total_tokens"] == 2100  # fake usage
    assert calls["strategic_analysis"]["status"] == "completed"
    assert report["generation"]["previous_reports_used_as_evidence"] is False

    freshness = report["source_freshness"]
    assert freshness["sec"]["refresh_status"] == "skipped_by_request"
    assert freshness["sec"]["data_cutoff_period_end"]
    assert freshness["news"]["latest_cited_publication_date"]

    # Every request respected the configured budget.
    for request in client.requests:
        assert request["max_completion_tokens"] <= 2800

    # JSON-serialisable exactly as research_jobs persists it.
    json.dumps(report, allow_nan=False)
    assert '"_allow"' not in json.dumps(report)
    assert not any("pilot" in text.lower() for text in report["limitations"])


def test_refresh_failure_is_labelled_and_research_continues(isolated_runtime, monkeypatch):
    install_fake_groq(monkeypatch)

    # Network is blocked by the test guard, so any real refresh must fail.
    report = run("MSFT", {"refresh": "if_stale", "on_refresh_failure": "continue_with_cached"})
    fresh = report["source_freshness"]

    news = fresh["news"]
    # MSFT news (6h policy) is stale in the stored snapshot.
    assert news["refresh_status"] == "failed"
    assert news["evidence_state"] == "refresh_failed_using_cached"
    assert "NEWSDATA" in fresh["refresh_failures"]

    failed = db_rows(
        isolated_runtime.db,
        "SELECT status FROM source_refreshes WHERE company_ticker='MSFT' "
        "AND source='NEWSDATA' ORDER BY id DESC LIMIT 1",
    )
    assert failed[0]["status"] == "failed"


def test_refresh_failure_with_fail_policy_stops(isolated_runtime, monkeypatch):
    from app.services.source_refresh import SourceRefreshError

    install_fake_groq(monkeypatch)
    with pytest.raises(SourceRefreshError):
        run("MSFT", {"refresh": "if_stale", "on_refresh_failure": "fail"})


def test_financial_provider_failure_uses_deterministic_fallback(isolated_runtime, monkeypatch):
    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_financial_analysis":
            return completion("{not json", finish_reason="stop")
        return default_handler(kwargs, n)

    install_fake_groq(monkeypatch, handler)
    report = run("AAPL")
    fin = report["financial"]["analysis"]
    assert fin["claims_selection"] == "deterministic_fallback"
    assert fin["status"] == "unavailable" and fin["findings"] == []
    assert "unavailable" in fin["status_reason"]
    claims = report["financial"]["verified_claims"]["verified_claims"]
    assert {c["metric"] for c in claims} == {"revenue", "operating_cash_flow"}
    assert report["generation"]["calls"][0]["status"] == "failed"
    # Bounded: invalid JSON -> reduced profile -> stop (2 requests).
    assert report["generation"]["calls"][0]["provider_requests"] == 2, report["generation"]["calls"][0]["attempts"]


def test_news_writing_failure_keeps_selection(isolated_runtime, monkeypatch):
    from groq import APITimeoutError
    import httpx

    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_news_analysis":
            raise APITimeoutError(request=httpx.Request("POST", "https://api.groq.com"))
        return default_handler(kwargs, n)

    install_fake_groq(monkeypatch, handler)
    monkeypatch.setattr("app.services.groq_structured.time.sleep", lambda s: None)
    report = run("NVDA")
    assert report["news"]["analysis"]["status"] == "unavailable"
    assert "timeout" in report["news"]["analysis"]["status_reason"]
    news_call = report["generation"]["calls"][2]
    assert news_call["provider_requests"] <= 3  # bounded: len(profiles) + 1
    assert report["news"]["approved_primary_articles"]


def test_withheld_strategic_analysis_falls_back_to_template(isolated_runtime, monkeypatch):
    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_strategic_analysis":
            body = json.loads(default_handler(kwargs, n).choices[0].message.content)
            body["priorities"][0]["interpretation"] = (
                "Strong revenue was driven by the new product launch, which caused "
                "the company to outperform and will increase margins next year. "
                "This is a confirmed risk for competitors and a clear signal for investors. "
                "The company is now positioned to keep growing across every product line, "
                "and management execution appears to be the main reason for the result. "
                "Readers should expect the same pattern to continue in coming quarters."
            )
            return completion(json.dumps(body))
        return default_handler(kwargs, n)

    install_fake_groq(monkeypatch, handler)
    report = run("META")
    strategic = report["strategic"]
    assert strategic["analysis_status"] == "partial"
    withheld = strategic["withheld_analysis"]
    assert withheld and "causal" in withheld[0]["reason"]
    first = strategic["research_priorities"][0]
    assert first["analysis"] is None
    assert first["monitoring_question"]  # template fallback still present
    assert first["monitoring_question_source"] == "template"


def test_strategic_provider_failure_fails_job_cleanly(isolated_runtime, monkeypatch):
    """A failed strategic call fails the job; it is never left 'running'."""
    from app.services import research_jobs
    from app.agents.research_workflow import run_research_workflow

    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_strategic_analysis":
            return completion("", finish_reason="length")
        return default_handler(kwargs, n)

    install_fake_groq(monkeypatch, handler)
    monkeypatch.setenv("INSIGHTIQ_AUTO_EXECUTIVE_SUMMARY", "0")
    root = isolated_runtime.root
    research_jobs.initialize_job_storage()
    job = research_jobs.create_job(root, "GOOGL")

    with pytest.raises(Exception):
        research_jobs.execute_queued_research_job(
            root, job["run_id"],
            lambda t, run_id: run_research_workflow(t, run_id=run_id, options={"refresh": "skip"}),
        )

    stored = research_jobs.read_job(job["run_id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "research_execution_failed"
    assert not (root / "reports" / "research_runs" / (job["run_id"] + ".json")).exists()


def test_completed_job_persists_new_immutable_report(isolated_runtime, monkeypatch):
    from app.services import research_jobs, report_history
    from app.agents.research_workflow import run_research_workflow

    install_fake_groq(monkeypatch)
    monkeypatch.setenv("INSIGHTIQ_AUTO_EXECUTIVE_SUMMARY", "0")
    monkeypatch.setattr(
        "app.services.research_backup.backup_completed_research_job",
        lambda root, run_id: {"backup_status": "verified"},
    )
    root = isolated_runtime.root
    research_jobs.initialize_job_storage()

    run_ids = []
    for _ in range(2):
        job = research_jobs.create_job(root, "AMZN")
        research_jobs.execute_queued_research_job(
            root, job["run_id"],
            lambda t, run_id: run_research_workflow(t, run_id=run_id, options={"refresh": "skip"}),
        )
        run_ids.append(job["run_id"])

    assert run_ids[0] != run_ids[1]
    paths = [root / "reports" / "research_runs" / f"{r}.json" for r in run_ids]
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    assert docs[0]["run_id"] != docs[1]["run_id"]
    assert all(research_jobs.read_job(r)["status"] == "completed" for r in run_ids)

    # Reading a saved report makes no provider call and changes nothing.
    before = paths[0].read_bytes()

    def forbidden(*a, **k):
        raise AssertionError("provider call during read")

    monkeypatch.setattr("app.services.groq_structured.default_client", forbidden)
    monkeypatch.setattr("app.agents.news_agent.Groq", forbidden)
    monkeypatch.setattr("app.services.sec_ingestion.refresh_sec_company", forbidden)
    monkeypatch.setattr("app.services.news_ingestion.refresh_news_company", forbidden)
    document = report_history.read_report("AMZN", run_ids[0])
    assert document["report"]["run_id"] == run_ids[0]
    assert document["report"]["report_schema_version"] == 2
    assert paths[0].read_bytes() == before


# ------------------------------------------------- report compatibility

from pathlib import Path as _Path

_SAVED = sorted((_Path(__file__).resolve().parents[1] / "reports" / "research_runs").glob("*.json"))


@pytest.mark.parametrize("report_file", _SAVED, ids=lambda p: p.stem[:8])
def test_existing_v1_reports_remain_readable(report_file):
    """Historical reports (no v2 fields) still build summary context; read-only."""
    from app.services import executive_summary as es

    raw = report_file.read_bytes()
    report = json.loads(raw.decode("utf-8"))
    assert "report_schema_version" not in report or report["report_schema_version"] == 2
    context = es.build_context(report)
    assert context["sec_financial_observations"]
    assert "validated_financial_findings" not in context or report.get("report_schema_version") == 2
    assert report_file.read_bytes() == raw


def test_v2_report_feeds_executive_summary_context(isolated_runtime, monkeypatch):
    from app.services import executive_summary as es

    install_fake_groq(monkeypatch)
    report = run("MSFT")
    context, messages, budget = es.fit_to_budget(report)
    assert budget["validated_ai_findings_included"] is True
    finding = context["validated_financial_findings"][0]
    assert set(finding["evidence_ids"]) <= es.allowed_evidence_ids(context)
    assert budget["estimated_input_tokens"] + es.MAX_COMPLETION_TOKENS <= es.REQUEST_TOKEN_BUDGET
