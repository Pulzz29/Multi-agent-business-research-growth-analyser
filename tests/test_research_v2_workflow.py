"""
End-to-end research workflow (report schema v2) on an isolated runtime.

The LLM (Azure OpenAI) is replaced by a deterministic test-only fake; SEC and NewsData.io are
unreachable (network guard) or replaced by test-only runners. No real
provider request is made and the real database / reports are untouched.
"""
import json
import os

import pytest

from research_v2_support import (  # noqa: F401  (fixture import)
    completion,
    db_rows,
    default_handler,
    install_fake_ai,
    isolated_runtime,
    schema_name,
)

TICKERS = ("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA")


def run(ticker, options=None):
    from app.agents.research_workflow import run_research_workflow

    return run_research_workflow(ticker, options=options or {"refresh": "skip"})


@pytest.mark.parametrize("ticker", TICKERS)
def test_v2_report_for_every_company(isolated_runtime, monkeypatch, ticker):
    client = install_fake_ai(monkeypatch)
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
    assert fin["claims_selection"] == "ai"
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

    # Every request used Chat Completions with the model from LLM_MODEL,
    # strict structured output and the configured caps.
    from app.services.ai_provider import ai_runtime_settings
    from research_v2_support import TEST_MODEL

    settings = ai_runtime_settings()
    caps = {settings[k] for k in settings if k.startswith("max_output_")}
    assert client.chat_requests and len(client.chat_requests) == len(client.requests)
    for request in client.chat_requests:
        assert request["model"] == os.environ["LLM_MODEL"]
        assert request["response_format"]["json_schema"]["strict"] is True
        assert request["max_completion_tokens"] in caps
        assert request["temperature"] == settings["temperature"]
        assert "reasoning_effort" not in request  # LLM_REASONING_EFFORT unset

    generation = report["generation"]
    assert generation["provider"] == "azure_openai"
    assert generation["deployment"] == os.environ["LLM_MODEL"]
    assert generation["models_served"] == [TEST_MODEL]
    assert calls["financial_analysis"]["response_id"].startswith("resp_test_")
    assert calls["financial_analysis"]["reasoning_tokens"] == 300

    # JSON-serialisable exactly as research_jobs persists it.
    json.dumps(report, allow_nan=False)
    assert '"_allow"' not in json.dumps(report)
    assert not any("pilot" in text.lower() for text in report["limitations"])


def test_refresh_failure_is_labelled_and_research_continues(isolated_runtime, monkeypatch):
    install_fake_ai(monkeypatch)

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

    install_fake_ai(monkeypatch)
    with pytest.raises(SourceRefreshError):
        run("MSFT", {"refresh": "if_stale", "on_refresh_failure": "fail"})


def test_financial_provider_failure_uses_deterministic_fallback(isolated_runtime, monkeypatch):
    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_financial_analysis":
            return completion("{not json", finish_reason="stop")
        return default_handler(kwargs, n)

    install_fake_ai(monkeypatch, handler)
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
    from openai import APITimeoutError
    import httpx

    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_news_analysis":
            raise APITimeoutError(request=httpx.Request("POST", "https://insightiq-test.openai.azure.com/openai/v1/responses"))
        return default_handler(kwargs, n)

    install_fake_ai(monkeypatch, handler)
    monkeypatch.setattr("app.services.ai_provider.time.sleep", lambda s: None)
    report = run("NVDA")
    assert report["news"]["analysis"]["status"] == "unavailable"
    assert "timeout" in report["news"]["analysis"]["status_reason"]
    news_call = report["generation"]["calls"][2]
    assert news_call["provider_requests"] <= 3  # bounded: len(profiles) + 1
    assert report["news"]["approved_primary_articles"]


def test_withheld_strategic_analysis_falls_back_to_template(isolated_runtime, monkeypatch):
    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_strategic_analysis":
            body = json.loads(default_handler(kwargs, n).output_text)
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

    install_fake_ai(monkeypatch, handler)
    report = run("META")
    strategic = report["strategic"]
    assert strategic["analysis_status"] == "partial"
    withheld = strategic["withheld_analysis"]
    assert withheld and "causal" in withheld[0]["reason"]
    first = strategic["research_priorities"][0]
    assert first["analysis"] is None
    assert first["monitoring_question"]  # template fallback still present
    assert first["monitoring_question_source"] == "template"


def test_strategic_provider_failure_marks_section_unavailable(isolated_runtime, monkeypatch):
    """No priorities are invented when the strategic AI call fails."""
    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_strategic_analysis":
            return completion("", finish_reason="length")
        return default_handler(kwargs, n)

    install_fake_ai(monkeypatch, handler)
    report = run("GOOGL")
    strategic = report["strategic"]
    assert strategic["research_priorities"] == []
    assert strategic["analysis_status"] == "unavailable"
    assert "output_token_limit" in strategic["status_reason"]
    calls = {c["call"]: c for c in report["generation"]["calls"]}
    assert calls["strategic_analysis"]["status"] == "failed"
    assert calls["strategic_analysis"]["failure_reason"] == "output_token_limit"


def test_failing_workflow_fails_job_cleanly(isolated_runtime, monkeypatch):
    """A job whose workflow raises is marked failed; it is never left 'running'."""
    from app.services import research_jobs

    monkeypatch.setenv("INSIGHTIQ_AUTO_EXECUTIVE_SUMMARY", "0")
    root = isolated_runtime.root
    research_jobs.initialize_job_storage()
    job = research_jobs.create_job(root, "GOOGL")

    def failing_runner(ticker, run_id):
        raise RuntimeError("simulated workflow failure")

    with pytest.raises(Exception):
        research_jobs.execute_queued_research_job(root, job["run_id"], failing_runner)

    stored = research_jobs.read_job(job["run_id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "research_execution_failed"
    from research_v2_support import get_doc

    assert get_doc(root / "reports" / "research_runs" / (job["run_id"] + ".json")) is None


def test_completed_job_persists_new_immutable_report(isolated_runtime, monkeypatch):
    from app.services import research_jobs, report_history
    from app.agents.research_workflow import run_research_workflow

    install_fake_ai(monkeypatch)
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
    from research_v2_support import get_doc

    docs = [json.loads(get_doc(p)) for p in paths]
    assert docs[0]["run_id"] != docs[1]["run_id"]
    assert all(research_jobs.read_job(r)["status"] == "completed" for r in run_ids)

    # Reading a saved report makes no provider call and changes nothing.
    before = get_doc(paths[0])

    def forbidden(*a, **k):
        raise AssertionError("provider call during read")

    monkeypatch.setattr("app.services.ai_provider.default_client", forbidden)
    monkeypatch.setattr("app.services.sec_ingestion.refresh_sec_company", forbidden)
    monkeypatch.setattr("app.services.news_ingestion.refresh_news_company", forbidden)
    document = report_history.read_report("AMZN", run_ids[0])
    assert document["report"]["run_id"] == run_ids[0]
    assert document["report"]["report_schema_version"] == 2
    assert get_doc(paths[0]) == before


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

    install_fake_ai(monkeypatch)
    report = run("MSFT")
    from app.services import ai_provider

    context = es.build_context(report, include_descriptions=True, include_ai_findings=True)
    finding = context["validated_financial_findings"][0]
    assert set(finding["evidence_ids"]) <= es.allowed_evidence_ids(context)
    # The full context level fits the configured Azure input budget.
    instructions, items = ai_provider.split_messages(es.build_messages(context))
    estimate = ai_provider.estimate_request_tokens(
        instructions, items, ai_provider.text_format("insightiq_executive_summary", es.ExecutiveSummary))
    assert estimate <= ai_provider.ai_runtime_settings()["input_token_budget"]


def test_ai_selecting_no_material_news_completes_on_financial_evidence(isolated_runtime, monkeypatch):
    # The AI may judge that none of the verified candidates is material. The run
    # must then use financial evidence only, say why, and keep the call record
    # (response ID and billed tokens) - never substitute a deterministic pick.
    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_news_selections":
            return completion(json.dumps({"selected_articles": []}), response_id="resp_none")
        return default_handler(kwargs, n)

    install_fake_ai(monkeypatch, handler)
    report = run("NVDA")
    news = report["news"]
    assert news["approved_primary_articles"] == [] and news["selected_articles"] == []
    assert news["analysis"]["status"] == "unavailable"
    assert "selected as material" in news["analysis"]["status_reason"]
    assert any("found none of the" in d for d in news["disclosures"])
    call = [c for c in report["generation"]["calls"] if c["call"] == "news_selection"][0]
    assert call["status"] == "completed" and call["response_id"] == "resp_none"
    assert call["input_tokens"] is not None
    assert report["agent_statuses"]["news"] == "completed"
