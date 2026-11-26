"""A. Project, dependency, configuration and path-portability checks."""
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import BACKEND, PROJECT_ROOT

APP = BACKEND / "app"
MODULES = sorted(
    ".".join(p.relative_to(BACKEND).with_suffix("").parts)
    for p in APP.rglob("*.py")
    if p.name != "__init__.py"
)


def test_python_syntax_compiles():
    for path in APP.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


@pytest.mark.parametrize("module", [m for m in MODULES if m != "app.server_entrypoint"])
def test_module_imports(module):
    importlib.import_module(module)


def test_required_files_present():
    required = [
        "backend/app/main.py", "backend/app/server_entrypoint.py",
        "backend/app/agents/research_workflow.py", "backend/app/agents/financial_agent.py",
        "backend/app/agents/news_agent.py", "backend/app/agents/strategic_agent.py",
        "backend/app/services/forecast_cache.py", "backend/app/services/forecasting.py",
        "backend/app/services/news_ingestion.py", "backend/app/services/sec_ingestion.py",
        "frontend/package.json", "frontend/package-lock.json", "frontend/vite.config.js",
        "frontend/src/App.jsx", "frontend/src/lib/api.js", "data/insightiq.sqlite3",
    ]
    missing = [r for r in required if not (PROJECT_ROOT / r).is_file()]
    assert not missing


def test_dependency_versions_match_lock():
    from importlib.metadata import version

    for line in (PROJECT_ROOT / "requirements.lock.txt").read_text().splitlines():
        if "==" in line:
            name, pinned = line.split("==")
            assert version(name.split("[")[0]) == pinned, name


def test_pip_check_clean():
    result = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_settings_load_and_project_root():
    from app.core.config import get_settings

    settings = get_settings()
    assert Path(settings.project_root).resolve() == PROJECT_ROOT
    assert settings.llm_model == os.environ["LLM_MODEL"]
    assert settings.azure_openai_endpoint.endswith("/openai/v1/")
    assert settings.llm_reasoning_effort in {None, "none", "minimal", "low", "medium", "high", "xhigh"}


def test_groq_is_fallback_only():
    """Groq is only the key/config fallback in ai_provider (via the openai SDK, no groq package)."""
    import importlib.util

    assert importlib.util.find_spec("groq") is None
    # core/security.py names GROQ_API_KEY only to redact its value from logs.
    allowed = {"core/config.py", "services/ai_provider.py", "agents/research_workflow.py",
               "services/executive_summary.py", "core/security.py"}
    offenders = {
        path.relative_to(APP).as_posix() for path in APP.rglob("*.py")
        if re.search(r"groq", path.read_text(encoding="utf-8"), re.I)
    }
    assert offenders <= allowed, offenders
    provider_code = (APP / "services" / "ai_provider.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+groq\b", provider_code, re.M)
    requirements = (PROJECT_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    assert "groq" not in requirements.lower() and "openai" in requirements.lower()


def test_no_colab_or_posix_only_code_left():
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "portability.py":
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in (r"google\.colab", r"/content/", r"^import fcntl", r'"/tmp/'):
            if re.search(pattern, text, re.M):
                offenders.append(f"{path.name}: {pattern}")
    assert not offenders


def test_startup_config_is_local():
    config = json.loads((PROJECT_ROOT / "data" / "server_startup_config.json").read_text(encoding="utf-8"))
    assert Path(config["project_root"]).resolve() == PROJECT_ROOT
    assert "/content" not in config["runtime_database_path"]


def test_missing_credentials_are_rejected(tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    clean = {k: v for k, v in os.environ.items()
             if k not in ("API_ACCESS_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
                          "AZURE_OPENAI_DEPLOYMENT", "NEWSDATA_API_KEY", "SEC_USER_AGENT")}
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "run_backend.py"), "--env-file", str(empty_env)],
        capture_output=True, text=True, env=clean, timeout=60)
    assert result.returncode != 0
    assert "API_ACCESS_KEY" in (result.stderr + result.stdout)


def test_invalid_sec_user_agent_rejected():
    from pydantic import ValidationError
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(SEC_USER_AGENT="bad", NEWSDATA_API_KEY="x", API_ACCESS_KEY="x")


def test_localize_stored_path():
    from app.core.portability import localize_stored_path

    legacy = "/content/drive/MyDrive/Multi-Agent-Business-Research/builds/20261122T173459146790Z"
    local = localize_stored_path(legacy + "/reports/research_runs/x.json", PROJECT_ROOT)
    assert local == (PROJECT_ROOT / "reports" / "research_runs" / "x.json").resolve()
    # Paths outside the legacy root are not rebased, so path-safety checks still reject them.
    other = localize_stored_path("/content/drive/MyDrive/elsewhere/x.json", PROJECT_ROOT)
    assert not str(other).startswith(str(PROJECT_ROOT))
    # Any earlier copy of the project (other machine/folder), no configuration needed.
    for stored in ("C:\\Users\\someone\\Documents\\InsightIQ\\reports\\research_runs\\y.json",
                   "/home/dev/old-copy/reports/research_runs/y.json"):
        assert localize_stored_path(stored, PROJECT_ROOT) == (
            PROJECT_ROOT / "reports" / "research_runs" / "y.json").resolve()
    backup = localize_stored_path("D:/old/InsightIQ/data/sqlite_backups/b.sqlite3", PROJECT_ROOT)
    assert backup == (PROJECT_ROOT / "data" / "sqlite_backups" / "b.sqlite3").resolve()
    # Only the project's own directories are anchors; traversal is never rebased.
    for stored in ("C:/elsewhere/secrets.json", "/etc/passwd",
                   "C:/x/reports/research_runs/../../../etc/passwd"):
        assert not localize_stored_path(stored, PROJECT_ROOT).is_relative_to(PROJECT_ROOT.resolve())


def test_exclusive_file_lock(tmp_path):
    from app.core.portability import exclusive_file_lock

    with (tmp_path / "x.lock").open("a+b") as handle:
        with exclusive_file_lock(handle):
            pass
