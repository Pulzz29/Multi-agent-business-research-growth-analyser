"""Per-report chart dataset (frozen, point-in-time) and the research report PDF."""

import json
import os
import re
import shutil
import zlib

import pytest

from conftest import PROJECT_ROOT
from research_v2_support import install_fake_ai, isolated_runtime  # noqa: F401  (fixture)

RUN = "5a25fd13-a894-4b58-ba69-ed8c4422fbe8"  # saved MSFT report (SEC profile)


@pytest.fixture
def api(isolated_runtime, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    source = PROJECT_ROOT / "reports" / "research_runs" / f"{RUN}.json"
    if not source.is_file():
        pytest.skip("saved MSFT report not present")
    target = isolated_runtime.root / "reports" / "research_runs"
    target.mkdir(parents=True, exist_ok=True)
    from research_v2_support import put_doc

    put_doc(target / source.name, source.read_text(encoding="utf-8"))
    ai = install_fake_ai(monkeypatch)
    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as http:
        yield http, headers, isolated_runtime.root, ai


def pdf_text(content):
    """Decompressed content of every PDF stream (no PDF library needed)."""
    import base64

    out = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\n?endstream", content, re.S):
        data = match.group(1).strip()
        if data.endswith(b"~>"):  # ReportLab: /ASCII85Decode then /FlateDecode
            data = base64.a85decode(data[:-2])
        try:
            out.append(zlib.decompress(data))
        except zlib.error:
            out.append(data)
    return b"\n".join(out).decode("latin-1")


def test_insights_endpoint_is_frozen_and_complete(api):
    http, headers, root, ai = api
    url = f"/api/v1/companies/MSFT/reports/{RUN}/insights"
    first = http.get(url, headers=headers)
    assert first.status_code == 200
    data = first.json()
    assert data["run_id"] == RUN and data["currency"] == "USD" and data["data_as_of"]
    assert len(data["revenue_net_income"]["quarterly"]) >= 2
    assert data["growth"]["metrics"]["revenue_yoy"]["status"] == "ok"
    assert data["price"]["available"] is False and "not stored" in data["price"]["reason"]
    assert all(s["url"] and s["publisher"] for s in data["sources"])
    assert {r["key"] for r in data["ratios"]["rows"]} >= {"pe_ratio", "roe_pct", "debt_to_equity"}
    path = root / "reports" / "research_runs" / "insights" / f"{RUN}.json"
    from research_v2_support import get_doc

    before = get_doc(path)
    assert before is not None
    second = http.get(url, headers=headers).json()
    assert get_doc(path) == before and second["computed_at_utc"] == data["computed_at_utc"]
    assert not ai.requests  # no AI calls


def test_insights_are_point_in_time(api):
    """Statements stored after the report was generated are not used."""
    http, headers, root, _ = api
    from app.services import market_storage

    market_storage.register_company(symbol="MSFT", market="US", name="Microsoft Corporation", currency="USD")
    market_storage.insert_statement_rows([{
        "symbol": "MSFT", "provider": "FMP", "statement": "income", "period_type": "annual",
        "period_end": "2026-06-30", "fiscal_year": "2026", "metric": "revenue", "value_text": "1",
        "currency": "USD", "source_field": "test",
    }])
    data = http.get(f"/api/v1/companies/MSFT/reports/{RUN}/insights", headers=headers).json()
    # The row above was stored "now", after the report: ratios come from SEC data.
    assert data["ratios"]["basis"].startswith("SEC filings")


def test_insights_input_errors(api):
    http, headers, _, _ = api
    assert http.get(f"/api/v1/companies/MSFT/reports/{RUN}/insights").status_code == 401
    assert http.get("/api/v1/companies/ZZZZ/reports/" + RUN + "/insights", headers=headers).status_code == 404
    assert http.get("/api/v1/companies/MSFT/reports/not-a-uuid/insights", headers=headers).status_code == 422
    missing = "00000000-0000-4000-8000-000000000000"
    assert http.get(f"/api/v1/companies/MSFT/reports/{missing}/insights", headers=headers).status_code == 404


def test_pdf_without_ai_brief(api):
    http, headers, _, ai = api
    r = http.get(f"/api/v1/companies/MSFT/reports/{RUN}/pdf", headers=headers)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and len(r.content) > 5000
    assert re.fullmatch(r'attachment; filename="Microsoft_Corporation_Research_Report_\d{4}-\d{2}-\d{2}\.pdf"',
                        r.headers["content-disposition"])
    text = pdf_text(r.content)
    assert "AI summary not yet generated" in text
    for expected in ("Revenue and net income", "Margin trends", "Year-over-year growth", "USD billions",
                     "Key metrics and growth", "News sources", "Disclaimer", "Page 1"):
        assert expected in text, expected
    assert not ai.requests  # the PDF never calls the AI


def test_pdf_includes_the_generated_summary_and_research(api):
    http, headers, root, ai = api
    from app.services import executive_summary as es

    es.generate_brief(root, RUN, ticker="MSFT", client=None)  # fake client installed by the fixture
    calls = len(ai.requests)
    r = http.get(f"/api/v1/companies/MSFT/reports/{RUN}/pdf", headers=headers)
    text = pdf_text(r.content)
    for expected in ("AI summary", "Financial position", "Thorough research", "Growth analysis",
                     "Valuation snapshot", "based on data as of", "not investment advice"):
        assert expected in text, expected
    assert "AI summary not yet generated" not in text
    assert len(ai.requests) == calls


def test_pdf_input_errors(api):
    http, headers, _, _ = api
    assert http.get(f"/api/v1/companies/MSFT/reports/{RUN}/pdf").status_code == 401
    assert http.get("/api/v1/companies/MSFT/reports/not-a-uuid/pdf", headers=headers).status_code == 422


def test_filename_is_sanitised():
    from app.services.company_report_pdf import safe_filename

    assert safe_filename("Tata Consultancy Services Ltd.", "2026-11-24T10:00:00+00:00") == \
        "Tata_Consultancy_Services_Ltd_Research_Report_2026-11-24.pdf"
    assert safe_filename('a/../b"<>', "2026-01-02") == "a_b_Research_Report_2026-01-02.pdf"


def test_sector_report_pdf_has_page_numbers():
    from app.services import sector_report

    fixture = PROJECT_ROOT / "frontend" / "tests" / "fixtures" / "sector_snapshot.test-fixture.json"
    snapshot = json.loads(fixture.read_text(encoding="utf-8"))
    assert "Page 1" in pdf_text(sector_report.render_pdf(snapshot))
