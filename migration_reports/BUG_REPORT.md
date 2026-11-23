# Bug report

## Fixed during migration (environment defects)

| ID | Severity | Defect | Fix |
|---|---|---|---|
| M1 | Critical | `startup_recovery.py` imports Linux-only `fcntl`, so the backend cannot import on Windows | Portable lock (`core/portability.py`) |
| M2 | High | SQLite connections opened with `with sqlite3.connect(...)` are never closed, so `os.replace`/`unlink` fail with WinError 32 during snapshot, backup and restore | `closing_connection` (same transaction semantics, plus close) |
| M3 | Critical | Stored Colab paths (`/content/drive/...`) in DB rows and `data/job_backup_status/*.json`: reports return 503 and startup reconciliation refuses to start | Exact-prefix rebasing of the legacy Colab root; DB rows untouched |
| M4 | Critical | `data/server_startup_config.json` and the newest manifest pinned Colab's `/tmp` runtime path, so the server refused to start | Local config written; one new verified backup created with the app's own function |
| M5 | High (data) | An empty runtime DB would be restored from the 69-article backup rather than the 75-article snapshot | Runtime DB seeded from `data/insightiq.sqlite3` |

## B1: Ingestion did not create a recovery point (FIXED, 2026-11-23)

**Severity:** High (silent data loss on recovery).

**Root cause:** news and SEC ingestion call `sync_snapshot_to_drive()`. That updates `data/insightiq.sqlite3` and copies the previous snapshot into `data/backups/`. But controlled startup (`server_bootstrap.bootstrap_api_database`) restores **only** from the newest verified manifest in `data/sqlite_backups/`, and those manifests were created only after completed research jobs. Any news refreshed after the last completed job was missing from the recovery point used at startup.

**Evidence the defect was real:** the Colab export shows it happened. Its newest `sqlite_backups` manifest (18:57 UTC) holds 69 articles, while `data/insightiq.sqlite3` holds 75 after the later refreshes.

**Fix** (backups taken first in `migration_inputs/pre_b1_fix_20261122T203839Z/`, read-only, with SHA-256 hashes; diff in [artifacts/b1_fix.diff](artifacts/b1_fix.diff), 45 added lines):
- `database/sqlite_manager.py`: new `create_recovery_point(reason)`. It calls the existing `sqlite_recovery.create_verified_backup()` (SHA-256 verified backup + manifest with table counts in `data/sqlite_backups/`). It never raises: a backup failure is logged and reported as `verified: False`, so it cannot turn an already-committed refresh into a failure. That matches the existing policy for research-job backups.
- `services/news_ingestion.py` and `services/sec_ingestion.py`: after a successful `sync_snapshot_to_drive(...)`, call `create_recovery_point("news_refresh:<T>")` / `("sec_refresh:<T>")`, and add `recovery_point_verified` to the internal result. The public API response of `/api/v1/news/refresh` is unchanged.
- No change to agents, prompts, forecasting, relevance filtering, deduplication, cooldown, page limits or stored records.

**Verification:**
- `scripts/test_isolated_news_recovery.py` works on a temp copy of `data/` and `reports/`, with a mocked provider and the network blocked. It runs the real `refresh_news_company()`, deletes the runtime DB, then runs the real `prepare_server_runtime()`.
  - **With the fix:** 75 → 77 articles, and **77 after restoration**, including both refreshed articles. SEC facts 12,737, runs 6, integrity ok, 5/5 recovery points verified.
  - **Without the fix** (helper disabled): 77 before the wipe, but **75 after restoration**. The refreshed articles are lost even though the persistent snapshot has 77. This reproduces B1.
- `tests/test_b1_recovery.py` (3 tests) is now part of the regression suite.
- The real database was not touched by the test: still byte-identical to the export, with no test articles.

**Confirmed live (2026-11-23):** the approved MSFT refresh automatically created a verified recovery point with all 86 articles.

**Operational note:** each successful refresh or SEC ingestion now adds about 6 MB to `data/sqlite_backups/`. The news refresh has a 10-minute cooldown. A retention policy would be a separate change (see B8).

## B7: The 7th research job exists only in the job journal (OPEN, decision needed)

**Finding:** run `ddc47493-9801-46e3-b51b-418bfac9536e` (MSFT, **failed**, `APIStatusError` / `research_execution_failed`, created 2026-11-22 20:17:40 UTC, all three agents failed within 2 s) has a journal file in `data/research_job_journal/`, but it is **absent from all 24 SQLite files** in the export.

**Why:** it was created 36 s after the last persistent snapshot (the 20:17:04 news refresh). Failed jobs trigger neither a snapshot nor a backup, so the row lived only in Colab's `/tmp` runtime DB, which was not part of the Drive export. Same class of gap as B1, for failed jobs.

**Impact:** no report was lost (the job failed before producing one). The dashboard shows 6 jobs locally, versus 7 in the Colab session. The journal file is preserved unchanged.

**Not done (needs your decision):**
1. Optional data repair: insert this single row into `research_execution_runs` from its journal record, with a pre-change backup and a verified recovery point. Nothing would be invented, since every field comes from the journal.
2. Optional code fix: also create a recovery point when a job reaches `failed`, or reconcile journal records missing from SQLite at startup.

## B9: News Agent HTTP 413 "request too large" (FIXED, 2026-11-23)

**Severity:** Critical (blocked every MSFT research run after the 20:17 refresh).
**Root cause:** stored article descriptions can hold full article text. One MSFT article holds 47,591 characters, which pushed the News Agent request from about 25.7 KB to 72.5 KB (≈19,000 requested tokens against an inferred 8,000 TPM limit).
**Fix:** a configurable, token-aware evidence budget (`INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET`, default 6,000 including the 1,200 completion tokens). Whole articles are chosen by relevance, then recency; nothing is truncated; the schema and evidence IDs are unchanged. MSFT is now 11.8 KB / ≤5,129 estimated tokens.
**Details:** [NEWS_AGENT_413_FIX.md](NEWS_AGENT_413_FIX.md). The 7th job (B7) failed with `APIStatusError` 2 s after start at 20:17:40 UTC, which fits this 413.

## B10: Failed jobs marked every agent as failed (FIXED, 2026-11-23)

**Severity:** Medium (misleading status). Statuses now come from recorded agent trace events (completed / failed / pending), so a News failure shows Financial completed and Strategic pending. See [NEWS_AGENT_413_FIX.md](NEWS_AGENT_413_FIX.md) §3.

## B11: Moving the project folder breaks stored-path checks (OPEN, low; workaround exists)

Jobs created locally store absolute paths (`C:\...\InsightIQ\reports\...`, `...\data\sqlite_backups\...`). If the folder is moved or renamed, startup reconciliation would treat those as outside the project and refuse to start. This was found when the isolated test copied the project after the live run. **Workaround:** set `INSIGHTIQ_LEGACY_PROJECT_ROOTS=<old folder path>` in `.env` (multiple paths separated by `;`), and stored paths will be rebased exactly as for the Colab root. A permanent fix (storing paths relative to the project root) would be a separate change.

## Remaining items (pre-existing behaviour, not changed)

| ID | Severity | Item |
|---|---|---|
| B2 | Low | Forecast cache TTL is 24 h; caches will be recomputed locally on the next research run (no API cost) |
| B3 | Low | `/dashboard/overview` ≈ 300 ms (recomputed per call) |
| B4 | Info | Servers bind to `127.0.0.1`; use `http://127.0.0.1:5173` |
| B5 | Low | No `<h1>` on pages (accessibility) |
| B6 | Info | The notebook is not self-contained; treat the local project as the source and put it under Git |
| B8 | Low | No retention policy for `data/sqlite_backups/` and `data/backups/`; disk use grows with each refresh or research backup |

## Not verified

- A live NewsData refresh and a Groq research job (awaiting your approval).
- The exact Groq account token limit. It is inferred (≈8,000 TPM) and will be recorded by the new 413 diagnostics if one recurs.
