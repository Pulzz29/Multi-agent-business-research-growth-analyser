"""
Company-specific research: cross-company contamination checks, job
cancellation and deadlines, and evidence scoping. Isolated runtime, fake
Azure client, no network.
"""
import json

import pytest

from research_v2_support import (  # noqa: F401  (fixture import)
    completion,
    default_handler,
    install_fake_ai,
    isolated_runtime,
    schema_name,
)


def run(ticker):
    from app.agents.research_workflow import run_research_workflow

    return run_research_workflow(ticker, options={"refresh": "skip"})


# ------------------------------------------------------- contamination

def test_validator_rejects_other_company_not_in_evidence():
    from app.services.narrative_validation import NarrativeRejected, check_company_scope, company_scope

    with company_scope("MSFT", "Microsoft quarterly revenue USD 77.7 billion"):
        with pytest.raises(NarrativeRejected, match="AAPL"):
            check_company_scope("Microsoft revenue grew faster than Apple Inc. revenue.", "finding")
        check_company_scope("Microsoft revenue was reported for the quarter.", "finding")
    # A company named in the evidence may be named in the text.
    with company_scope("RELIANCE.NS", "Nvidia partners with Reliance Industries on AI data centres"):
        check_company_scope("Reliance Industries announced a partnership with Nvidia.", "item")


def test_ambiguous_words_are_not_treated_as_other_companies():
    from app.services.narrative_validation import foreign_companies

    text = "Revenue was a low point; the target is to manage cost, and the outlook is uncertain."
    assert foreign_companies(text, "MSFT", "") == []


def test_financial_finding_naming_another_company_is_withheld(isolated_runtime, monkeypatch):
    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_financial_analysis":
            body = json.loads(default_handler(kwargs, n).output_text)
            body["findings"][0]["interpretation"] += " This compares favourably with Microsoft Corporation."
            return completion(json.dumps(body))
        return default_handler(kwargs, n)

    install_fake_ai(monkeypatch, handler)
    report = run("AAPL")
    analysis = report["financial"]["analysis"]
    reasons = " ".join(w["reason"] for w in analysis["withheld"])
    assert "another company" in reasons and "MSFT" in reasons
    texts = json.dumps(analysis["findings"])
    assert "Microsoft" not in texts


def test_strategic_text_naming_another_company_is_withheld(isolated_runtime, monkeypatch):
    def handler(kwargs, n):
        if schema_name(kwargs) == "insightiq_strategic_analysis":
            body = json.loads(default_handler(kwargs, n).output_text)
            body["priorities"][0]["why_it_matters"] = (
                "It matters because Alphabet Inc. reported similar figures this quarter."
            )
            return completion(json.dumps(body))
        return default_handler(kwargs, n)

    install_fake_ai(monkeypatch, handler)
    report = run("MSFT")
    strategic = report["strategic"]
    assert any("another company" in w["reason"] for w in strategic["withheld_analysis"])
    assert "Alphabet" not in json.dumps([p.get("analysis") for p in strategic["research_priorities"]])


def test_every_evidence_item_belongs_to_the_selected_company(isolated_runtime, monkeypatch):
    install_fake_ai(monkeypatch)
    report = run("NVDA")
    assert report["ticker"] == "NVDA"
    for article in report["news"]["selected_articles"] + report["news"]["approved_primary_articles"]:
        assert article["ticker"] == "NVDA" and article["evidence_id"].startswith("NEWS_NVDA_")
    for claim in report["financial"]["verified_claims"]["verified_claims"]:
        assert claim["evidence_id"].startswith("SEC_")
    for record in report["financial"]["evidence_package"]["records"]:
        assert "NVDA" not in json.dumps(record) or record.get("ticker", "NVDA") == "NVDA"


# ------------------------------------------------- cancellation / deadline

def _queued_job(root, ticker):
    from app.services import research_jobs

    research_jobs.initialize_job_storage()
    return research_jobs.create_job(root, ticker)


def test_cancel_queued_job_never_runs(isolated_runtime, monkeypatch):
    from app.services import research_jobs

    install_fake_ai(monkeypatch)
    job = _queued_job(isolated_runtime.root, "MSFT")
    cancelled = research_jobs.request_cancel(isolated_runtime.root, job["run_id"])
    assert cancelled["status"] == "failed" and cancelled["error_code"] == "cancelled"
    with pytest.raises(research_jobs.ResearchJobError):
        research_jobs.execute_queued_research_job(isolated_runtime.root, job["run_id"], lambda t, run_id: {})


def test_cancel_running_job_stops_before_next_agent_and_saves_no_report(isolated_runtime, monkeypatch):
    from app.agents.research_workflow import run_research_workflow
    from app.services import research_jobs

    monkeypatch.setenv("INSIGHTIQ_AUTO_EXECUTIVE_SUMMARY", "0")
    root = isolated_runtime.root
    job = _queued_job(root, "MSFT")

    def handler(kwargs, n):
        # The user cancels while the financial call is in flight.
        research_jobs.request_cancel(root, job["run_id"])
        return default_handler(kwargs, n)

    client = install_fake_ai(monkeypatch, handler)
    with pytest.raises(Exception) as info:
        research_jobs.execute_queued_research_job(
            root, job["run_id"],
            lambda t, run_id: run_research_workflow(t, run_id=run_id, options={"refresh": "skip"}),
        )
    assert type(info.value).__name__ == "JobCancelled"
    stored = research_jobs.read_job(job["run_id"])
    assert stored["status"] == "failed" and stored["error_code"] == "cancelled"
    assert not (root / "reports" / "research_runs" / f"{job['run_id']}.json").exists()
    # No further AI request after the cancellation was observed.
    assert len(client.requests) == 1
    assert not research_jobs.cancel_requested(job["run_id"])


def test_job_deadline_stops_research(isolated_runtime, monkeypatch):
    from app.agents.research_workflow import run_research_workflow
    from app.core.config import get_settings
    from app.services import research_jobs

    install_fake_ai(monkeypatch)
    monkeypatch.setenv("INSIGHTIQ_AUTO_EXECUTIVE_SUMMARY", "0")
    monkeypatch.setattr(get_settings(), "research_job_deadline_seconds", 300)
    import time as real_time

    start = real_time.monotonic()
    calls = {"n": 0}

    def fake_monotonic():
        # Every call advances 200 s: the 300 s job deadline is set at one call
        # and passed two calls later, whatever else reads the clock first.
        calls["n"] += 1
        return start + 200 * calls["n"]

    monkeypatch.setattr("app.services.ai_provider.time.monotonic", fake_monotonic)
    monkeypatch.setattr("time.monotonic", fake_monotonic)
    root = isolated_runtime.root
    job = _queued_job(root, "AAPL")
    with pytest.raises(Exception) as info:
        research_jobs.execute_queued_research_job(
            root, job["run_id"],
            lambda t, run_id: run_research_workflow(t, run_id=run_id, options={"refresh": "skip"}),
        )
    assert type(info.value).__name__ == "JobDeadlineExceeded"
    assert research_jobs.read_job(job["run_id"])["error_code"] == "deadline_exceeded"


def test_cancel_endpoint_validates_ids():
    from fastapi.testclient import TestClient

    from app.main import app
    import os

    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as client:
        assert client.post("/api/v1/jobs/not-a-uuid/cancel", headers=headers).status_code == 422
        assert client.post("/api/v1/jobs/00000000-0000-4000-8000-000000000000/cancel").status_code == 401


def test_reissued_news_id_never_resolves_to_another_company():
    """Regression: after a restore, ID 93 belonged to ETERNAL.NS while an ITC.NS
    report cited ID 93. The citation must resolve by company + URL, or be flagged."""
    import sqlite3

    from app.services.report_history import resolve_news_evidence

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE news_articles (id INTEGER PRIMARY KEY, company_ticker TEXT, article_url TEXT)")
    con.execute("INSERT INTO news_articles VALUES (93, 'ETERNAL.NS', 'https://e.invalid/a')")
    cited = {"database_id": 93, "source_url": "https://i.invalid/itc-results"}
    assert resolve_news_evidence(con, "ITC.NS", cited) == {"status": "unresolvable", "database_id": None}
    con.execute("INSERT INTO news_articles VALUES (120, 'ITC.NS', 'https://i.invalid/itc-results')")
    assert resolve_news_evidence(con, "ITC.NS", cited) == {"status": "resolved_by_source_url", "database_id": 120}
    same = {"database_id": 120, "source_url": "https://i.invalid/itc-results"}
    assert resolve_news_evidence(con, "ITC.NS", same) == {"status": "resolved", "database_id": 120}
