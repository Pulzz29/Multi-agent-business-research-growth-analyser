"""
Observability, security hardening and maintenance on an isolated database.
All telemetry below is produced by the code under test; nothing is injected
into the metrics except where a rule's input is deliberately created.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from research_v2_support import FakeAIClient, completion, db_rows, isolated_runtime, open_db  # noqa: F401

postgres_only = pytest.mark.skipif(os.environ.get("DB_BACKEND") != "postgres",
                                   reason="telemetry tables need PostgreSQL")


@pytest.fixture
def api(isolated_runtime):
    from fastapi.testclient import TestClient

    from app.core.security import LIMITER
    from app.main import app

    LIMITER.reset()
    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as client:
        yield client, headers
    LIMITER.reset()


# ------------------------------------------------------------- pure units

def test_percentiles_are_interpolated_from_measured_values():
    from app.services.telemetry import percentile

    assert percentile([], 95) is None
    assert percentile([7], 50) == 7
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(1, 101)), 95) == pytest.approx(95.05)


def test_secrets_are_redacted_from_log_output(monkeypatch):
    from app.core.security import RedactingFilter, redact

    monkeypatch.setenv("DB_PASSWORD", "pg-super-secret-123")
    text = ("Authorization: Bearer abcdefghijklmnop password=pg-super-secret-123 "
            "url postgresql://app:hunter22@db/x api_key=XYZ12345 gsk_ABCDEFGHIJKLMNOPQRSTUV")
    cleaned = redact(text)
    for secret in ("abcdefghijklmnop", "pg-super-secret-123", "hunter22", "XYZ12345", "ABCDEFGHIJKLMNOPQRSTUV"):
        assert secret not in cleaned
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "key %s", ("pg-super-secret-123",), None)
    RedactingFilter().filter(record)
    assert "pg-super-secret-123" not in record.getMessage()


def test_rate_limiter_window():
    from app.core.security import RateLimiter

    limiter = RateLimiter()
    assert [limiter.check("c", 2, now=0)[0] for _ in range(3)] == [True, True, False]
    allowed, retry = limiter.check("c", 2, now=30)
    assert not allowed and 1 <= retry <= 31
    assert limiter.check("c", 2, now=60.5)[0] is True  # window passed


def test_tls_and_proxy_options(monkeypatch, tmp_path):
    from app.core.security import uvicorn_options

    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
    assert uvicorn_options()["forwarded_allow_ips"] == "10.0.0.0/8" and uvicorn_options()["proxy_headers"]
    monkeypatch.setenv("SSL_CERTFILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_KEYFILE", str(tmp_path / "missing.key"))
    with pytest.raises(RuntimeError):
        uvicorn_options()
    (tmp_path / "c.pem").write_text("x")
    (tmp_path / "k.pem").write_text("x")
    monkeypatch.setenv("SSL_CERTFILE", str(tmp_path / "c.pem"))
    monkeypatch.setenv("SSL_KEYFILE", str(tmp_path / "k.pem"))
    assert uvicorn_options()["ssl_certfile"].endswith("c.pem")


def test_temporary_file_cleanup_keeps_real_files(tmp_path):
    from app.services.cache_cleanup import cleanup_temporary_files

    runs = tmp_path / "reports" / "research_runs"
    runs.mkdir(parents=True)
    report = runs / "0be8200f-4906-498e-8c20-b5b507f0b553.json"
    report.write_text("{}")
    stale = runs / ("0be8200f-4906-498e-8c20-b5b507f0b553.json.tmp-" + "a" * 32)
    stale.write_text("partial")
    fresh = runs / ("x.json.tmp-" + "b" * 32)
    fresh.write_text("in progress")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    os.utime(report, (old, old))
    deleted = cleanup_temporary_files(root=tmp_path)
    assert [d.replace("\\", "/") for d in deleted] == [f"reports/research_runs/{stale.name}"]
    assert report.is_file() and fresh.is_file() and not stale.exists()


# --------------------------------------------------- traces (PostgreSQL)

@postgres_only
def test_spans_nest_and_correlate_database_statements(isolated_runtime):
    from app.database import db
    from app.services import telemetry

    with telemetry.span("workflow", "unit_test", run_id="run-1") as outer:
        with db.connect() as conn:
            conn.execute("SELECT COUNT(*) FROM companies").fetchone()
        with telemetry.span("forecast", "inner"):
            pass
        trace_id = telemetry.current_trace_id()
        outer["note"] = "measured"
    telemetry.flush()
    spans = telemetry.trace_detail(trace_id)["spans"]
    by_kind = {s["kind"]: s for s in spans}
    root = by_kind["workflow"]
    assert root["parent_span_id"] is None and root["run_id"] == "run-1"
    assert by_kind["db"]["name"] == "SELECT companies" and by_kind["db"]["parent_span_id"] == root["span_id"]
    assert by_kind["forecast"]["parent_span_id"] == root["span_id"]
    assert all(s["duration_ms"] >= 0 for s in spans)


@postgres_only
def test_model_call_and_validation_spans_carry_provider_tokens(isolated_runtime):
    from pydantic import BaseModel

    from app.services import ai_provider, telemetry
    from app.services.ai_provider import RequestProfile

    telemetry.install()

    class Tiny(BaseModel):
        answer: str

    def validate(parsed, profile):
        return parsed.answer

    client = FakeAIClient(lambda kw, n: completion(json.dumps({"answer": "ok"}), usage=(120, 30)))
    with telemetry.span("agent", "unit_agent"):
        ai_provider.structured_completion(
            agent="financial", call_name="t", schema_name="tiny", schema_model=Tiny,
            build_messages=lambda p: [{"role": "user", "content": "x"}],
            profiles=[RequestProfile("only", 200)], validate=validate, client=client, sleep=lambda s: None)
        trace_id = telemetry.current_trace_id()
    telemetry.flush()
    spans = telemetry.trace_detail(trace_id)["spans"]
    call = next(s for s in spans if s["kind"] == "model_call")
    assert call["input_tokens"] == 120 and call["output_tokens"] == 30
    assert call["attributes"]["model"] and call["attributes"]["outcome"] == "completed"
    assert any(s["kind"] == "validation" and s["status"] == "ok" for s in spans)
    usage = telemetry.token_usage(days=1)
    live = [m for m in usage["models"] if "live_trace" in m["sources"]]
    assert live and live[0]["input_tokens"] >= 120


@postgres_only
def test_api_request_trace_headers_audit_and_metrics(api):
    from app.services import telemetry

    client, headers = api
    response = client.get("/api/v1/markets/US/companies?search=app", headers=headers)
    assert response.status_code == 200
    trace_id = response.headers["X-Trace-Id"]
    for name in ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy", "Referrer-Policy"):
        assert name in response.headers
    assert "Strict-Transport-Security" not in response.headers  # plain HTTP
    client.post("/api/v1/markets/US/companies/search", json={"q": ""}, headers=headers)  # 422, audited
    telemetry.flush()
    spans = telemetry.trace_detail(trace_id)["spans"]
    api_span = next(s for s in spans if s["kind"] == "api")
    assert api_span["attributes"]["route"] == "/api/v1/markets/{market}/companies"
    assert any(s["kind"] == "db" and s["parent_span_id"] for s in spans)
    events = telemetry.audit_events()
    assert events[0]["action"] == "POST /api/v1/markets/{market}/companies/search"
    assert events[0]["outcome"] == "http_422"
    overview = client.get("/api/v1/operations/overview", headers=headers).json()
    kinds = {k["kind"]: k for k in overview["latency"]["by_kind"]}
    assert kinds["api"]["count"] >= 2 and kinds["api"]["p95_ms"] >= kinds["api"]["p50_ms"] > 0
    assert overview["runs"]["window_days"] == 30 and "by_status" in overview["runs"]
    detail = client.get(f"/api/v1/operations/traces/{trace_id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["spans"]
    assert client.get("/api/v1/operations/overview").status_code == 401


def test_https_requests_get_hsts(api):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, base_url="https://testserver")
    response = client.get("/health/live")
    assert response.headers.get("Strict-Transport-Security", "").startswith("max-age=")


def test_rate_limit_returns_429_with_retry_after(api, monkeypatch):
    client, headers = api
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3")
    codes = [client.get("/api/v1/markets", headers=headers).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    limited = client.get("/api/v1/markets", headers=headers)
    assert int(limited.headers["Retry-After"]) >= 1
    assert client.get("/health/live").status_code == 200  # health checks are never limited


@postgres_only
def test_alerts_open_persist_and_resolve(isolated_runtime):
    from app.services import telemetry

    now = datetime.now(timezone.utc)
    con = open_db(isolated_runtime.db)
    try:
        for i in range(3):
            con.execute(
                "INSERT INTO telemetry_spans(trace_id, span_id, kind, name, status, started_at, duration_ms) "
                "VALUES (?, ?, 'model_call', 'structured_completion', 'error', ?, 10)",
                ("a" * 32, f"s{i}", now - timedelta(minutes=5)))
        con.commit()
    finally:
        con.close()
    assert "model_call_failures" in telemetry.evaluate_alerts()
    telemetry.evaluate_alerts()
    alert = next(a for a in telemetry.alerts() if a["rule"] == "model_call_failures")
    assert alert["occurrences"] == 2 and alert["resolved_at"] is None
    assert "backup_not_verified" in {a["rule"] for a in telemetry.alerts()}  # no verified backup in this DB
    con = open_db(isolated_runtime.db)
    try:
        con.execute("DELETE FROM telemetry_spans")
        con.commit()
    finally:
        con.close()
    telemetry.evaluate_alerts()
    resolved = [a for a in telemetry.alerts(include_resolved=True) if a["rule"] == "model_call_failures"]
    assert resolved[0]["resolved_at"] is not None


@postgres_only
def test_telemetry_retention_never_touches_business_data(isolated_runtime, monkeypatch):
    from app.services import telemetry

    monkeypatch.setenv("TELEMETRY_RETENTION_DAYS", "7")
    old = datetime.now(timezone.utc) - timedelta(days=8)
    con = open_db(isolated_runtime.db)
    try:
        con.execute("INSERT INTO telemetry_spans(trace_id, span_id, kind, name, status, started_at, duration_ms) "
                    "VALUES (?, 'old', 'api', 'GET /x', 'ok', ?, 1)", ("b" * 32, old))
        con.commit()
    finally:
        con.close()
    before = {t: db_rows(isolated_runtime.db, f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
              for t in ("sec_facts", "news_articles", "research_execution_runs", "stored_documents")}
    assert telemetry.cleanup()["telemetry_spans"] >= 1
    for table, count in before.items():
        assert db_rows(isolated_runtime.db, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == count
