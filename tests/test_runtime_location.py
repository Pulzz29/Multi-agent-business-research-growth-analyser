"""
Runtime database location alignment across Windows sessions (offline).

Uses temporary directories only; the real runtime database, configuration
and backups are never touched (working path, backup creation and restore
are redirected to test doubles).
"""
import json
import sqlite3

import pytest

from app.services import runtime_location


def make_db(path, runs=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE research_execution_runs (run_id TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE sec_facts (id INTEGER PRIMARY KEY)")
    con.executemany("INSERT INTO research_execution_runs VALUES (?)", [(f"r{i}",) for i in range(runs)])
    con.commit()
    con.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.database import sqlite_manager
    from app.services import server_bootstrap, sqlite_recovery, startup_recovery

    current = tmp_path / "Temp4" / "insightiq.sqlite3"
    old = tmp_path / "Temp2" / "insightiq.sqlite3"
    config_path = tmp_path / "server_startup_config.json"
    config = {"version": 1, "project_root": str(tmp_path), "runtime_database_path": str(old)}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    calls = {"backup": 0, "restore": []}

    monkeypatch.setattr(sqlite_manager, "working_database_path", lambda: current)
    monkeypatch.setattr(sqlite_recovery, "create_verified_backup",
                        lambda root: (calls.__setitem__("backup", calls["backup"] + 1)
                                      or (tmp_path / "b.sqlite3", tmp_path / "b.manifest.json")))
    monkeypatch.setattr(server_bootstrap, "latest_backup_pair",
                        lambda d: (tmp_path / "backup.sqlite3", tmp_path / "backup.manifest.json", {}))

    def fake_restore(database_path, backup_path, manifest_path, reconcile_jobs=True):
        calls["restore"].append(str(database_path))
        make_db(database_path, runs=5)

    monkeypatch.setattr(startup_recovery, "prepare_runtime_database", fake_restore)
    return {"current": current, "old": old, "config_path": config_path, "config": config,
            "calls": calls, "root": tmp_path}


def align(env):
    return runtime_location.align_runtime_location(env["root"], env["config_path"], env["config"])


def test_session_change_copies_previous_database(env):
    make_db(env["old"], runs=3)
    result = align(env)
    con = sqlite3.connect(str(env["current"]))
    assert con.execute("SELECT COUNT(*) FROM research_execution_runs").fetchone()[0] == 3
    con.close()
    saved = json.loads(env["config_path"].read_text(encoding="utf-8"))
    assert saved["runtime_database_path"] == str(env["current"].resolve())
    assert result["config"]["runtime_database_path"] == str(env["current"].resolve())
    assert env["calls"]["backup"] == 1
    assert env["old"].exists()  # the previous database is never removed


def test_session_change_without_previous_database_restores_backup(env):
    result = align(env)
    assert env["calls"]["restore"] == [str(env["current"].resolve())]
    assert any("verified backup" in a for a in result["actions"])


def test_empty_file_is_moved_aside_not_deleted(env):
    env["current"].parent.mkdir(parents=True)
    con = sqlite3.connect(str(env["current"]))  # an early connection leaves an empty file
    con.execute("PRAGMA journal_mode=WAL").fetchall()
    con.close()
    make_db(env["old"], runs=1)
    result = align(env)
    moved = [p for p in env["current"].parent.iterdir() if ".incomplete-" in p.name]
    assert moved, result["actions"]
    assert "research_execution_runs" in runtime_location._tables(env["current"])


def test_existing_database_is_not_modified(env):
    make_db(env["current"], runs=7)
    make_db(env["old"], runs=1)
    before = env["current"].read_bytes()
    result = align(env)
    assert env["current"].read_bytes() == before
    assert "current session database already present (not modified)" in result["actions"]


def test_matching_path_is_a_no_op(env):
    make_db(env["current"], runs=1)
    env["config"]["runtime_database_path"] = str(env["current"])
    result = align(env)
    assert result["actions"] == [] and env["calls"]["backup"] == 0
