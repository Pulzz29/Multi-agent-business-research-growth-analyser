# Test results (2026-11-23, Windows 11, Python 3.13.17, Node 24.19.0)

How to rerun: start the backend with `.env.test` and run `.venv\Scripts\python.exe -m pytest -q`.

All tests block non-loopback network access (autouse guard in `tests/conftest.py`). The backend under test used `.env.test`, whose provider keys are deliberately invalid. **No Groq, NewsData.io or SEC request was made.** No research job was submitted and no news refresh was triggered.

**Automated result (latest run, after the B1 and HTTP 413 fixes): 133 passed, 0 failed.** That is 114 migration tests + 3 B1 tests + 16 News Agent 413 tests. Earlier runs: 114/114, then 117/117. The run was repeated after a backend restart and after the line-ending fix. Raw results: [artifacts/pytest_results.txt](artifacts/pytest_results.txt) and [artifacts/pytest_junit.xml](artifacts/pytest_junit.xml).

| Area | Test(s) | Expected | Actual | Status |
|---|---|---|---|---|
| **A. Project & dependencies** | | | | |
| Syntax | `test_python_syntax_compiles` | all `backend/app` compiles | compiles | PASS |
| Imports | `test_module_imports` (31 modules) | every module imports on Windows | all import | PASS |
| Required files | `test_required_files_present` | 16 core files present | present | PASS |
| Dependency pins | `test_dependency_versions_match_lock`, `test_pip_check_clean` | Colab-recorded versions, no conflicts | match, clean | PASS |
| Config | `test_settings_load_and_project_root` | settings load, root = project | yes | PASS |
| Path portability | `test_no_colab_or_posix_only_code_left`, `test_startup_config_is_local`, `test_localize_stored_path` | no `google.colab`, `/content/`, `fcntl` or `/tmp/` left; legacy paths rebased only for the exact Colab root | as expected | PASS |
| Env handling | `test_missing_credentials_are_rejected`, `test_invalid_sec_user_agent_rejected` | refuses to start without keys; rejects a bad SEC identity | as expected | PASS |
| Lock | `test_exclusive_file_lock` | Windows lock works | works | PASS |
| **B. Backend API** | | | | |
| Startup | manual + log | startup recovery verified, uvicorn on 127.0.0.1:8000 | verified; `Database restored: False`, `Interrupted jobs reconciled: 0` | PASS |
| Health | `test_health_live`, `test_health_ready` | alive / ready, database available | yes | PASS |
| Companies | `test_companies` | 6 tickers | 6 | PASS |
| Dashboard | `test_dashboard_overview` | 200; `recent_limit` bounds enforced | 200 / 422 | PASS |
| Jobs & reports | `test_job_and_report_retrieval` | 6 jobs; 5 reports 200; failed job 409 | as expected | PASS |
| Validation | `test_job_id_validation`, `test_job_submission_validation` | 422 bad IDs / tickers / extra fields; 404 unknown | as expected | PASS |
| Backup health | `test_backup_health` | healthy, 5/5 verified | healthy, 5/5 | PASS |
| Observability | `test_observability`, `test_traces_for_saved_run` | 200 | 200 | PASS |
| **C. News** | | | | |
| Status per company | `test_news_status[×6]` | counts equal DB (4/3/11/16/23/18) | equal; timestamps present | PASS |
| Unsupported ticker | `test_news_status_unsupported`, `test_news_refresh_rejects_unsupported_ticker` | rejected before any provider call | 404/422 | PASS |
| Refresh auth | `test_post_routes_require_key` | 401 without or with a wrong key | 401 | PASS |
| Refresh button | browser | present on Research page | "Refresh MSFT news" shown | PASS |
| Cooldown / page limit | source review | 600 s cooldown, `max_pages=2`, single-flight lock | present in `main.py` | PASS (static) |
| Deduplication | source review | `normalized_article_url`, provider-id lookup and `INSERT OR IGNORE` in `news_ingestion.py` | present, unchanged | PASS (static) |
| No news deletion | `test_persistent_database_integrity_and_counts` | 75 articles | 75 | PASS |
| Live refresh | — | needs your approval | not run | NOT_RUN |
| **D. AI workflow** | | | | |
| Agents + LangGraph | `test_agent_modules_and_graph` | graph compiles with financial → news → strategic | yes | PASS |
| Saved report schema | `test_saved_report_schema[×5]` | all sections present, verified claims non-empty | yes | PASS |
| Evidence grounding | `test_saved_report_evidence_grounded_in_database[×5]` | every cited article id/URL and SEC fact id exists for that ticker | yes | PASS |
| Live research job | — | needs your approval (Groq) | not run | NOT_RUN |
| **E. Financial** | | | | |
| Normalization | `test_normalized_quarterly_financials[×6]` | ≥20 quarters per metric, evidence attached | yes | PASS |
| Forecast cache | `test_forecast_cache_valid_and_fingerprint_matches[×12]` | valid, hash OK, fingerprint = Colab | all 12 match | PASS |
| SARIMAX | `test_sarimax_recomputation_matches_cache` | local MSFT revenue forecast = cached (≤1e-6 rel.) | match, same model `sarimax_1` | PASS |
| **G. Security** | | | | |
| Auth | `test_missing_key_rejected[×7]`, `test_invalid_key_rejected[×7]`, `test_wrong_scheme_rejected` | 401 + `WWW-Authenticate: Bearer` | as expected | PASS |
| Docs disabled | `test_docs_disabled` | /docs, /redoc, /openapi.json → 404 | 404 | PASS |
| Error disclosure | `test_error_responses_do_not_leak_internals` | no traceback, SQLite or path text | none | PASS |
| Secret scan | script | no keys in project files or bundle | none found | PASS |
| npm audit | `npm audit` | — | 0 vulnerabilities | PASS |
| Python vuln scan | pip-audit | — | tool not installed | NOT_RUN |
| **H. Reliability** | | | | |
| Integrity / no loss | `test_persistent_database_integrity_and_counts`, `test_runtime_database_has_no_data_loss`, `test_all_backups_still_valid` | ok, counts unchanged | ok | PASS |
| Files unchanged | `test_report_and_cache_files_unchanged`, `test_original_export_untouched` | hashes equal | equal | PASS |
| Restart | manual: stop, start, full suite | recovery verified, no restore, 114 pass | as expected | PASS |
| **I. Performance** | | | | |
| API latency | `test_latency_repeated_requests` | p95 < 2 s | dashboard overview median ≈ 295 ms, p95 ≈ 347 ms | PASS |
| Frontend | curl + build | — | index 6 ms; JS bundle 256 KB (80 KB gzip), CSS 20 KB | PASS |
| **F. Frontend (browser)**: see [UI_REVIEW.md](UI_REVIEW.md) | | | | |
| Signed-out UI, themes, mobile, keyboard | built-in browser | render, no errors | as expected | PASS |
| Signed-in UI (Overview data, report display, Activity data) | built-in browser, throwaway access key (since revoked) | data renders | verified 2026-11-23 (see UI_REVIEW) | PASS |

## 2026-11-23 follow-up (B1 fix, discrepancy, env, signed-in UI, recovery)

| Area | Test | Expected | Actual | Status |
|---|---|---|---|---|
| B1 wiring | `test_ingestion_paths_create_recovery_points` | news and SEC ingestion create a recovery point after the snapshot | present | PASS |
| B1 failure safety | `test_recovery_point_failure_never_breaks_refresh` | a backup error returns `verified: False` and does not raise | as expected | PASS |
| B1 isolated recovery | `test_isolated_refreshed_news_survives_restoration` / `scripts/test_isolated_news_recovery.py` | refreshed news survives a runtime-DB wipe + controlled startup restore | with fix 77→77 (2/2 refreshed articles present); without fix 77→75 (defect reproduced) | PASS |
| Real DB untouched by isolated test | sha + counts | byte-identical to export, 75 articles, no fixture rows | as expected | PASS |
| 6 vs 7 jobs | scan of all 24 SQLite files + journal | identify discrepancy | 7th run `ddc47493…` (failed) is only in the journal (B7) | DONE (no records changed) |
| Running env file | process command line | known | `.env.test` (offline profile); `.env` has provider keys blank | DONE |
| Signed-in Overview | browser | live stats | 6 companies, 12,737 facts, 75 articles, 5 completed / 6 jobs, 6 recent runs, backups "Verified" | PASS |
| Signed-in report | browser | open saved report | run 5c401240: 4 financial claims (evidence expands: SEC concept/period/value), 2 news articles, 3 priorities (high/medium/low); all agent steps Completed | PASS |
| Research / Refresh News UI | browser | status per company; button present (not clicked) | MSFT 23 saved, last refresh Nov 23 1:47 AM IST; AAPL 4 saved after switching company | PASS |
| Signed-in Activity | browser | metrics | 8 spans, workflow p95 7.41 s, 5 verified backups, 6 runs, reliability "Insufficient Data" (small sample, expected) | PASS |
| Key revocation | curl | throwaway key rejected after restart | 401 | PASS |
| Restart with B1 code | log | startup recovery verified | verified, no restore, 0 interrupted | PASS |

## 2026-11-23: News Agent HTTP 413 fix

16 offline tests in `tests/test_news_agent_413.py`, all PASS: large, normal and empty collections; oversized single article rejected without truncation; relevance priority; evidence IDs preserved and excluded IDs not selectable; real-DB request within budget for all 6 tickers with field-identical content; configurable budget and invalid values; request identical to the pre-fix code when it fits; sanitized 413 diagnostics (limit 8000 / requested 19011 parsed, no article text or key); accurate statuses (financial completed, news failed, strategic pending) in an isolated DB. Full suite: **133 passed**. Report files and the primary DB are still identical to the Drive export. Details: [NEWS_AGENT_413_FIX.md](NEWS_AGENT_413_FIX.md).

## 2026-11-23: Controlled live research test (approved: one MSFT research job)

| Item | Result |
|---|---|
| Backend | Restarted on `.env` (live keys); startup recovery verified |
| Submission | Exactly one job via `scripts/run_one_research_job.py` (access key read from `.env` in-process, never printed; marker file `logs/live_research_submitted_MSFT.json` blocks re-submission) |
| Run | `9dd5df73-4ecb-4cd3-8c36-185e57a706e5`, MSFT, queued → running → **completed** in 5.95 s |
| Agents | financial completed (2.38 s) · news completed (2.14 s) · strategic completed (1.41 s) |
| Report | Saved and served (HTTP 200): 7 verified financial claims, 4 news articles (`NEWS_MSFT_71`, `_2`, `_11`, `_7`), 4 strategic priorities |
| News Groq request | 11,738 bytes; 9 of 12 candidates (3 excluded by budget, disclosed); **actual usage: 2,620 prompt + 720 completion = 3,340 tokens**; conservative estimate was 3,929 input tokens, budget 6,000 |
| HTTP 413 / 429 | None |
| NewsData.io / SEC calls | None (the research job reads stored data only) |
| Backups | New verified recovery point `insightiq_20261122T210004011875Z_e37c70b0a3` (12,737 facts / 75 articles / 7 runs / 6 completed); job backup status `verified`; backup health 6/6 |
| Data preservation | The 5 earlier reports are still byte-identical to the Drive export; SEC facts and news unchanged |
| Regression after the run | **135 passed** (133 + schema and grounding tests for the new report); the backup-health test now reads the expected count from the database instead of a fixed 5 |

## 2026-11-23: Controlled live news refresh (approved: one MSFT refresh)

| Item | Result |
|---|---|
| Method | `scripts/run_one_news_refresh.py`: the same `POST /api/v1/news/refresh` the UI button uses; key read from `.env` in-process; marker file blocks repetition |
| Response | HTTP 200 in 4.0 s · `status: success` · 20 fetched · **11 inserted** (ids 76–86) · MSFT 23 → 34 articles · total 75 → 86 |
| Provider limits honoured | `max_pages=2`, `max_articles=30`; 10-minute cooldown in force afterwards |
| Existing records | All 75 original articles unchanged (content hash identical before and after); no deletions |
| Deduplication | No duplicate introduced. One pre-existing cross-company article (ids 18 AAPL / 36 META) is allowed by `UNIQUE(provider, provider_article_id, company_ticker)` and was already in the Drive export |
| B1 fix, live | A new verified recovery point was created automatically: `insightiq_20261122T210317540939Z_22c47803a8` (86 articles, 7 runs); persistent snapshot 86; integrity ok |
| Refresh log | `source_refreshes` #15: success, fetched 20, inserted 11 |
| News Agent budget with the new data | 3 new descriptions of 10,600–12,200 chars. All 12 candidates would be ≈14,300 estimated input tokens, which would hit 413 again without the fix. With the fix: 9 articles, 8,362 bytes, ≈3,977 estimated tokens including completion |
| Regression | **136 passed**. Two snapshot tests now check that every original article and run is preserved exactly instead of expecting frozen counts (+1 new test), and the isolated recovery test compares against its starting count |
