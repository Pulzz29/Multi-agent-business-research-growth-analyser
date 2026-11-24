"""
Test-only support for the research-intelligence (report schema v2) tests.

* ``isolated_runtime`` gives a test its own project root and its own SQLite
  working database (seeded through a read-only backup of the stored
  snapshot), so no test writes to the real runtime database, reports or
  forecast cache.
* ``FakeGroqClient`` and the ``respond_*`` helpers are deterministic stand-ins
  for Groq used ONLY inside tests. They write content derived from the
  evidence payload they receive, exactly as a real model would see it.
"""

import gc
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

COPY_REPORTS = (
    "financial_evidence_reconciliation_final.json",
    "forecast_uncertainty_quality_review.json",
)
COPY_CODE = (
    "backend/app/services/financial_normalization.py",
    "backend/app/services/forecasting.py",
)


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    from app.core.config import get_settings

    name = TEST_DB_PREFIX + uuid.uuid4().hex[:10]
    root = tmp_path / name
    (root / "reports" / "research_runs").mkdir(parents=True)
    (root / "data").mkdir()
    for file_name in COPY_REPORTS:
        shutil.copy2(PROJECT_ROOT / "reports" / file_name, root / "reports" / file_name)
    for rel in COPY_CODE:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / rel, target)
    shutil.copytree(
        PROJECT_ROOT / "data" / "forecast_cache_v1",
        root / "data" / "forecast_cache_v1",
    )

    working_dir = Path(tempfile.gettempdir()) / "insightiq_sqlite" / name
    working_dir.mkdir(parents=True, exist_ok=True)
    working_db = working_dir / "insightiq.sqlite3"

    source = sqlite3.connect(
        (PROJECT_ROOT / "data" / "insightiq.sqlite3").resolve().as_uri() + "?mode=ro",
        uri=True,
    )
    target = sqlite3.connect(str(working_db))
    try:
        source.backup(target)
        # Tests start from a known state: no market data, whatever the live
        # snapshot contains (this changes only the isolated test copy).
        existing = {r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("market_quotes", "market_statements", "market_companies", "provider_usage"):
            if table in existing:
                target.execute(f"DELETE FROM {table}")
        six = ("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA")
        marks = ",".join("?" for _ in six)
        # Refresh logs of market sources, and rows of companies added later.
        target.execute(
            "DELETE FROM source_refreshes WHERE source NOT IN ('SEC', 'NEWSDATA') "
            f"OR company_ticker NOT IN ({marks})", six)
        target.execute(f"DELETE FROM news_articles WHERE company_ticker NOT IN ({marks})", six)
        target.execute(f"DELETE FROM news_articles WHERE provider = 'FINNHUB'")
        target.execute(f"DELETE FROM research_execution_runs WHERE ticker NOT IN ({marks})", six)
        target.execute(f"DELETE FROM companies WHERE ticker NOT IN ({marks})", six)
        target.commit()
    finally:
        source.close()
        target.close()

    previous_root = os.environ.get("INSIGHTIQ_PROJECT_ROOT")
    os.environ["INSIGHTIQ_PROJECT_ROOT"] = str(root)
    get_settings.cache_clear()

    from app.services import market_ingestion, news_ingestion, sec_ingestion

    for module in (sec_ingestion, news_ingestion):
        monkeypatch.setattr(
            module, "sync_snapshot_to_drive",
            lambda *a, **k: {"integrity_ok": True},
        )
        monkeypatch.setattr(
            module, "create_recovery_point",
            lambda *a, **k: {"verified": True},
        )

    monkeypatch.setattr(market_ingestion, "create_recovery_point", lambda *a, **k: {"verified": True})

    try:
        yield SimpleNamespace(root=root, db=working_db)
    finally:
        if previous_root is None:
            os.environ.pop("INSIGHTIQ_PROJECT_ROOT", None)
        else:
            os.environ["INSIGHTIQ_PROJECT_ROOT"] = previous_root
        get_settings.cache_clear()
        remove_test_database_dir(working_dir)


TEST_DB_PREFIX = "InsightIQ_v2test_"


def remove_test_database_dir(path):
    """Delete a test-only working DB folder (never the real runtime folder)."""
    path = Path(path)
    if not path.name.startswith(TEST_DB_PREFIX):
        raise AssertionError("refusing to delete a non-test database folder")
    # app.database.connect() connections are closed when collected; on
    # Windows open SQLite handles would otherwise block deletion.
    gc.collect()
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.2)
        gc.collect()


def db_rows(db_path, sql, params=()):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


# ----------------------------------------------------------------- fake Groq

def completion(content, finish_reason="stop", usage=(1200, 900)):
    usage_ns = (
        SimpleNamespace(
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[0] + usage[1],
        )
        if usage else None
    )
    return SimpleNamespace(
        id="test-response",
        model="test-model",
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content),
        )],
        usage=usage_ns,
    )


class FakeGroqClient:
    """Routes each request to ``handler(kwargs, call_number)``."""

    def __init__(self, handler):
        self.requests = []

        def create(**kwargs):
            self.requests.append(kwargs)
            return handler(kwargs, len(self.requests))

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def payload_of(kwargs):
    return json.loads(kwargs["messages"][-1]["content"])


def schema_name(kwargs):
    return kwargs["response_format"]["json_schema"]["name"]


def respond_provider_financial(evidence):
    periods = evidence["reported_periods"]
    currency = evidence["currency"]
    by_metric = {}
    for p in periods:
        by_metric.setdefault(p["metric"], []).append(p)
    for rows in by_metric.values():
        rows.sort(key=lambda r: r["period_end"])
    claims = [
        {k: p[k] for k in ("evidence_id", "metric", "period_start", "period_end", "value_usd")}
        for metric in by_metric for p in by_metric[metric][-2:]
    ]
    rev = by_metric["revenue"][-1]
    yoy_id = f"CMP_revenue_{rev['period_end'].replace('-', '')}_yoy"
    yoy = next((c for c in evidence["comparisons"] if c["comparison_id"] == yoy_id), None)
    unit = "quarter" if rev["period_type"] == "quarterly" else "fiscal year"
    findings = [{
        "title": "Latest reported revenue against the prior year",
        "evidence_ids": [rev["evidence_id"]],
        "comparison_ids": [yoy["comparison_id"]] if yoy else [],
        "interpretation": (
            f"Revenue for the {unit} ending {rev['period_end']} was {currency} "
            f"{rev['value_billion_usd']} billion, as reported by the data provider. "
            + (f"Against the period one year earlier the InsightIQ comparison shows a change of "
               f"{yoy['change_pct'].lstrip('-')}%. " if yoy else "")
            + "Comparing matching periods limits seasonal distortion. "
            "The figure describes reported scale and direction only."
        ),
        "business_significance": "It anchors the company's reported revenue scale for the research period.",
        "limitations": "A single comparison cannot show what drove the change or whether it will persist.",
    }]
    return {"claims": claims, "findings": findings}


def respond_financial(kwargs):
    evidence = payload_of(kwargs)["evidence"]
    if "reported_periods" in evidence:
        return respond_provider_financial(evidence)
    quarters = evidence["sec_quarters"]
    by_metric = {}
    for q in quarters:
        by_metric.setdefault(q["metric"], []).append(q)
    for rows in by_metric.values():
        rows.sort(key=lambda r: r["period_end"])

    claims = []
    for metric in ("revenue", "operating_cash_flow"):
        for q in by_metric[metric][-2:]:
            claims.append({k: q[k] for k in (
                "evidence_id", "metric", "period_start", "period_end", "value_usd")})

    rev = by_metric["revenue"][-1]
    yoy_id = f"CMP_revenue_{rev['period_end'].replace('-', '')}_yoy"
    yoy = next(
        (c for c in evidence["comparisons"] if c["comparison_id"] == yoy_id),
        None,
    )
    ocf = by_metric["operating_cash_flow"][-1]

    findings = [{
        "title": "Latest quarterly revenue against the prior-year quarter",
        "evidence_ids": [rev["evidence_id"]],
        "comparison_ids": [yoy["comparison_id"]] if yoy else [],
        "interpretation": (
            f"Revenue for the standalone quarter ending {rev['period_end']} was "
            f"USD {rev['value_billion_usd']} billion. "
            + (f"Against the same quarter one year earlier, the InsightIQ comparison "
               f"shows a change of {yoy['change_pct'].lstrip('-')}%. " if yoy else "")
            + "Comparing matching quarters reduces seasonal distortion in the reading. "
            "The figure describes reported scale and direction only."
        ),
        "business_significance": (
            "It anchors the company's current reported revenue scale for the research period."
        ),
        "limitations": (
            "A single comparison cannot show what drove the change or whether it will persist."
        ),
    }, {
        "title": "Operating cash flow in the latest reported quarter",
        "evidence_ids": [ocf["evidence_id"]],
        "comparison_ids": [],
        "interpretation": (
            f"Operating cash flow for the quarter ending {ocf['period_end']} was "
            f"USD {ocf['value_billion_usd']} billion. This measures cash generated by "
            "operations before investment spending. Quarterly cash flow can vary with "
            "working-capital timing, so one quarter should be read with care."
        ),
        "business_significance": (
            "Cash generation indicates the internal funding available to the business."
        ),
        "limitations": (
            "The evidence does not include capital expenditure, so free cash flow is unknown."
        ),
    }]
    return {"claims": claims, "findings": findings}


def respond_news(kwargs):
    articles = payload_of(kwargs)["articles"]
    items = []
    for article in articles[:3]:
        headline_only = article["content_basis"] == "headline_only"
        items.append({
            "evidence_ids": [article["evidence_id"]],
            "theme_group": "company development",
            "reported_development": (
                f"{article['publisher'] or 'The publisher'} reported the headline "
                f"\"{article['headline']}\"."
                if headline_only else
                f"{article['publisher'] or 'The publisher'} reported a development "
                "concerning the company. The article excerpt describes it in summary form."
            ),
            "company_relevance": "The headline names the company directly.",
            "potential_implications": (
                "It is unclear whether this could matter for the business; "
                "no business implication can be drawn yet."
            ),
            "uncertainty": "The full article body was not reviewed and the report is unverified.",
        })
    return {"items": items}


def respond_strategic(kwargs):
    evidence = payload_of(kwargs)["evidence"]
    fins = [e for e in evidence if e["kind"] == "verified_sec_financial_fact"]
    fin = fins[0]
    news = next((e for e in evidence if e["kind"] == "company_news_headline"), None)
    if news is None:  # provider company without company news: two financial items
        news = fins[1] if len(fins) > 1 else None

    def priority(item, level):
        return {
            "evidence_id": item["evidence_id"],
            "theme": item["allowed_theme"],
            "priority": level,
            "title": "Monitor the reported item in subsequent periods",
            "classification": "monitoring_priority",
            "interpretation": (
                "The cited evidence records one observation for the company. "
                "On its own it does not establish direction or persistence, and the "
                "relationship between reported financial results and news coverage is "
                "unverified. The observation is useful as a reference point for the "
                "next filings and for corroborating reports from primary sources. "
                "Further evidence is needed before any conclusion about risk or "
                "opportunity can be drawn from this item."
            ),
            "why_it_matters": "It provides a reference point for the next reporting period.",
            "indicators_to_monitor": ["Subsequent quarterly filings", "Company announcements"],
            "follow_up": "",
            "uncertainty": "A single observation cannot show direction or persistence.",
            "supporting_evidence_ids": [item["evidence_id"]],
        }

    chosen = [priority(fin, "high")]
    if news is not None:
        chosen.append(priority(news, "medium"))
    return {"priorities": chosen}


def default_handler(kwargs, n):
    name = schema_name(kwargs)
    if name == "insightiq_financial_analysis":
        return completion(json.dumps(respond_financial(kwargs)))
    if name == "insightiq_news_analysis":
        return completion(json.dumps(respond_news(kwargs)))
    if name == "insightiq_strategic_analysis":
        return completion(json.dumps(respond_strategic(kwargs)))
    if name == "insightiq_news_selections":
        articles = payload_of(kwargs)["articles"]
        return completion(json.dumps({"selected_articles": [
            {"evidence_id": a["evidence_id"], "topic": "other_business_news"}
            for a in articles[:4]
        ]}))
    raise AssertionError("unexpected schema " + name)


def install_fake_groq(monkeypatch, handler=default_handler):
    """Patch every Groq entry point with one fake client (tests only)."""
    from app.agents import news_agent
    from app.services import groq_structured

    client = FakeGroqClient(handler)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_TEST_ONLY_NOT_A_REAL_KEY")
    monkeypatch.setattr(groq_structured, "default_client", lambda timeout: client)
    monkeypatch.setattr(news_agent, "Groq", lambda **kw: client)
    return client
