"""Shared fixtures for the InsightIQ local migration test suite.

Safety:
* every test runs with outbound network access blocked (loopback only), so no
  Azure OpenAI, NewsData.io, Finnhub, FMP, Yahoo Finance or SEC request can be made;
* only API_ACCESS_KEY (to call the locally running backend), SEC_USER_AGENT
  (a non-secret identity) and the local PostgreSQL connection values (DB_HOST,
  DB_PORT, DB_USER, DB_PASSWORD, DB_SSLMODE; used only for the separate test
  database "<DB_NAME>_test") are read from the env file. Every provider
  credential is replaced by an obviously fake test value, so a real key never
  enters the test process, its output or a test artifact.
API tests target the locally running backend (default http://127.0.0.1:8000).
"""
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "backend"
ENV_FILE = Path(os.getenv("INSIGHTIQ_TEST_ENV_FILE")
                or (PROJECT_ROOT / ".env.test" if (PROJECT_ROOT / ".env.test").is_file() else PROJECT_ROOT / ".env"))
ORIGINAL_EXPORT = PROJECT_ROOT / "migration_inputs" / "drive_export_original" / "20261122T173459146790Z"
requires_original_export = __import__("pytest").mark.skipif(
    not ORIGINAL_EXPORT.is_dir(), reason="original Drive export not present (deployment package)")
BASE_URL = os.getenv("INSIGHTIQ_TEST_BASE_URL", "http://127.0.0.1:8000")
FRONTEND_URL = os.getenv("INSIGHTIQ_TEST_FRONTEND_URL", "http://127.0.0.1:5173")
TICKERS = ("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA")
BASELINE = PROJECT_ROOT / "migration_reports" / "artifacts" / "baseline_original.json"

# Read only what tests genuinely need from the env file (see module docstring).
# Non-secret settings tests need: the access key for the local API, the SEC identity,
# and the legacy project roots used to rebase paths recorded on earlier machines.
ENV_KEYS_FROM_FILE = ("API_ACCESS_KEY", "SEC_USER_AGENT")
TEST_PROVIDER_ENV = {
    "AZURE_OPENAI_API_KEY": "TEST_ONLY_NOT_A_REAL_AZURE_KEY",
    "AZURE_OPENAI_ENDPOINT": "https://insightiq-test.openai.azure.com/openai/v1/",
    # Neutral test model name: the real model always comes from LLM_MODEL in .env.
    "LLM_MODEL": "test-model",
    "NEWSDATA_API_KEY": "TEST_ONLY_NOT_A_REAL_NEWSDATA_KEY",
    "FINNHUB_API_KEY": "TEST_ONLY_NOT_A_REAL_FINNHUB_KEY",
    "FMP_API_KEY": "TEST_ONLY_NOT_A_REAL_FMP_KEY",
}
_file_values = dotenv_values(ENV_FILE)
for key in ENV_KEYS_FROM_FILE:
    if _file_values.get(key) is not None:
        os.environ.setdefault(key, _file_values[key])
for key, value in TEST_PROVIDER_ENV.items():
    os.environ[key] = value
# A real Groq key in the shell must never turn a test's Azure failure into a live call,
# and LLM tuning values from the shell must not change test behaviour.
for key in (
    "GROQ_API_KEY", "GROQ_MODEL", "AZURE_OPENAI_DEPLOYMENT", "LLM_API", "LLM_TEMPERATURE",
    "LLM_MAX_OUTPUT_TOKENS", "LLM_TIMEOUT_SECONDS", "LLM_MAX_RETRIES", "LLM_REASONING_EFFORT",
    "LLM_EXPECTED_MODEL_PREFIX", "INSIGHTIQ_AZURE_REASONING_EFFORT",
    "INSIGHTIQ_AZURE_EXPECTED_MODEL_PREFIX", "INSIGHTIQ_AZURE_HTTP_TIMEOUT_SECONDS",
):
    os.environ.pop(key, None)
os.environ.setdefault("SEC_USER_AGENT", "InsightIQ tests test@example.invalid")

# ---- Test database --------------------------------------------------------
# PostgreSQL: the separate test database "<DB_NAME>_test" on the server from
# the env file (never the application database; created by
# scripts/setup_postgres.py --with-test-db). Each isolated test gets its own
# schema there. When PostgreSQL is unreachable (or INSIGHTIQ_TEST_DB_BACKEND=
# sqlite) the suite uses isolated SQLite copies, as before.
for key in ("DATABASE_URL", "DB_SCHEMA", "DB_BACKEND", "CACHE_TTL_HOURS", "DATA_REFRESH_TTL_HOURS",
            "CACHE_CLEANUP_INTERVAL_MINUTES"):
    os.environ.pop(key, None)
APP_DB_NAME = (_file_values.get("DB_NAME") or "").strip()
for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_SSLMODE"):
    if _file_values.get(key) is not None:
        os.environ[key] = _file_values[key]
TEST_DB_NAME = f"{APP_DB_NAME}_test" if APP_DB_NAME else ""
os.environ["DB_NAME"] = TEST_DB_NAME
del _file_values


def _postgres_test_database_available():
    if os.getenv("INSIGHTIQ_TEST_DB_BACKEND", "").strip().lower() == "sqlite" or not TEST_DB_NAME:
        return False
    try:
        import psycopg

        psycopg.connect(host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT") or 5432, dbname=TEST_DB_NAME,
                        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD") or None,
                        connect_timeout=3).close()
        return True
    except Exception:
        return False


TEST_DB_BACKEND = "postgres" if _postgres_test_database_available() else "sqlite"
os.environ["DB_BACKEND"] = TEST_DB_BACKEND
if TEST_DB_BACKEND == "postgres":
    assert TEST_DB_NAME.endswith("_test") and TEST_DB_NAME != APP_DB_NAME
    # Tests that are not isolated read the copy of the live runtime data.
    os.environ["DB_SCHEMA"] = "seed_live"
else:
    print("[tests] PostgreSQL test database unavailable: using isolated SQLite copies.")
os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))
sys.path.insert(0, str(BACKEND))

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in _LOOPBACK:
        raise ConnectionRefusedError(f"Test network guard blocked {host}")
    return _real_connect(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host not in _LOOPBACK and host is not None:
        raise socket.gaierror(f"Test network guard blocked DNS for {host}")
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)


@pytest.fixture(scope="session", autouse=True)
def seeded_test_database():
    """PostgreSQL only: copy the SQLite data into the test database once
    (re-seeded when the SQLite files change)."""
    if TEST_DB_BACKEND != "postgres":
        yield None
        return
    from research_v2_support import ensure_seed_schemas

    ensure_seed_schemas()
    yield TEST_DB_NAME
    from app.database import db

    db.close_pools()


@pytest.fixture(autouse=True)
def fresh_model_quirks():
    """Each test starts without model behaviour learned by an earlier test."""
    from app.services import ai_provider

    ai_provider.reset_model_quirks()
    yield
    ai_provider.reset_model_quirks()


@pytest.fixture(scope="session")
def api_key():
    key = os.getenv("API_ACCESS_KEY", "")
    assert key, "API_ACCESS_KEY missing from the test env file"
    return key


@pytest.fixture(scope="session")
def client():
    import httpx

    with httpx.Client(base_url=BASE_URL, timeout=30.0, trust_env=False) as c:
        yield c


class _SecretHeaders(dict):
    """Request headers that never print the access key (assertion messages,
    tracebacks and logs show a masked value)."""

    def __repr__(self):
        return "{'Authorization': 'Bearer ***'}"

    __str__ = __repr__


@pytest.fixture(scope="session")
def auth(api_key):
    return _SecretHeaders({"Authorization": f"Bearer {api_key}"})


def _redact_access_key(text):
    key = os.environ.get("API_ACCESS_KEY") or ""
    return text.replace(key, "[REDACTED_API_ACCESS_KEY]") if len(key) >= 8 else text


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # Safety net: a failure report (assertion introspection, captured output,
    # tracebacks) must never contain the local API access key.
    outcome = yield
    report = outcome.get_result()
    if report.longrepr is not None:
        text = str(report.longrepr)
        redacted = _redact_access_key(text)
        if redacted != text:
            report.longrepr = redacted
    report.sections = [(name, _redact_access_key(body)) for name, body in report.sections]


@pytest.fixture(scope="session")
def baseline():
    return json.loads(BASELINE.read_text(encoding="utf-8"))


# ------------------------------------------------ runtime contamination guard
# Saved reports, summaries, sector snapshots and forecast caches are genuine
# runtime artifacts. The whole test session must leave them byte-identical;
# otherwise the session fails (see tests/test_runtime_isolation.py).

GUARDED_GLOBS = (
    "reports/research_runs/*.json",
    "reports/research_runs/summaries/*.json",
    "reports/sector_comparisons/*.json",
    "data/forecast_cache_v*/*.json",
)


def runtime_artifact_hashes():
    import hashlib

    out = {}
    for pattern in GUARDED_GLOBS:
        for path in PROJECT_ROOT.glob(pattern):
            out[str(path.relative_to(PROJECT_ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def pytest_sessionstart(session):
    session.config._insightiq_artifacts_before = runtime_artifact_hashes()


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_insightiq_artifacts_before", None)
    if before is None:
        return
    after = runtime_artifact_hashes()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if changed:
        print("\nRUNTIME CONTAMINATION: tests changed genuine runtime artifacts:", changed[:20])
        session.exitstatus = 1
