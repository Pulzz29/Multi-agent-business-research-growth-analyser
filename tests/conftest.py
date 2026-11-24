"""Shared fixtures for the InsightIQ local migration test suite.

Safety:
* every test runs with outbound network access blocked (loopback only), so no
  Azure OpenAI, NewsData.io, Finnhub, FMP, Yahoo Finance or SEC request can be made;
* only API_ACCESS_KEY (to call the locally running backend) and SEC_USER_AGENT
  (a non-secret identity) are read from the env file. Every provider credential is
  replaced by an obviously fake test value, so a real key never enters the test
  process, its output or a test artifact.
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
ENV_KEYS_FROM_FILE = ("API_ACCESS_KEY", "SEC_USER_AGENT", "INSIGHTIQ_LEGACY_PROJECT_ROOTS")
TEST_PROVIDER_ENV = {
    "AZURE_OPENAI_API_KEY": "TEST_ONLY_NOT_A_REAL_AZURE_KEY",
    "AZURE_OPENAI_ENDPOINT": "https://insightiq-test.openai.azure.com/openai/v1/",
    "AZURE_OPENAI_DEPLOYMENT": "gpt-5.4-pro",
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
os.environ.setdefault("SEC_USER_AGENT", "InsightIQ tests test@example.invalid")
del _file_values
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
