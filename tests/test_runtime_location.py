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
    assert "current session database is the newest (not modified)" in result["actions"]


def test_matching_path_is_a_no_op(env):
    make_db(env["current"], runs=1)
    env["config"]["runtime_database_path"] = str(env["current"])
    result = align(env)
    assert result["actions"] == [] and env["calls"]["backup"] == 0



def make_dated_db(path, newest_update):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE research_execution_runs (run_id TEXT PRIMARY KEY, updated_at_utc TEXT)")
    con.execute("CREATE TABLE sec_facts (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO research_execution_runs VALUES ('r1', ?)", (newest_update,))
    con.commit()
    con.close()


def test_reused_session_folder_with_older_database_is_superseded_by_newer_backup(env, monkeypatch):
    """Regression (2026-11-24): the session temp folder switched back to a folder
    that still held an older database while newer runs existed only in the
    latest verified backup. The newest data must win; the older file is kept."""
    from app.services import server_bootstrap

    backup = env["root"] / "backup.sqlite3"
    make_dated_db(backup, "2026-11-23T17:58:25+00:00")
    make_dated_db(env["current"], "2026-11-23T12:00:00+00:00")
    monkeypatch.setattr(server_bootstrap, "latest_backup_pair",
                        lambda d: (backup, env["root"] / "backup.manifest.json", {}))
    result = align(env)
    assert env["calls"]["restore"] == [str(env["current"].resolve())]
    moved = [p for p in env["current"].parent.iterdir() if ".superseded-" in p.name]
    assert moved, result["actions"]  # the older database is moved aside, never deleted
    assert any("verified backup" in a for a in result["actions"])


def test_newer_current_database_is_kept_over_older_backup(env, monkeypatch):
    from app.services import server_bootstrap

    backup = env["root"] / "backup.sqlite3"
    make_dated_db(backup, "2026-11-20T10:00:00+00:00")
    make_dated_db(env["current"], "2026-11-24T09:00:00+00:00")
    before = env["current"].read_bytes()
    monkeypatch.setattr(server_bootstrap, "latest_backup_pair",
                        lambda d: (backup, env["root"] / "backup.manifest.json", {}))
    result = align(env)
    assert env["calls"]["restore"] == [] and env["current"].read_bytes() == before
    assert "current session database is the newest (not modified)" in result["actions"]


def test_latest_activity_reads_timestamps(tmp_path):
    path = tmp_path / "x.sqlite3"
    make_dated_db(path, "2026-11-23T17:58:25+00:00")
    assert runtime_location.latest_activity(path) == "2026-11-23T17:58:25+00:00"


def test_backup_verification_cache_reverifies_any_changed_file(tmp_path, monkeypatch):
    # The dashboard reads backup health on every request; verification is
    # cached only while both files are byte-for-byte the same file identity.
    from app.services import startup_recovery

    backup = tmp_path / "b.sqlite3"
    manifest = tmp_path / "b.manifest.json"
    backup.write_bytes(b"x" * 10)
    manifest.write_text("{}", encoding="utf-8")
    calls = []

    def fake_verify(b, m):
        calls.append(b)
        if b.read_bytes().endswith(b"!"):
            raise startup_recovery.StartupRecoveryError("Recovery backup checksum mismatch.")
        return {"backup_file": b.name}

    monkeypatch.setattr(startup_recovery, "_verify_backup_files", fake_verify)
    monkeypatch.setattr(startup_recovery, "_VERIFIED", {})
    startup_recovery.verified_backup(backup, manifest)
    startup_recovery.verified_backup(backup, manifest)
    assert len(calls) == 1                      # unchanged file: served from cache

    backup.write_bytes(b"x" * 10 + b"!")        # altered backup
    for _ in range(2):                          # failures are never cached
        with pytest.raises(startup_recovery.StartupRecoveryError):
            startup_recovery.verified_backup(backup, manifest)
    assert len(calls) == 3
