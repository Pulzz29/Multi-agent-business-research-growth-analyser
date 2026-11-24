"""News Agent request budget: offline regression tests for the selection
request size, sanitized diagnostics and accurate per-agent failure statuses.

No Azure OpenAI or NewsData.io request is made: the Azure client is a
test-only stub and outbound network is blocked by conftest.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from openai import APIStatusError

from conftest import PROJECT_ROOT, TICKERS

class Captured(Exception):
    pass


def article(i, relevance=0.7, description_chars=200, ticker="MSFT"):
    return {
        "evidence_id": f"NEWS_{ticker}_{i}",
        "database_id": i,
        "ticker": ticker,
        "title": f"Microsoft business update {i}",
        "description": ("Microsoft revenue " * (description_chars // 18 + 1))[:description_chars],
        "source_url": f"https://example.invalid/{i}",
        "published_at": f"2026-11-22T{(23 - i % 24):02d}:00:00+00:00",
        "publisher": "Example",
        "relevance_score": relevance,
        "relevance_reason": "company_business_development",
    }


@pytest.fixture()
def news_agent(monkeypatch):
    from app.agents import news_agent as module

    monkeypatch.delenv("INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET", raising=False)
    captured = {}

    def capture(**kwargs):
        # Record the exact request, then answer with a valid selection of the
        # first offered article (test-only response, never stored anywhere).
        from research_v2_support import completion

        captured.clear()
        captured.update(kwargs)
        offered = json.loads(kwargs["input"][-1]["content"])["articles"]
        return completion(json.dumps({"selected_articles": [
            {"evidence_id": offered[0]["evidence_id"], "topic": "other_business_news"}]}))

    from types import SimpleNamespace

    from app.services import ai_provider

    stub = SimpleNamespace(responses=SimpleNamespace(create=capture))
    monkeypatch.setattr(ai_provider, "default_client", lambda timeout=None: stub)
    module._captured = captured
    return module


def capture_request(module, ticker, articles):
    module.select_news(ticker, articles)
    return dict(module._captured)


def request_messages(request):
    return [{"role": "system", "content": request["instructions"]}, *request["input"]]


# ---------------- large / normal / empty collections ----------------

def test_large_collection_fits_budget_without_truncation(news_agent):
    big = [article(i, description_chars=12000 if i % 2 else 300) for i in range(1, 13)]
    originals = json.loads(json.dumps(big))
    subset, report = news_agent.fit_articles_to_budget("MSFT", big, budget=6000)

    assert 0 < len(subset) < len(big)
    assert report["estimated_input_tokens"] <= report["input_token_budget"]
    assert report["articles_excluded_for_budget"] == len(big) - len(subset)
    # Records are the original objects, unmodified, in original order.
    assert all(any(s is b for b in big) for s in subset)
    assert big == originals
    positions = [big.index(s) for s in subset]
    assert positions == sorted(positions)


def test_high_relevance_preferred_when_over_budget(news_agent):
    low = [article(i, relevance=0.6, description_chars=4000) for i in range(1, 7)]
    high = [article(i, relevance=0.9, description_chars=4000) for i in range(7, 13)]
    subset, _ = news_agent.fit_articles_to_budget("MSFT", low + high, budget=6000)
    kept_high = [a for a in subset if a["relevance_score"] == 0.9]
    kept_low = [a for a in subset if a["relevance_score"] == 0.6]
    assert 0 < len(subset) < 12 and kept_high
    # A lower-relevance article is only kept once every higher-relevance one is kept.
    assert not kept_low or len(kept_high) == len(high)


def test_normal_collection_unchanged(news_agent):
    normal = [article(i) for i in range(1, 13)]
    subset, report = news_agent.fit_articles_to_budget("MSFT", normal)
    assert subset == normal and report["articles_excluded_for_budget"] == 0


def test_empty_collection_rejected(news_agent):
    with pytest.raises(news_agent.NewsAgentError):
        news_agent.fit_articles_to_budget("MSFT", [])


def test_single_oversized_article_is_rejected_not_truncated(news_agent):
    with pytest.raises(news_agent.NewsAgentError):
        news_agent.fit_articles_to_budget("MSFT", [article(1, description_chars=60000)], budget=6000)


# ---------------- evidence IDs ----------------

def test_evidence_ids_preserved_and_excluded_ids_not_selectable(news_agent):
    big = [article(i, description_chars=12000 if i in (3, 5) else 300) for i in range(1, 9)]
    subset, _ = news_agent.fit_articles_to_budget("MSFT", big, budget=3000)
    sent_ids = {a["evidence_id"] for a in subset}
    assert sent_ids <= {a["evidence_id"] for a in big}
    excluded = [a for a in big if a["evidence_id"] not in sent_ids]
    assert excluded

    response = news_agent.NewsSelections(selected_articles=[
        news_agent.NewsSelection(evidence_id=excluded[0]["evidence_id"], topic="other_business_news")])
    with pytest.raises(news_agent.NewsAgentError):
        news_agent.validate_news_selections(response, subset)


# ---------------- request-size compliance on the real database ----------------

@pytest.mark.parametrize("ticker", TICKERS)
def test_real_request_within_budget(news_agent, ticker):
    articles, _ = news_agent.load_news_evidence(ticker)
    request = capture_request(news_agent, ticker, articles)
    from app.services.ai_provider import ai_runtime_settings

    settings = ai_runtime_settings()
    estimated = news_agent.estimate_request_tokens(request_messages(request))
    assert estimated <= settings["input_token_budget"]
    assert request["max_output_tokens"] == settings["max_output_news_selection"]
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["name"] == "insightiq_news_selections"
    sent = json.loads(request["input"][-1]["content"])["articles"]
    by_id = {a["evidence_id"]: a for a in articles}
    for item in sent:  # every sent field is byte-identical to the stored record
        source = by_id[item["evidence_id"]]
        for key in ("title", "description", "published_at", "publisher"):
            assert item[key] == source[key]


def test_configurable_budget(news_agent, monkeypatch):
    articles, _ = news_agent.load_news_evidence("MSFT")
    monkeypatch.setenv("INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET", "3500")
    request = capture_request(news_agent, "MSFT", articles)
    assert news_agent.estimate_request_tokens(request_messages(request)) <= 3500
    monkeypatch.setenv("INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET", "100")
    with pytest.raises(news_agent.NewsAgentError):
        news_agent.news_request_token_budget()
    monkeypatch.setenv("INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET", "abc")
    with pytest.raises(news_agent.NewsAgentError):
        news_agent.news_request_token_budget()


# ---------------- sanitized oversize-input diagnostics ----------------

def test_oversize_input_is_reported_sanitized(news_agent, monkeypatch, caplog):
    """Azure rejects an over-long input (HTTP 400 context_length_exceeded):
    the call fails with a short reason and no article text or key is logged."""
    from types import SimpleNamespace

    from app.services import ai_provider

    secret_text = "UNIQUE-ARTICLE-BODY-MARKER"
    articles = [article(1), {**article(2), "description": secret_text}]
    response = httpx.Response(400, request=httpx.Request(
        "POST", "https://insightiq-test.openai.azure.com/openai/v1/responses"))

    def raise_400(**kwargs):
        raise APIStatusError(
            "This model's maximum context length was exceeded.", response=response,
            body={"error": {"code": "context_length_exceeded", "param": "input"}},
        )

    stub = SimpleNamespace(responses=SimpleNamespace(create=raise_400))
    monkeypatch.setattr(ai_provider, "default_client", lambda timeout=None: stub)
    with caplog.at_level("INFO"):
        with pytest.raises(ai_provider.AICallFailed) as info:
            news_agent.select_news("MSFT", articles)
    assert info.value.reason == "payload_too_large"
    attempt = info.value.diagnostics["attempts"][0]
    assert attempt["status_code"] == 400 and attempt["provider_error_code"] == "context_length_exceeded"
    dumped = json.dumps(info.value.diagnostics) + caplog.text
    assert secret_text not in dumped and "Microsoft business update" not in dumped
    assert os.environ["AZURE_OPENAI_API_KEY"] not in dumped


# ---------------- accurate agent statuses (isolated database) ----------------

STATUS_CHILD = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import httpx
from openai import APIStatusError
from langgraph.graph import StateGraph, START, END
from typing import TypedDict
from app.database import sqlite_manager
root = Path(os.environ["INSIGHTIQ_PROJECT_ROOT"]).resolve()
assert sqlite_manager.working_database_path().resolve().is_relative_to(root)
assert sqlite_manager.restore_database_from_drive()["restored"]
from app.services import research_jobs

class S(TypedDict, total=False):
    run_id: str
    ticker: str
    status: str

def financial_agent(state): return {"status": "running"}
def news_agent(state):
    raise APIStatusError("Request too large", body=None, response=httpx.Response(
        413, request=httpx.Request("POST", "https://insightiq-test.openai.azure.com/openai/v1/responses")))
def strategic_agent(state): return {"status": "completed"}

b = StateGraph(S)
for fn in (financial_agent, news_agent, strategic_agent):
    b.add_node(fn.__name__, fn)
b.add_edge(START, "financial_agent"); b.add_edge("financial_agent", "news_agent")
b.add_edge("news_agent", "strategic_agent"); b.add_edge("strategic_agent", END)
graph = b.compile()

def runner(ticker, run_id=None):
    return graph.invoke({"run_id": run_id, "ticker": ticker, "status": "running"})

job = research_jobs.create_job(root, "MSFT")
try:
    research_jobs.execute_queued_research_job(root, job["run_id"], runner)
except Exception as exc:
    error = type(exc).__name__
final = research_jobs.read_job(job["run_id"])
journal = json.loads(research_jobs.mirror_path(root, job["run_id"]).read_text(encoding="utf-8"))
print("RESULT " + json.dumps({"status": final["status"], "agents": final["agent_statuses"],
      "journal_agents": journal["agent_statuses"], "error_type": final["error_type"], "raised": error}))
'''


def test_failed_news_agent_keeps_accurate_agent_statuses():
    base = Path(tempfile.mkdtemp(prefix="iq_413_"))
    try:
        root = base / "iq_isolated_status"
        (root / "data").mkdir(parents=True)
        (root / "tmp").mkdir()
        shutil.copy2(PROJECT_ROOT / "data" / "insightiq.sqlite3", root / "data" / "insightiq.sqlite3")
        script = base / "child.py"
        script.write_text(STATUS_CHILD, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("INSIGHTIQ_")}
        env.update({"INSIGHTIQ_PROJECT_ROOT": str(root), "TEMP": str(root / "tmp"), "TMP": str(root / "tmp"),
                    "API_ACCESS_KEY": "x" * 20, "AZURE_OPENAI_API_KEY": "isolated-placeholder",
                    "AZURE_OPENAI_ENDPOINT": "https://isolated-test.openai.azure.com/",
                    "AZURE_OPENAI_DEPLOYMENT": "gpt-5.4-pro",
                    "NEWSDATA_API_KEY": "isolated-placeholder", "SEC_USER_AGENT": "InsightIQ-test test@example.invalid"})
        proc = subprocess.run([sys.executable, "-I", str(script), str(PROJECT_ROOT / "backend")],
                              capture_output=True, text=True, env=env, timeout=300)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert line, proc.stdout[-2000:] + proc.stderr[-3000:]
        result = json.loads(line[7:])
        expected = {"financial": "completed", "news": "failed", "strategic": "pending"}
        assert result["status"] == "failed" and result["raised"] == "APIStatusError"
        assert result["agents"] == expected and result["journal_agents"] == expected
        assert result["error_type"] == "APIStatusError"
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------- AI selects none / fallback disclosure ----------------

def test_zero_selection_is_valid_and_more_than_four_is_rejected(news_agent):
    candidates = [article(i) for i in range(1, 7)]
    none = news_agent.NewsSelections(selected_articles=[])
    assert news_agent.validate_news_selections(none, candidates) == []
    five = news_agent.NewsSelections(selected_articles=[
        news_agent.NewsSelection(evidence_id=a["evidence_id"], topic="other_business_news") for a in candidates[:5]])
    with pytest.raises(news_agent.NewsAgentError):
        news_agent.validate_news_selections(five, candidates)


def test_ai_selecting_no_article_is_reported_not_overridden(news_agent, monkeypatch):
    candidates = [article(1, ticker="ITC.NS")]
    monkeypatch.setattr(news_agent, "load_news_evidence", lambda ticker: (candidates, 0))
    monkeypatch.setattr(news_agent, "select_news",
                        lambda ticker, articles: ([], {"call": "news_selection", "response_id": "resp_x",
                                                       "input_tokens": 50}))
    with pytest.raises(news_agent.NoMaterialNews) as info:
        news_agent.news_agent_node({"run_id": "r", "ticker": "ITC.NS", "status": "running"})
    assert info.value.diagnostics["response_id"] == "resp_x"
    assert info.value.diagnostics["selection_method"] == "ai_selected_none"
    assert info.value.available_articles == candidates
    assert "none of the 1 verified" in str(info.value)


def test_fallback_disclosure_names_the_real_cause(news_agent, monkeypatch):
    from app.services.ai_provider import AICallFailed

    candidates = [article(1), article(2)]
    monkeypatch.setattr(news_agent, "load_news_evidence", lambda ticker: (candidates, 0))
    for reason, expected in (("validation_rejected", "did not pass validation"),
                             ("connection_error", "provider was unavailable")):
        def fail(ticker, articles, reason=reason):
            raise AICallFailed(reason, {"call": "news_selection"})
        monkeypatch.setattr(news_agent, "select_news", fail)
        result = news_agent.news_agent_node({"run_id": "r", "ticker": "MSFT", "status": "running"})
        assert result["selection_method"] == "deterministic_fallback"
        assert any(expected in d for d in result["disclosures"])
