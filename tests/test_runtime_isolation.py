"""Tests never receive real provider credentials and never write runtime artifacts."""
import os

from dotenv import dotenv_values

from conftest import ENV_FILE, PROJECT_ROOT, TEST_PROVIDER_ENV, runtime_artifact_hashes

PROVIDER_KEYS = ("AZURE_OPENAI_API_KEY", "NEWSDATA_API_KEY", "FINNHUB_API_KEY", "FMP_API_KEY")


def test_no_real_provider_credentials_in_the_test_process():
    real = dotenv_values(ENV_FILE)
    for key in PROVIDER_KEYS:
        assert os.environ.get(key) == TEST_PROVIDER_ENV[key], key
        value = (real.get(key) or "").strip()
        if len(value) >= 8:
            assert value not in os.environ.values(), f"{key} real value leaked into the test environment"


def test_runtime_artifacts_are_hashed_for_the_session_guard():
    hashes = runtime_artifact_hashes()
    assert any(k.startswith("reports") for k in hashes), "no saved reports found to guard"
    assert all(len(v) == 64 for v in hashes.values())


def test_isolated_runtime_uses_a_temporary_root(isolated_runtime):
    from app.core.config import get_settings
    from app.database.sqlite_manager import working_database_path

    root = os.path.realpath(str(get_settings().project_root))
    assert root != os.path.realpath(str(PROJECT_ROOT))
    assert os.path.realpath(str(working_database_path())) == os.path.realpath(str(isolated_runtime.db))


from research_v2_support import isolated_runtime  # noqa: E402,F401  (fixture import)
