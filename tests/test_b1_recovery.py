"""BUG B1 regression tests: ingestion must leave a verified recovery point."""
import inspect
import json
import subprocess
import sys

from conftest import PROJECT_ROOT


def test_ingestion_paths_create_recovery_points():
    from app.services import news_ingestion, sec_ingestion

    for module, reason in ((news_ingestion, '"news_refresh:"'), (sec_ingestion, '"sec_refresh:"')):
        source = inspect.getsource(module)
        assert "create_recovery_point(" + reason in source, module.__name__
        assert '"recovery_point_verified": recovery_point["verified"]' in source


def test_recovery_point_failure_never_breaks_refresh(monkeypatch):
    from app.database import sqlite_manager
    from app.services import sqlite_recovery

    def boom(root):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(sqlite_recovery, "create_verified_backup", boom)
    result = sqlite_manager.create_recovery_point("news_refresh:MSFT")
    assert result == {"verified": False, "reason": "news_refresh:MSFT", "error_type": "OSError"}


def test_isolated_refreshed_news_survives_restoration():
    """Runs scripts/test_isolated_news_recovery.py (temp copy, mocked provider, no network)."""
    proc = subprocess.run(
        [sys.executable, "-I", str(PROJECT_ROOT / "scripts" / "test_isolated_news_recovery.py")],
        capture_output=True, text=True, timeout=1200)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    report = json.loads((PROJECT_ROOT / "migration_reports" / "artifacts"
                         / "isolated_news_recovery_test.json").read_text(encoding="utf-8"))
    with_fix, without_fix = report["with_fix"][-1], report["without_fix"][-1]
    start = report["with_fix"][1]
    assert with_fix["news_articles"] == start["news_articles"] + 2 and with_fix["fixture_articles_present"] == 2
    assert with_fix["sec_facts"] == 12737 and with_fix["runs"] == start["runs"] and with_fix["integrity"] == "ok"
    # Demonstrates the original defect: without the fix refreshed news is lost on restore.
    assert without_fix["fixture_articles_present"] == 0
    assert without_fix["persistent_snapshot_news"] == without_fix["news_articles"] + 2
