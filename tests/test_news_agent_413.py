"""HTTP 413 fix: offline regression tests for the News Agent request budget,
sanitized diagnostics and accurate per-agent failure statuses.

No Groq or NewsData.io request is made: guarded_chat_completion is stubbed and
outbound network is blocked by conftest.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from groq import APIStatusError

from conftest import PROJECT_ROOT, TICKERS

PRE_FIX_DIR = next((PROJECT_ROOT / "migration_inputs").glob("pre_413_fix_*"), None)


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

    def capture(client, **kwargs):
        captured.clear()
        captured.update(kwargs)
        raise Captured()

    monkeypatch.setattr(module, "guarded_chat_completion", capture)
    module._captured = captured
    return module


def capture_request(module, ticker, articles):
    with pytest.raises(Captured):
        module.select_news_with_groq(ticker, articles)
    return dict(module._captured)


# ---------------- large / normal / empty collections ----------------

def test_large_collection_fits_budget_without_truncation(news_agent):
    big = [article(i, description_chars=12000 if i % 2 else 300) for i in range(1, 13)]
    originals = json.loads(json.dumps(big))
    subset, report = news_agent.fit_articles_to_budget("MSFT", big)

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
    subset, _ = news_agent.fit_articles_to_budget("MSFT", low + high)
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
        news_agent.fit_articles_to_budget("MSFT", [article(1, description_chars=60000)])


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
    estimated = news_agent.estimate_request_tokens(request["messages"], request["response_format"])
    assert estimated + request["max_completion_tokens"] <= news_agent.DEFAULT_NEWS_REQUEST_TOKEN_BUDGET
    assert request["max_completion_tokens"] == 1200
    assert request["response_format"]["json_schema"]["strict"] is True
    sent = json.loads(request["messages"][1]["content"])["articles"]
    by_id = {a["evidence_id"]: a for a in articles}
    for item in sent:  # every sent field is byte-identical to the stored record
        source = by_id[item["evidence_id"]]
        for key in ("title", "description", "published_at", "publisher"):
            assert item[key] == source[key]


def test_configurable_budget(news_agent, monkeypatch):
    articles, _ = news_agent.load_news_evidence("MSFT")
    monkeypatch.setenv("INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET", "3500")
    request = capture_request(news_agent, "MSFT", articles)
    assert news_agent.estimate_request_tokens(request["messages"], request["response_format"]) <= 3500 - 1200
    monkeypatch.setenv("INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET", "100")
    with pytest.raises(news_agent.NewsAgentError):
        news_agent.news_request_token_budget()
    monkeypatch.setenv("INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET", "abc")
    with pytest.raises(news_agent.NewsAgentError):
        news_agent.news_request_token_budget()


@pytest.mark.skipif(PRE_FIX_DIR is None, reason="pre-fix backup not present (deployment package)")
def test_within_budget_request_identical_to_original_code(news_agent):
    """For a collection that already fit, the request equals the pre-fix implementation's."""
    spec = importlib.util.spec_from_file_location("news_agent_prefix", PRE_FIX_DIR / "news_agent.py")
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    captured = {}

    def capture(client, **kwargs):
        captured.update(kwargs)
        raise Captured()

    original.guarded_chat_completion = capture
    articles, _ = news_agent.load_news_evidence("GOOGL")
    with pytest.raises(Captured):
        original.select_news_with_groq("GOOGL", articles)
    new = capture_request(news_agent, "GOOGL", articles)
    for key in ("model", "messages", "response_format", "temperature", "max_completion_tokens"):
        assert new[key] == captured[key], key


# ---------------- sanitized 413 diagnostics ----------------

def test_413_diagnostic_is_sanitized_and_error_propagates(news_agent, monkeypatch, caplog):
    secret_text = "UNIQUE-ARTICLE-BODY-MARKER"
    articles = [article(1), {**article(2), "description": secret_text}]
    message = ("Request too large for model `openai/gpt-oss-120b` on tokens per minute (TPM): "
               "Limit 8000, Requested 19011, please reduce your message size and try again.")
    response = httpx.Response(413, request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"))

    def raise_413(client, **kwargs):
        raise APIStatusError(message, response=response, body=None)

    monkeypatch.setattr(news_agent, "guarded_chat_completion", raise_413)
    before = len(news_agent.recent_request_size_errors())
    with caplog.at_level("WARNING"):
        with pytest.raises(APIStatusError):
            news_agent.select_news_with_groq("MSFT", articles)
    event = news_agent.recent_request_size_errors()[before]
    assert event["status_code"] == 413 and event["provider_limit_tokens"] == 8000
    assert event["provider_requested_tokens"] == 19011 and event["provider_limit_type"] == "tokens_per_minute"
    assert event["articles_sent"] == 2 and event["request_body_bytes"] > 0
    dumped = json.dumps(event) + caplog.text
    assert secret_text not in dumped and "Microsoft business update" not in dumped
    assert os.environ.get("GROQ_API_KEY", "x") not in dumped


# ---------------- accurate agent statuses (isolated database) ----------------

STATUS_CHILD = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import httpx
from groq import APIStatusError
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
        413, request=httpx.Request("POST", "https://api.groq.com/x")))
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
                    "API_ACCESS_KEY": "x" * 20, "GROQ_API_KEY": "isolated-placeholder",
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
