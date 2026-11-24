"""Isolated BUG B1 recovery test: does refreshed news survive runtime-database restoration?

For each scenario a throwaway copy of data/ and reports/ is made in a temp directory, and
INSIGHTIQ_PROJECT_ROOT and TEMP point into it, so the real project database is never opened for
writing. The NewsData.io call is replaced by a local fixture (two clearly synthetic articles on
example.invalid); all non-loopback network access is blocked.

Steps per scenario (each in a fresh Python process):
  setup    - seed the isolated runtime DB from data/insightiq.sqlite3 and create a recovery point
             for the isolated runtime path (mirrors scripts/prepare_local_runtime.py)
  refresh  - run the real refresh_news_company() with the mocked provider page
  wipe     - delete the isolated runtime DB (simulates %TEMP% cleanup / new machine)
  restore  - run the real controlled-startup recovery, prepare_server_runtime()
  verify   - count articles and look for the refreshed fixture URLs in the restored DB

Scenarios: "with_fix" (current code) and "without_fix" (create_recovery_point disabled,
reproducing the pre-fix behaviour).

Usage: .venv\\Scripts\\python.exe scripts\\test_isolated_news_recovery.py [--keep]
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_URLS = [
    "https://example.invalid/insightiq-isolated-test/microsoft-cloud-revenue-update",
    "https://example.invalid/insightiq-isolated-test/microsoft-ai-partnership-expansion",
]


# ---------------------------------------------------------------- child process
def child(step, scenario):
    import socket

    root = Path(os.environ["INSIGHTIQ_PROJECT_ROOT"]).resolve()
    assert root != PROJECT_ROOT and str(root).startswith(str(Path(tempfile.gettempdir()).resolve().parent)), root

    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ConnectionRefusedError("isolated test: network blocked")
        return real_connect(self, address)

    socket.socket.connect = guarded
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))

    from app.database import sqlite_manager

    runtime = sqlite_manager.working_database_path().resolve()
    assert runtime.is_relative_to(root), f"runtime DB escaped isolation: {runtime}"
    backups = root / "data" / "sqlite_backups"
    out = {"step": step, "scenario": scenario}

    if step == "setup":
        assert sqlite_manager.restore_database_from_drive()["restored"]
        from app.services.sqlite_recovery import create_verified_backup

        create_verified_backup(root)
        config = {"version": 1, "project_root": str(root), "runtime_database_path": str(runtime),
                  "server_mode": "single_worker", "startup_recovery_required": True}
        (root / "data" / "server_startup_config.json").write_text(json.dumps(config), encoding="utf-8")

    elif step == "refresh":
        import asyncio
        from app.services import news_ingestion

        async def fake_get_page(self, client, params):
            return {"status": "success", "nextPage": None, "results": [
                {"article_id": "isolated-test-1", "link": FIXTURE_URLS[0], "source_name": "Isolated Test",
                 "title": "Microsoft reports cloud revenue growth in quarterly earnings update",
                 "description": "Microsoft revenue and earnings update (synthetic isolated-test fixture).",
                 "pubDate": "2026-11-23 06:00:00"},
                {"article_id": "isolated-test-2", "link": FIXTURE_URLS[1], "source_name": "Isolated Test",
                 "title": "Microsoft expands AI partnership and investment strategy",
                 "description": "Microsoft partnership and investment news (synthetic isolated-test fixture).",
                 "pubDate": "2026-11-23 07:00:00"},
            ]}

        news_ingestion.NewsDataClient.get_page = fake_get_page
        if scenario == "without_fix":
            news_ingestion.create_recovery_point = lambda reason: {"verified": False, "reason": "disabled"}

        result = asyncio.run(news_ingestion.refresh_news_company(
            "MSFT", max_articles=30, max_pages=1, force_refresh=True))
        out["refresh"] = {k: result.get(k) for k in (
            "status", "records_fetched", "records_inserted", "snapshot_verified", "recovery_point_verified")}

    elif step == "wipe":
        for suffix in ("", "-wal", "-shm"):
            Path(str(runtime) + suffix).unlink(missing_ok=True)
        out["runtime_exists"] = runtime.exists()

    elif step == "restore":
        from app.services.runtime_startup import prepare_server_runtime

        result = prepare_server_runtime(runtime, backups, root)
        out["startup"] = {k: result[k] for k in ("status", "database_restored", "verified_recovery_points")}

    elif step == "verify":
        with closing(sqlite3.connect(runtime.as_uri() + "?mode=ro", uri=True)) as con:
            out["news_articles"] = con.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
            out["runs"] = con.execute("SELECT COUNT(*) FROM research_execution_runs").fetchone()[0]
            out["sec_facts"] = con.execute("SELECT COUNT(*) FROM sec_facts").fetchone()[0]
            out["fixture_articles_present"] = con.execute(
                f"SELECT COUNT(*) FROM news_articles WHERE article_url IN ({','.join('?' * len(FIXTURE_URLS))})",
                FIXTURE_URLS).fetchone()[0]
            out["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        persistent = root / "data" / "insightiq.sqlite3"
        with closing(sqlite3.connect(persistent.as_uri() + "?mode=ro", uri=True)) as con:
            out["persistent_snapshot_news"] = con.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]

    print("RESULT " + json.dumps(out))


# ---------------------------------------------------------------- parent process
def run_scenario(scenario, base):
    root = base / f"iq_isolated_{scenario}"
    (root / "tmp").mkdir(parents=True)
    ignore = shutil.ignore_patterns("*-wal", "*-shm", "*.tmp")
    shutil.copytree(PROJECT_ROOT / "data", root / "data", ignore=ignore)
    shutil.copytree(PROJECT_ROOT / "reports", root / "reports", ignore=ignore)

    env = {k: v for k, v in os.environ.items() if not k.startswith("INSIGHTIQ_")}
    env.update({
        "INSIGHTIQ_PROJECT_ROOT": str(root), "TEMP": str(root / "tmp"), "TMP": str(root / "tmp"),
        "API_ACCESS_KEY": "isolated-test-access-key", "AZURE_OPENAI_API_KEY": "isolated-test-placeholder", "AZURE_OPENAI_ENDPOINT": "https://isolated-test.openai.azure.com/", "AZURE_OPENAI_DEPLOYMENT": "isolated-test-deployment",
        "NEWSDATA_API_KEY": "isolated-test-placeholder", "SEC_USER_AGENT": "InsightIQ-isolated-test test@example.invalid",
        # The copy lives at a different root: paths stored by the real project are rebased,
        # exactly as for a moved project folder (see BUG_REPORT B11). Legacy roots already
        # configured for this installation (e.g. the source PC's folder) are kept as well.
        "INSIGHTIQ_LEGACY_PROJECT_ROOTS": ";".join(filter(None, (
            os.environ.get("INSIGHTIQ_LEGACY_PROJECT_ROOTS", "").strip(), str(PROJECT_ROOT)))),
    })
    results = []
    for step in ("setup", "verify", "refresh", "verify", "wipe", "restore", "verify"):
        proc = subprocess.run([sys.executable, "-I", __file__, "--child", step, scenario],
                              capture_output=True, text=True, env=env, timeout=600)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        if proc.returncode != 0 or line is None:
            results.append({"step": step, "error": (proc.stderr or proc.stdout)[-1500:]})
            break
        results.append(json.loads(line[7:]))
    return results


def main():
    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        return child(sys.argv[i + 1], sys.argv[i + 2])

    base = Path(tempfile.mkdtemp(prefix="iq_b1_"))
    report = {}
    try:
        for scenario in ("with_fix", "without_fix"):
            report[scenario] = run_scenario(scenario, base)
    finally:
        if "--keep" not in sys.argv:
            shutil.rmtree(base, ignore_errors=True)

    out_arg = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--out=")), None)
    out = Path(out_arg) if out_arg else PROJECT_ROOT / "migration_reports" / "artifacts" / "isolated_news_recovery_test.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for scenario, steps in report.items():
        print(f"== {scenario}")
        for s in steps:
            print("  ", json.dumps({k: v for k, v in s.items() if k != "scenario"}))

    final_fix = report["with_fix"][-1]
    final_nofix = report["without_fix"][-1]
    start_fix = report["with_fix"][1].get("news_articles")
    start_nofix = report["without_fix"][1].get("news_articles")
    ok = (final_fix.get("fixture_articles_present") == 2 and final_fix.get("news_articles") == start_fix + 2
          and final_nofix.get("fixture_articles_present") == 0 and final_nofix.get("news_articles") == start_nofix)
    print("B1 ISOLATED RECOVERY TEST:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
