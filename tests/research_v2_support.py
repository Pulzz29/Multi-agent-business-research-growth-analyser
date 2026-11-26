"""
Test-only support for the research-intelligence (report schema v2) tests.

* ``isolated_runtime`` gives a test its own project root and its own
  database: on PostgreSQL a fresh schema in the test database (seeded from
  the stored snapshot), on SQLite a working copy seeded through a read-only
  backup of the snapshot. No test writes to the real database, reports or
  forecast cache.
* ``FakeAIClient`` and the ``respond_*`` helpers are deterministic stand-ins
  for Azure OpenAI used ONLY inside tests. They write content derived from the
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
    for cache_dir in sorted((PROJECT_ROOT / "data").glob("forecast_cache_v*")):
        shutil.copytree(cache_dir, root / "data" / cache_dir.name)

    if os.environ.get("DB_BACKEND") == "postgres":
        yield from _isolated_postgres(root, monkeypatch)
        return

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
        # SEC data of companies onboarded later (and their validation records).
        if "sec_facts" in existing:
            target.execute(f"DELETE FROM sec_facts WHERE company_ticker NOT IN ({marks})", six)
        if "sec_company_validation" in existing:
            target.execute("DELETE FROM sec_company_validation")
        target.execute(f"DELETE FROM companies WHERE ticker NOT IN ({marks})", six)
        # Live activity after the test baseline (genuine refreshes and news in
        # the real database) is removed from this isolated copy only, so the
        # tests' freshness scenarios do not depend on today's date.
        target.execute("DELETE FROM source_refreshes WHERE started_at >= ?", (TEST_DATA_CUTOFF,))
        target.execute("DELETE FROM news_articles WHERE retrieved_at >= ?", (TEST_DATA_CUTOFF,))
        target.commit()
    finally:
        source.close()
        target.close()

    from app.services import sec_coverage

    sec_coverage.invalidate_cache()
    previous_root = os.environ.get("INSIGHTIQ_PROJECT_ROOT")
    os.environ["INSIGHTIQ_PROJECT_ROOT"] = str(root)
    # The snapshot's stored articles are fixed historical data: a wide news
    # coverage window keeps these tests independent of today's date. The
    # coverage-window rule itself is tested with a fixed clock in
    # test_news_relevance.py.
    monkeypatch.setenv("INSIGHTIQ_NEWS_MAX_AGE_DAYS", "3650")
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
        sec_coverage.invalidate_cache()
        remove_test_database_dir(working_dir)


TEST_DB_PREFIX = "InsightIQ_v2test_"
SIX = ("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA")


def _baseline_deletes(conn):
    """The same known starting state as the SQLite copies (test data only)."""
    for table in ("market_quotes", "market_statements", "market_companies", "provider_usage"):
        conn.execute(f"DELETE FROM {table}")
    marks = ",".join("?" for _ in SIX)
    conn.execute("DELETE FROM source_refreshes WHERE source NOT IN ('SEC', 'NEWSDATA') "
                 f"OR company_ticker NOT IN ({marks})", SIX)
    conn.execute(f"DELETE FROM news_articles WHERE company_ticker NOT IN ({marks})", SIX)
    conn.execute("DELETE FROM news_articles WHERE provider = 'FINNHUB'")
    conn.execute(f"DELETE FROM research_execution_runs WHERE ticker NOT IN ({marks})", SIX)
    conn.execute(f"DELETE FROM sec_facts WHERE company_ticker NOT IN ({marks})", SIX)
    conn.execute("DELETE FROM sec_company_validation")
    conn.execute(f"DELETE FROM companies WHERE ticker NOT IN ({marks})", SIX)
    conn.execute("DELETE FROM source_refreshes WHERE started_at >= ?", (TEST_DATA_CUTOFF,))
    conn.execute("DELETE FROM news_articles WHERE retrieved_at >= ?", (TEST_DATA_CUTOFF,))


class PostgresTestSchema:
    """Handle of one isolated PostgreSQL test schema (isolated_runtime.db)."""

    def __init__(self, schema):
        self.schema = schema

    def connect(self):
        from app.database import db

        return db.direct_connect(schema=self.schema)

    def __repr__(self):
        return f"PostgresTestSchema({self.schema!r})"


def open_db(handle):
    """Connection to a test database handle (PostgreSQL schema or SQLite path)."""
    if isinstance(handle, PostgresTestSchema):
        return handle.connect()
    from app.database.db import Connection

    con = sqlite3.connect(str(handle))
    con.row_factory = sqlite3.Row
    return Connection(con, False)


def _seed_fingerprint(path):
    stat = Path(path).stat()
    return f"{Path(path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}"


def _seed_schema(schema, source):
    """(Re)create schema in the test database from the SQLite file source."""
    from app.database import db
    from app.database.migrations import run_migrations
    from app.database.sqlite_to_postgres import copy_database

    fingerprint = _seed_fingerprint(source)
    admin = db.direct_connect(schema="public", autocommit=True)
    try:
        exists = admin.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = '_seed_meta'",
            (schema,),
        ).fetchone()
        if exists:
            row = admin.execute(f'SELECT fingerprint FROM "{schema}"._seed_meta').fetchone()
            if row and row[0] == fingerprint:
                existing = db.direct_connect(schema=schema)
                try:
                    run_migrations(existing)  # newer migrations only
                finally:
                    existing.close()
                return False
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin.close()
    conn = db.direct_connect(schema=schema)
    try:
        run_migrations(conn)
        copy_database(source, conn)
        conn.execute("CREATE TABLE _seed_meta (fingerprint TEXT NOT NULL)")
        conn.execute("INSERT INTO _seed_meta VALUES (?)", (fingerprint,))
        conn.commit()
    finally:
        conn.close()
    return True


def ensure_seed_schemas():
    """seed_snapshot: data/insightiq.sqlite3 (isolated tests start from it).
    seed_live: the runtime working copy (what non-isolated tests read)."""
    from app.database.sqlite_manager import working_database_path

    snapshot = PROJECT_ROOT / "data" / "insightiq.sqlite3"
    _seed_schema("seed_snapshot", snapshot)
    live = working_database_path()
    _seed_schema("seed_live", live if live.is_file() else snapshot)
    # Saved reports, journals and forecast caches of the project (test copy).
    from app.services import document_store

    document_store.import_files(PROJECT_ROOT, overwrite=True)


def _isolated_postgres(root, monkeypatch):
    from app.core.config import get_settings
    from app.database import db
    from app.database.migrations import run_migrations
    from app.database.sqlite_to_postgres import TABLE_ORDER, reset_sequences

    schema = "t_" + uuid.uuid4().hex[:12]
    admin = db.direct_connect(schema="public", autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        admin.close()
    conn = db.direct_connect(schema=schema)
    try:
        run_migrations(conn)
        six = ",".join(f"'{t}'" for t in SIX)
        # Copy only the rows the baseline keeps (same result as copy + _baseline_deletes).
        keep = {
            "market_quotes": "FALSE", "market_statements": "FALSE", "market_companies": "FALSE",
            "provider_usage": "FALSE", "sec_company_validation": "FALSE",
            "companies": f"ticker IN ({six})",
            "sec_facts": f"company_ticker IN ({six})",
            "news_articles": f"company_ticker IN ({six}) AND provider <> 'FINNHUB'",
            "research_execution_runs": f"ticker IN ({six})",
            "source_refreshes": f"source IN ('SEC', 'NEWSDATA') AND company_ticker IN ({six})",
        }
        for table in TABLE_ORDER:
            conn.execute(f'INSERT INTO "{schema}"."{table}" SELECT * FROM seed_snapshot."{table}" '
                         f'WHERE {keep.get(table, "TRUE")}')
        reset_sequences(conn)
        _baseline_deletes(conn)
        conn.commit()
    finally:
        conn.close()

    from app.services import market_storage, sec_coverage

    sec_coverage.invalidate_cache()
    previous_root = os.environ.get("INSIGHTIQ_PROJECT_ROOT")
    os.environ["INSIGHTIQ_PROJECT_ROOT"] = str(root)
    monkeypatch.setenv("INSIGHTIQ_NEWS_MAX_AGE_DAYS", "3650")
    previous_schema = os.environ.get("DB_SCHEMA")
    monkeypatch.setenv("DB_SCHEMA", schema)
    get_settings.cache_clear()
    db.close_pools()
    market_storage._INITIALISED.clear()

    # The forecast caches copied into the temporary root (stored in the database).
    from app.services import document_store

    document_store.import_files(root, overwrite=True)

    from app.services import market_ingestion, news_ingestion, sec_ingestion

    for module in (sec_ingestion, news_ingestion):
        monkeypatch.setattr(module, "sync_snapshot_to_drive", lambda *a, **k: {"integrity_ok": True})
        monkeypatch.setattr(module, "create_recovery_point", lambda *a, **k: {"verified": True})
    monkeypatch.setattr(market_ingestion, "create_recovery_point", lambda *a, **k: {"verified": True})
    try:
        yield SimpleNamespace(root=root, db=PostgresTestSchema(schema), schema=schema)
    finally:
        if previous_root is None:
            os.environ.pop("INSIGHTIQ_PROJECT_ROOT", None)
        else:
            os.environ["INSIGHTIQ_PROJECT_ROOT"] = previous_root
        db.close_pools()
        # Restore the session schema now: later code in this teardown (and the
        # next test) must not cache settings that point at the dropped schema.
        if previous_schema is None:
            monkeypatch.delenv("DB_SCHEMA", raising=False)
        else:
            monkeypatch.setenv("DB_SCHEMA", previous_schema)
        get_settings.cache_clear()
        sec_coverage.invalidate_cache()
        market_storage._INITIALISED.clear()
        admin = db.direct_connect(schema="public", autocommit=True)
        try:
            assert schema.startswith("t_")
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            admin.close()


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
    con = open_db(db_path)
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def live_db_scalar(sql, params=()):
    """Read the database the running backend uses (live API tests only)."""
    if os.environ.get("DB_BACKEND") == "postgres":
        from app.database import db
        from conftest import APP_DB_NAME

        con = db.direct_connect(dbname=APP_DB_NAME, schema="public")
        try:
            return con.execute(sql, params).fetchone()[0]
        finally:
            con.close()
    from app.database.sqlite_manager import working_database_path

    con = sqlite3.connect(working_database_path().resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchone()[0]
    finally:
        con.close()


# ------------------------------------------------- fake Azure OpenAI client
# TEST-ONLY stand-in for Chat Completions and the Responses API (create / retrieve / cancel). It
# never reaches the network; responses are built by a per-test handler.

# Isolated tests start from the stored data as of this time (see isolated_runtime).
TEST_DATA_CUTOFF = "2026-11-24T00:00:00+00:00"

# Served-model id the fake returns: the configured test model (LLM_MODEL=test-model) plus a version.
TEST_MODEL = "test-model-2026-01-01"


def completion(content, finish_reason="stop", usage=(1200, 900), status=None,
               model=TEST_MODEL, reasoning_tokens=300, response_id="resp_test"):
    """A Responses-API-shaped object. finish_reason 'length' -> incomplete."""
    usage_ns = (
        SimpleNamespace(
            input_tokens=usage[0],
            output_tokens=usage[1],
            total_tokens=usage[0] + usage[1],
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        if usage else None
    )
    if status is None:
        status = "completed" if finish_reason == "stop" else "incomplete"
    incomplete = (
        SimpleNamespace(reason="max_output_tokens" if finish_reason == "length" else finish_reason)
        if status == "incomplete" else None
    )
    output = [SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text=content or "")],
    )] if content is not None else []
    return SimpleNamespace(
        id=response_id,
        status=status,
        model=model,
        output=output,
        output_text=content or "",
        usage=usage_ns,
        incomplete_details=incomplete,
        error=None,
    )


class FakeAIClient:
    """Routes each create() to ``handler(kwargs, call_number)`` (tests only)."""

    def __init__(self, handler):
        self.requests = []
        self.cancelled = []
        self._stored = {}

        def create(**kwargs):
            self.requests.append(kwargs)
            response = handler(kwargs, len(self.requests))
            if getattr(response, "id", None) == "resp_test":
                response.id = f"resp_test_{len(self.requests)}"
            self._stored[response.id] = response
            return response

        def retrieve(response_id):
            return self._stored[response_id]

        def cancel(response_id):
            self.cancelled.append(response_id)
            stored = self._stored.get(response_id)
            if stored is not None:
                stored.status = "cancelled"
            return stored

        def chat_create(**kwargs):
            # Chat Completions request -> the Responses-shaped kwargs the handlers
            # read; the handler's Responses-shaped answer -> a chat completion.
            self.chat_requests.append(kwargs)
            return _as_chat(create(**_chat_to_responses_kwargs(kwargs)))

        self.chat_requests = []
        self.responses = SimpleNamespace(create=create, retrieve=retrieve, cancel=cancel)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=chat_create))


def _chat_to_responses_kwargs(kwargs):
    messages = kwargs["messages"]
    fmt = kwargs.get("response_format") or {}
    if fmt.get("type") == "json_schema":
        text = {"format": {"type": "json_schema", **fmt["json_schema"]}}
    else:
        text = {"format": dict(fmt)}
    out = {
        "model": kwargs["model"],
        "instructions": "\n\n".join(m["content"] for m in messages if m["role"] in ("system", "developer")),
        "input": [m for m in messages if m["role"] not in ("system", "developer")],
        "text": text,
        "max_output_tokens": kwargs.get("max_completion_tokens", kwargs.get("max_tokens")),
    }
    if "reasoning_effort" in kwargs:
        out["reasoning"] = {"effort": kwargs["reasoning_effort"]}
    if "temperature" in kwargs:
        out["temperature"] = kwargs["temperature"]
    return out


def _as_chat(response):
    """A Responses-shaped fake answer re-shaped as a Chat Completions answer."""
    import httpx
    import openai

    status = getattr(response, "status", "completed")
    if status == "failed":
        request = httpx.Request("POST", "https://insightiq-test.openai.azure.com/openai/v1/chat/completions")
        raise openai.InternalServerError("server error", response=httpx.Response(500, request=request), body=None)
    refusal = None
    text = ""
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                refusal = getattr(part, "refusal", None) or "refused"
            elif getattr(part, "type", None) == "output_text":
                text += getattr(part, "text", "") or ""
    text = text or getattr(response, "output_text", "") or ""
    finish = "stop"
    if status == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
        finish = "length" if reason == "max_output_tokens" else reason or "length"
    usage = getattr(response, "usage", None)
    chat_usage = None
    if usage is not None:
        chat_usage = SimpleNamespace(
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            completion_tokens_details=SimpleNamespace(
                reasoning_tokens=getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None)),
            prompt_tokens_details=SimpleNamespace(
                cached_tokens=getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", None)),
        )
    return SimpleNamespace(
        id=response.id, model=getattr(response, "model", None), usage=chat_usage,
        choices=[SimpleNamespace(finish_reason=finish,
                                 message=SimpleNamespace(content=text or None, refusal=refusal))],
    )


def payload_of(kwargs):
    return json.loads(kwargs["input"][-1]["content"])


def schema_name(kwargs):
    return kwargs["text"]["format"]["name"]


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


BRIEF_FILLER = (
    "The saved report records these observations for the stated periods, and the figures are "
    "listed exactly as stored by InsightIQ for this company without additional interpretation."
)


def respond_brief(kwargs, research_overrides=None):
    """A valid research brief for the request's evidence (tests only)."""
    evidence = payload_of(kwargs)["evidence"]
    sec = [o["evidence_id"] for o in evidence.get("sec_financial_observations") or []]
    news = [a["evidence_id"] for a in evidence.get("news_articles") or []]
    first = sec[:1] or ["METRIC_FINANCIALS"]
    summary = [
        {"section": "financial_position", "evidence_ids": first,
         "text": f"{BRIEF_FILLER} {BRIEF_FILLER}"},
        {"section": "evidence_limitations", "evidence_ids": [],
         "text": "The evidence covers a limited set of periods and cannot establish causes or outcomes."},
    ]
    research = [
        {"section": s, "evidence_ids": [ids], "text": BRIEF_FILLER}
        for s, ids in (("business_overview", "METRIC_FINANCIALS"), ("financial_performance", "METRIC_FINANCIALS"),
                       ("growth_analysis", "METRIC_GROWTH"), ("valuation_snapshot", "METRIC_RATIOS"),
                       ("key_risks", "METRIC_GROWTH"), ("outlook", "METRIC_FINANCIALS"))
    ]
    if news:
        research.insert(4, {"section": "recent_news", "evidence_ids": news[:1], "text": BRIEF_FILLER})
    for section, changes in (research_overrides or {}).items():
        for item in research:
            if item["section"] == section:
                item.update(changes)
    return {"summary_sections": summary, "research_sections": research}


def default_handler(kwargs, n):
    name = schema_name(kwargs)
    if name == "insightiq_financial_analysis":
        return completion(json.dumps(respond_financial(kwargs)))
    if name == "insightiq_news_analysis":
        return completion(json.dumps(respond_news(kwargs)))
    if name == "insightiq_strategic_analysis":
        return completion(json.dumps(respond_strategic(kwargs)))
    if name == "insightiq_research_brief":
        return completion(json.dumps(respond_brief(kwargs)))
    if name == "insightiq_news_selections":
        articles = payload_of(kwargs)["articles"]
        return completion(json.dumps({"selected_articles": [
            {"evidence_id": a["evidence_id"], "topic": "other_business_news"}
            for a in articles[:4]
        ]}))
    raise AssertionError("unexpected schema " + name)


def install_fake_ai(monkeypatch, handler=default_handler):
    """Route every Azure OpenAI request to one fake client (tests only)."""
    from app.services import ai_provider

    client = FakeAIClient(handler)
    monkeypatch.setattr(ai_provider, "default_client", lambda timeout=None: client)
    return client


def make_cache_stale(ticker, metric):
    """Test precondition: mark the isolated copy's cache entry as written by an
    earlier forecasting method version (the runtime snapshot may already hold a
    current-version cache)."""
    import json
    from app.services import forecast_cache

    path = forecast_cache.cache_path(ticker, metric)
    text = get_doc(path)
    if text is not None:
        envelope = json.loads(text)
        envelope["version"] = forecast_cache.CACHE_VERSION - 1
        put_doc(path, json.dumps(envelope))


def put_doc(path, text):
    """Store a document the way the application does (database on
    PostgreSQL, file on SQLite) - test setup only."""
    from app.services import document_store

    document_store.write_text(path, text)


def get_doc(path):
    """Read a stored document (database on PostgreSQL, file on SQLite)."""
    from app.services import document_store

    return document_store.read_text(path)
