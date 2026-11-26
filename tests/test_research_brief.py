"""AI summary + thorough research ("research brief"): one implementation for
the automatic path, the button and the research job. Fake AI client only."""

import json
import os
import shutil
import threading

import pytest

from conftest import PROJECT_ROOT
from research_v2_support import (  # noqa: F401  (fixture)
    FakeAIClient,
    completion,
    install_fake_ai,
    isolated_runtime,
    respond_brief,
    schema_name,
)

RUN = "5a25fd13-a894-4b58-ba69-ed8c4422fbe8"  # saved MSFT report (SEC profile)


@pytest.fixture
def report_root(isolated_runtime):
    source = PROJECT_ROOT / "reports" / "research_runs" / f"{RUN}.json"
    if not source.is_file():
        pytest.skip("saved MSFT report not present")
    target = isolated_runtime.root / "reports" / "research_runs"
    target.mkdir(parents=True, exist_ok=True)
    from research_v2_support import put_doc

    put_doc(target / source.name, source.read_text(encoding="utf-8"))
    return isolated_runtime.root


def brief_client(**overrides):
    return FakeAIClient(lambda kwargs, n: completion(json.dumps(respond_brief(kwargs, **overrides))))


def sidecar(root):
    from research_v2_support import get_doc

    return json.loads(get_doc(root / "reports" / "research_runs" / "summaries" / f"{RUN}.json"))


def test_brief_is_generated_validated_and_reused_without_an_ai_call(report_root):
    from app.services import executive_summary as es

    client = brief_client()
    record = es.generate_brief(report_root, RUN, ticker="MSFT", client=client)
    assert len(client.requests) == 1
    assert client.requests[0]["text"]["format"]["name"] == "insightiq_research_brief"
    assert record["status"] == "completed" and record["brief_version"] == es.BRIEF_VERSION
    assert record["sections"][0]["section"] == "financial_position"
    research = record["research"]
    assert research["status"] == "completed"
    assert [s["section"] for s in research["sections"]][:3] == ["business_overview", "financial_performance", "growth_analysis"]
    assert record["based_on"]["dataset_sha256"] and record["data_as_of"]
    assert all(s.get("url") for s in record["sources"])
    # The prompt carries computed figures (not raw dumps) with citable IDs.
    evidence = json.loads(client.requests[0]["input"][-1]["content"])["evidence"]
    assert {m["evidence_id"] for m in evidence["computed_metrics"]} == {
        "METRIC_FINANCIALS", "METRIC_GROWTH", "METRIC_RATIOS", "METRIC_PRICE"}

    again = es.generate_brief(report_root, RUN, ticker="MSFT", client=client)
    assert len(client.requests) == 1  # cache hit
    assert again["generated_at_utc"] == record["generated_at_utc"]
    assert es.is_current(report_root, RUN)


def test_regenerates_when_the_underlying_dataset_changes(report_root):
    from app.services import executive_summary as es

    client = brief_client()
    es.generate_brief(report_root, RUN, ticker="MSFT", client=client)
    # Simulate the report's dataset being rebuilt from refreshed data.
    path = report_root / "reports" / "research_runs" / "insights" / f"{RUN}.json"
    from research_v2_support import get_doc, put_doc

    data = json.loads(get_doc(path))
    data["dataset_sha256"] = "0" * 64
    put_doc(path, json.dumps(data))
    assert not es.is_current(report_root, RUN)
    es.generate_brief(report_root, RUN, ticker="MSFT", client=client)
    assert len(client.requests) == 2
    assert sidecar(report_root)["based_on"]["dataset_sha256"] == "0" * 64


def test_old_summary_without_research_is_readable_and_not_current(report_root):
    from app.services import executive_summary as es

    directory = report_root / "reports" / "research_runs" / "summaries"
    directory.mkdir(parents=True, exist_ok=True)
    from research_v2_support import put_doc

    put_doc(directory / f"{RUN}.json", json.dumps({
        "run_id": RUN, "ticker": "MSFT", "status": "completed", "word_count": 80,
        "sections": [{"section": "financial_position", "text": "Older summary.", "evidence_ids": []}],
    }))
    state = es.brief_state(report_root, RUN, "MSFT")
    assert state["status"] == "completed" and state["job_status"] == "done"
    assert state["research"] is None and state["is_current"] is False


def test_forced_regeneration_keeps_previous_versions(report_root):
    from app.services import executive_summary as es

    client = brief_client()
    first = es.generate_brief(report_root, RUN, ticker="MSFT", client=client)
    second = es.generate_brief(report_root, RUN, ticker="MSFT", client=client, force=True, trigger="button")
    assert len(client.requests) == 2
    assert second["attempt"]["forced"] is True and second["attempt"]["trigger"] == "button"
    assert second["previous_versions"][0]["generated_at_utc"] == first["generated_at_utc"]
    assert "previous_versions" not in second["previous_versions"][0]


def test_failure_keeps_the_previous_successful_version(report_root):
    from app.services import executive_summary as es

    es.generate_brief(report_root, RUN, ticker="MSFT", client=brief_client())
    before = sidecar(report_root)
    failing = FakeAIClient(lambda kwargs, n: completion("", finish_reason="length"))
    with pytest.raises(es.ExecutiveSummaryError, match="output limit"):
        es.generate_brief(report_root, RUN, ticker="MSFT", client=failing, force=True)
    after = sidecar(report_root)
    assert after["status"] == "completed"
    assert after["sections"] == before["sections"] and after["research"] == before["research"]
    assert after["attempt"]["status"] == "failed" and "output limit" in after["attempt"]["reason"]
    state = es.brief_state(report_root, RUN, "MSFT")
    assert state["job_status"] == "failed" and state["status"] == "completed" and state["summary"]


def test_first_failure_is_recorded_as_unavailable(report_root):
    from app.services import executive_summary as es

    failing = FakeAIClient(lambda kwargs, n: completion("", finish_reason="length"))
    with pytest.raises(es.ExecutiveSummaryError):
        es.generate_summary(report_root, RUN, ticker="MSFT", client=failing)
    record = sidecar(report_root)
    assert record["status"] == "unavailable" and record["failure_stage"] == "provider_request"
    assert record["attempt"]["status"] == "failed"


def test_invalid_research_section_is_withheld_and_the_summary_kept(report_root):
    from app.services import executive_summary as es

    overrides = {"research_overrides": {
        "valuation_snapshot": {"text": "The shares trade at 987654 times earnings according to the evidence, "
                                       "which the report lists exactly as stored by InsightIQ."},
        "outlook": {"text": "Margins will increase next year, as the stored observations listed for these periods show "
                            "in the saved report and in the computed figures for this company."},
    }}
    record = es.generate_brief(report_root, RUN, ticker="MSFT", client=brief_client(**overrides))
    research = record["research"]
    withheld = {w["section"]: w["reason"] for w in research["withheld"]}
    assert "987654" in withheld["valuation_snapshot"]
    assert "will increase" in withheld["outlook"]
    assert research["status"] == "partial"
    assert record["status"] == "completed" and record["sections"]


def test_concurrent_requests_share_one_generation(report_root):
    from app.services import executive_summary as es

    release = threading.Event()

    def slow(kwargs, n):
        release.wait(10)
        return completion(json.dumps(respond_brief(kwargs)))

    client = FakeAIClient(slow)
    outcome, _ = es.start_brief(report_root, RUN, "MSFT", client=client, trigger="auto")
    assert outcome == "started"
    # A second automatic request, a button press and the research-job hook all
    # reuse the running attempt instead of starting new AI calls.
    assert es.start_brief(report_root, RUN, "MSFT", client=client)[0] == "running"
    assert es.start_brief(report_root, RUN, "MSFT", client=client, force=True, trigger="button")[0] == "running"
    assert es.start_brief(report_root, RUN, "MSFT", client=client, trigger="research_job", background=False)[0] == "running"
    assert es.brief_state(report_root, RUN, "MSFT")["status"] == "generating"
    release.set()
    for _ in range(200):
        if es.active_attempt(RUN) is None:
            break
        threading.Event().wait(0.05)
    assert len(client.requests) == 1
    assert es.brief_state(report_root, RUN, "MSFT")["job_status"] == "done"
    # Once current, a non-forced request does nothing.
    assert es.start_brief(report_root, RUN, "MSFT", client=client)[0] == "current"


def test_interrupted_attempt_is_reported_as_failed(report_root):
    from app.services import executive_summary as es

    es.generate_brief(report_root, RUN, ticker="MSFT", client=brief_client())
    record = sidecar(report_root)
    record["attempt"] = {"status": "running", "started_at_utc": "2026-11-24T00:00:00+00:00"}
    path = report_root / "reports" / "research_runs" / "summaries" / f"{RUN}.json"
    from research_v2_support import put_doc

    put_doc(path, json.dumps(record))
    state = es.brief_state(report_root, RUN, "MSFT")
    assert state["job_status"] == "failed" and "interrupted" in state["error"]
    assert state["status"] == "completed"  # the previous version stays visible


def test_brief_prompt_fits_the_input_budget(report_root):
    from app.services import ai_provider, executive_summary as es, report_insights

    from research_v2_support import get_doc

    report = json.loads(get_doc(report_root / "reports" / "research_runs" / f"{RUN}.json"))
    insights = report_insights.load_or_build(report, report_root)
    context = es.build_brief_context(report, insights)
    instructions, items = ai_provider.split_messages(es.build_brief_messages(context))
    estimate = ai_provider.estimate_request_tokens(
        instructions, items, ai_provider.text_format("insightiq_research_brief", es.ResearchBrief))
    assert estimate <= ai_provider.ai_runtime_settings()["input_token_budget"]


# ------------------------------------------------------------------- API

@pytest.fixture
def api(report_root, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    client = install_fake_ai(monkeypatch)
    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as http:
        yield http, headers, client


def _wait_done(http, headers):
    from app.services import executive_summary as es

    for _ in range(200):
        if es.active_attempt(RUN) is None:
            break
        threading.Event().wait(0.05)
    return http.get(f"/api/v1/companies/MSFT/reports/{RUN}/executive-summary", headers=headers).json()


def test_api_auto_then_cached_then_forced(api):
    http, headers, client = api
    url = f"/api/v1/companies/MSFT/reports/{RUN}/executive-summary"
    first = http.post(url, headers=headers)  # no body: the automatic path
    assert first.status_code == 202 and first.json()["request_outcome"] in ("started", "running")
    state = _wait_done(http, headers)
    assert state["status"] == "completed" and state["job_status"] == "done"
    assert state["research"]["sections"] and state["sources"] is not None and state["data_as_of"]
    calls = len(client.requests)
    assert http.post(url, headers=headers).json()["request_outcome"] == "current"
    assert len(client.requests) == calls
    forced = http.post(url, headers=headers, json={"force": True})
    assert forced.status_code == 202 and forced.json()["request_outcome"] == "started"
    state = _wait_done(http, headers)
    assert len(client.requests) == calls + 1 and state["previous_available"] is True


def test_api_rejects_bad_input(api):
    http, headers, _ = api
    url = f"/api/v1/companies/MSFT/reports/{RUN}/executive-summary"
    assert http.post(url, headers=headers, json={"force": True, "extra": 1}).status_code == 422
    assert http.post(url, json={"force": True}).status_code == 401
    assert http.post("/api/v1/companies/MSFT/reports/not-a-uuid/executive-summary", headers=headers).status_code == 422
