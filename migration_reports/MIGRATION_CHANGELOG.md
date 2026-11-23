# Migration changelog

Source of truth: the Google Drive export `20261122T173459146790Z-20261122T201912Z-1-001.zip` (243 entries, CRC-verified). An untouched, read-only copy is in `migration_inputs/drive_export_original/`, with per-file SHA-256 hashes in `migration_inputs/drive_export_SHA256.txt`. The exact unified diff of every source change is in [artifacts/source_changes.diff](artifacts/source_changes.diff) (58 changed lines).

## Application source changes (environment compatibility only)

| File | Change | Why | How functionality is preserved |
|---|---|---|---|
| `backend/app/core/portability.py` | **New.** `closing_connection`, `exclusive_file_lock`, `localize_stored_path` | Shared, documented helpers for the three Windows/local issues below | No business logic; each helper mirrors the original semantics |
| `backend/app/services/startup_recovery.py` | `import fcntl` removed; `fcntl.flock` replaced by `exclusive_file_lock`; `with sqlite3.connect(...)` → `with closing_connection(...)` (6×) | `fcntl` does not exist on Windows, so the import failed and the backend could not start. Open SQLite handles block `os.replace` on Windows. | Same exclusive, blocking startup lock (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere). Same commit/rollback behaviour, plus close. |
| `backend/app/services/sqlite_recovery.py` | `with sqlite3.connect` → `with closing_connection` (6×) | Backup and restore did `os.replace` / `unlink` while connections were still open, causing WinError 32 | Identical transaction semantics; connections are closed at block end |
| `backend/app/database/sqlite_manager.py` | Same (3×) | `sync_snapshot_to_drive` failed with WinError 32 during the offline replay | Same |
| `backend/app/main.py` | Report retrieval compares `localize_stored_path(stored_path)` with the expected path | Database rows store Colab paths (`/content/drive/.../reports/research_runs/<id>.json`), so every report returned 503 locally | The file actually read is still always `<project>/reports/research_runs/<run_id>.json`; identity checks (run_id, ticker, status) are unchanged; only the exact legacy Colab root is rebased |
| `backend/app/services/research_backup.py` | `Path(job["report_path"]).resolve()` → `localize_stored_path(..., root)` | Same Colab path in the job row | Same expected-path check |
| `backend/app/services/backup_reconciliation.py` | Stored `backup_path` / `manifest_path` localized | `data/job_backup_status/*.json` store Colab paths; startup reconciliation would fail closed and refuse to start | The "must be inside `data/sqlite_backups`" check, checksum and job audit are unchanged |

Not changed: agents, prompts, LangGraph workflow, Groq client and rate limiting, SEC and NewsData ingestion, normalization, SARIMAX forecasting, cache logic, quality gates, API contracts, authentication, frontend source. `financial_normalization.py` and `forecasting.py` are byte-identical: the forecast cache fingerprints, which hash these files, still match.

## Data and configuration changes

| Path | Change | Reversible? |
|---|---|---|
| `data/server_startup_config.json` | Rewritten for this computer (`project_root`, `runtime_database_path` under `%TEMP%\insightiq_sqlite\InsightIQ`) | Yes. The Colab version is kept as `data/server_startup_config.colab.json`. |
| `data/sqlite_backups/insightiq_20261122T202450795958Z_5282062e63.*` | **Added** one verified backup + manifest using the app's own `create_verified_backup()` | Yes (delete the 4 files). Needed because the newest Colab manifest recorded the `/tmp` runtime path, which fails the server's source-identity check. It also makes the newest recovery point include the 75-article snapshot rather than the 69-article one. |
| `%TEMP%\insightiq_sqlite\InsightIQ\insightiq.sqlite3` | Runtime working DB seeded from `data/insightiq.sqlite3` with the app's `restore_database_from_drive()`; row counts verified equal | Yes |
| `data/insightiq.sqlite3`, all reports, caches, journals, backup-status files | **Unchanged** (SHA-256 verified) | n/a |
| Database rows | **Not modified.** The Colab paths stored in rows are handled in code instead. | n/a |

## New project files

| File | Purpose |
|---|---|
| `.env.example` | Documents the variables the code requires |
| `.env` | Local secrets file: `API_ACCESS_KEY` generated; **provider keys blank, for you to fill in**. Not committed; ACL limited to your account. |
| `.env.test` | Offline test profile with deliberately invalid provider keys and its own access key |
| `.gitignore` | Excludes secrets, venv, node_modules, dist, data, reports, migration inputs |
| `requirements.lock.txt` | Exact versions recorded by the Colab run (notebook cell 7) |
| `scripts/run_backend.py` | Loads `.env` and runs the app's own `app.server_entrypoint` |
| `scripts/prepare_local_runtime.py` | Idempotent one-time local runtime preparation (above) |
| `scripts/start_all.ps1` | One-command start of both services |
| `scripts/db_baseline.py` | Read-only integrity, count and hash report |
| `tests/*` | 114 non-destructive tests (network-guarded) |
| `pytest.ini`, `.vscode/{settings,launch,tasks}.json`, `.claude/launch.json` | Tooling |
| `README.md` | Local quick start |

## Frontend

No source changes. `npm ci` was run from the existing lockfile. `npm run build` produced `index-rioCyEcP.js` and `index-rWEKL8wO.css`, **byte-identical** to the Colab build. The Vite proxy (`/api`, `/health` → `127.0.0.1:8000`) was already correct.

## 2026-11-23: BUG B1 fix

Backups first: `migration_inputs/pre_b1_fix_20261122T203839Z/` (read-only; source files, both databases, startup config, SHA256SUMS).

| File | Change |
|---|---|
| `backend/app/database/sqlite_manager.py` | + `create_recovery_point(reason)`: wraps the existing `create_verified_backup()`, never raises |
| `backend/app/services/news_ingestion.py` | Recovery point after a successful snapshot; + internal `recovery_point_verified` |
| `backend/app/services/sec_ingestion.py` | Same for SEC ingestion |
| `scripts/test_isolated_news_recovery.py`, `tests/test_b1_recovery.py` | New isolated recovery test + regression tests |

Diff: [artifacts/b1_fix.diff](artifacts/b1_fix.diff). The public API is unchanged.

## 2026-11-23: News Agent HTTP 413 fix and accurate agent statuses

Backups first: `migration_inputs/pre_413_fix_20261122T204748Z/`. Changed `backend/app/agents/news_agent.py` (token-aware evidence budget, 413 diagnostics) and `backend/app/services/research_jobs.py` (per-agent failure statuses from trace events). Added `scripts/measure_news_request.py` and `tests/test_news_agent_413.py`; documented the optional `INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET` in `.env.example`. Diff: [artifacts/news_agent_413_fix.diff](artifacts/news_agent_413_fix.diff). Full write-up: [NEWS_AGENT_413_FIX.md](NEWS_AGENT_413_FIX.md).
