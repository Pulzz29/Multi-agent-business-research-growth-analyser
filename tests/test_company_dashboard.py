"""Company intelligence dashboard: read-only endpoints, history merge, and summary validation.

API tests run against the locally running backend (see conftest). They only use GET
routes and the summary POST without a key or with invalid input, so no Groq,
NewsData.io or SEC request can be made. Summary validation tests mock the model.
"""
import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import PROJECT_ROOT, TICKERS

META_RUN = "3871153c-0bd0-4ee3-9bbb-510d5eb32bce"
AMZN_RUN = "756279ca-009a-4af4-9de7-8c3a822e713c"

NEW_GET = [
    "/api/v1/companies/summary",
    "/api/v1/companies/META/overview",
    "/api/v1/research/runs",
    "/api/v1/research/runs?ticker=META",
    f"/api/v1/companies/META/reports/{META_RUN}",
]


def _hashes(pattern):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(PROJECT_ROOT.glob(pattern))
    }


# ---------------------------------------------------------------- auth


@pytest.mark.parametrize("path", NEW_GET)
def test_new_routes_require_key(client, path):
    r = client.get(path)
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_summary_post_requires_key(client):
    r = client.post(f"/api/v1/companies/META/reports/{META_RUN}/executive-summary")
    assert r.status_code == 401


def test_invalid_inputs(client, auth):
    assert client.get("/api/v1/companies/TSLA/overview", headers=auth).status_code == 404
    assert client.get("/api/v1/research/runs?ticker=TSLA", headers=auth).status_code == 404
    assert client.get("/api/v1/companies/META/reports/not-a-uuid", headers=auth).status_code == 422
    # A report is only served under its own company.
    assert client.get(f"/api/v1/companies/AAPL/reports/{META_RUN}", headers=auth).status_code == 404
    # Invalid run ID is rejected before any summary generation is attempted.
    assert client.post("/api/v1/companies/META/reports/nope/executive-summary", headers=auth).status_code == 422


# ----------------------------------------------------------- read-only data


@pytest.mark.parametrize("ticker", TICKERS)
def test_company_overview_all_companies(client, auth, ticker):
    before = _hashes("data/forecast_cache_v1/*.json")
    r = client.get(f"/api/v1/companies/{ticker}/overview", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == ticker
    for metric in ("revenue", "operating_cash_flow"):
        series = body["financials"][metric]
        assert len(series) >= 20
        for row in series:
            assert row["unit"] == "USD" and row["evidence"], row
            assert Decimal(row["value_usd"])  # exact decimal string, never a float
        fc = body["forecasts"][metric]
        assert fc["available"] is True
        assert len(fc["predictions"]) == fc["horizon_quarters"] == 4
    for article in body["news"]:
        assert article["source_url"] is None or article["source_url"].startswith("https://")
    # Opening a company never retrains or rewrites forecasts.
    assert _hashes("data/forecast_cache_v1/*.json") == before


def test_growth_only_for_valid_comparisons(client, auth):
    body = client.get("/api/v1/companies/AAPL/overview", headers=auth).json()
    from datetime import date, timedelta

    for metric in ("revenue", "operating_cash_flow"):
        series = body["financials"][metric]
        by_end = {r["period_end"]: r for r in series}
        for row in series:
            if row["qoq_change_pct"] is not None:
                prev = by_end[row["qoq_basis"]]
                assert date.fromisoformat(prev["period_end"]) + timedelta(days=1) == date.fromisoformat(row["period_start"])
            if row["yoy_change_pct"] is not None:
                prev = by_end[row["yoy_basis"]]
                gap = (date.fromisoformat(row["period_end"]) - date.fromisoformat(prev["period_end"])).days
                assert 350 <= gap <= 380 and prev["fiscal_quarter"] == row["fiscal_quarter"]


def test_meta_q4_2025_provenance(client, auth):
    body = client.get("/api/v1/companies/META/overview", headers=auth).json()
    expected = {
        "revenue": ("59893000000", {9886, 9883}),
        "operating_cash_flow": ("36214000000", {9186, 9185}),
    }
    for metric, (value, facts) in expected.items():
        row = next(r for r in body["financials"][metric] if r["period_end"] == "2025-12-31")
        assert row["period_start"] == "2025-10-01"
        assert row["value_usd"] == value
        assert row["method"] == "annual_minus_nine_month_ytd"
        assert {e["sec_fact_id"] for e in row["evidence"]} == facts
        assert row["fiscal_label"] == "FY2025 Q4"


def test_fiscal_labels_follow_company_calendars(client, auth):
    labels = {}
    for ticker in ("MSFT", "NVDA", "AAPL"):
        series = client.get(f"/api/v1/companies/{ticker}/overview", headers=auth).json()["financials"]["revenue"]
        labels[ticker] = {r["period_end"]: r["fiscal_label"] for r in series}
    assert labels["MSFT"]["2026-09-30"] == "FY2027 Q1"
    assert labels["NVDA"]["2026-07-26"] == "FY2027 Q2"
    assert labels["AAPL"]["2026-09-26"] == "FY2026 Q4"


def test_companies_summary(client, auth):
    body = client.get("/api/v1/companies/summary", headers=auth).json()
    assert [row["ticker"] for row in body["companies"]] == list(TICKERS)


# ------------------------------------------------------------- report history


def test_history_includes_every_saved_report(client, auth):
    body = client.get("/api/v1/research/runs", headers=auth).json()
    saved = {p.stem for p in (PROJECT_ROOT / "reports" / "research_runs").glob("*.json")}
    listed = {r["run_id"] for r in body["runs"] if r["report_available"]}
    assert saved <= listed
    assert body["completed_report_count"] >= len(saved)
    assert sum(1 for r in body["runs"] if r["is_latest_completed"]) == 1


@pytest.mark.parametrize("ticker,run_id", [("META", META_RUN), ("AMZN", AMZN_RUN)])
def test_saved_report_returned_unchanged(client, auth, ticker, run_id):
    r = client.get(f"/api/v1/companies/{ticker}/reports/{run_id}", headers=auth)
    assert r.status_code == 200
    stored = json.loads((PROJECT_ROOT / "reports" / "research_runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert r.json()["report"] == stored


def test_company_history_is_filtered(client, auth):
    body = client.get("/api/v1/research/runs?ticker=META", headers=auth).json()
    assert body["runs"] and all(r["ticker"] == "META" for r in body["runs"])


# ------------------------------------------------- summary validation (offline)


def _context():
    from app.services.executive_summary import build_context

    report = json.loads((PROJECT_ROOT / "reports" / "research_runs" / f"{META_RUN}.json").read_text(encoding="utf-8"))
    return report, build_context(report)


def _summary(**overrides):
    from app.services.executive_summary import ExecutiveSummary

    sections = [
        {"section": "financial_position", "evidence_ids": ["SEC_revenue_20251231"],
         "text": "Meta reported revenue of USD 59.89 billion for the quarter ending 2025-12-31, "
                 "up 23.8% on the same quarter a year earlier, according to SEC filings."},
        {"section": "news_developments", "evidence_ids": ["NEWS_META_36"],
         "text": "The Times Of India reported remarks by Meta's AI chief about Apple's rumored AI agent; "
                 "the publisher's account has not been independently verified by InsightIQ."},
        {"section": "evidence_limitations", "evidence_ids": [],
         "text": "The evidence covers few observations and cannot establish causes, partly due to the short history."},
    ]
    for index, changes in overrides.items():
        sections[int(index)].update(changes)
    return ExecutiveSummary.model_validate({"sections": sections})


def test_summary_validator_accepts_grounded_text():
    from app.services.executive_summary import validate_summary

    _, ctx = _context()
    sections, words = validate_summary(_summary(), ctx)
    assert sections[0]["section"] == "financial_position" and words > 0


@pytest.mark.parametrize(
    "override,message",
    [
        ({"0": {"text": "Revenue was USD 61.2 billion for the quarter ending 2025-12-31 and grew strongly over time in the period."}}, "number"),
        ({"1": {"evidence_ids": ["NEWS_META_999"]}}, "evidence ID"),
        ({"0": {"text": "Revenue of USD 59.89 billion rose due to advertising demand in the quarter ending 2025-12-31 and more."}}, "causal"),
        ({"0": {"evidence_ids": []}}, "cited no evidence"),
    ],
)
def test_summary_validator_rejects_ungrounded_text(override, message):
    from app.services.executive_summary import ExecutiveSummaryError, validate_summary

    _, ctx = _context()
    with pytest.raises(ExecutiveSummaryError, match=message):
        validate_summary(_summary(**override), ctx)


def test_summary_generation_rejects_incomplete_response(tmp_path, monkeypatch):
    import shutil

    from app.services import executive_summary as es

    (tmp_path / "reports" / "research_runs").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "reports" / "research_runs" / f"{META_RUN}.json", tmp_path / "reports" / "research_runs")

    def fake_call(messages):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="{}"))])

    monkeypatch.setattr(es, "call_groq", fake_call)
    with pytest.raises(es.ExecutiveSummaryError, match="incomplete"):
        es.generate_summary(tmp_path, META_RUN, ticker="META")
    record = json.loads((tmp_path / "reports" / "research_runs" / "summaries" / f"{META_RUN}.json").read_text())
    assert record["status"] == "unavailable" and record["failure_stage"] == "response_shape"
