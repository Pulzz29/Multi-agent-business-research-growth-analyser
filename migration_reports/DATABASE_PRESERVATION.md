# Database and saved-data preservation

Raw machine-readable evidence:
- [artifacts/baseline_original.json](artifacts/baseline_original.json): taken from the untouched export before any change
- [artifacts/baseline_after_migration.json](artifacts/baseline_after_migration.json): taken from the project after migration and all tests

## Backups taken before modification

1. The original ZIP was copied to `migration_inputs/` (SHA-256 in `migration_inputs/SHA256SUMS.txt`).
2. A full read-only extraction was made to `migration_inputs/drive_export_original/`, with 204 file hashes in `migration_inputs/drive_export_SHA256.txt`. Test `test_original_export_untouched` re-verifies every hash.

## Primary database `data/insightiq.sqlite3`

| Check | Original export | After migration |
|---|---|---|
| `PRAGMA integrity_check` | ok | ok |
| SHA-256 | identical | identical (byte-for-byte unchanged) |
| `sec_facts` | 12,737 | 12,737 |
| `news_articles` | 75 | 75 |
| `research_execution_runs` | 6 (5 completed, 1 failed) | 6 (5 completed, 1 failed) |
| `companies` | 6 | 6 |
| `source_refreshes` | 14 | 14 |
| `research_observability_events` | 12 | 12 |
| `reliability_monitor_evaluations` | 1 | 1 |
| `schema_migrations` | 3 | 3 |

The earlier reference counts were 12,737 SEC records, 73 news articles, 6 jobs and 5 reports. The export actually holds **75** articles, because the live MSFT refresh test (notebook cell 134) ran after the 73 figure was recorded.

SEC facts by company: AAPL 2,670 · AMZN 2,213 · GOOGL 1,252 · META 1,569 · MSFT 2,493 · NVDA 2,540.
News by company: AAPL 4 · AMZN 3 · GOOGL 11 · META 16 · MSFT 23 · NVDA 18.

## Research runs and saved reports

| run_id | Ticker | Status | Report file | Served by API locally |
|---|---|---|---|---|
| 6d8c549e-6bd2-4ecb-b5ce-7e4e0a0539cb | MSFT | failed | none | 409 (expected) |
| 1c5c8279-fd35-429d-8f5a-f4cd25a50815 | MSFT | completed | `reports/research_runs/1c5c8279-….json` | 200 |
| 9eca17cb-6d15-446c-a8a9-7f3977c746c0 | MSFT | completed | `reports/research_runs/9eca17cb-….json` | 200 |
| 0be8200f-4906-498e-8c20-b5b507f0b553 | MSFT | completed | `reports/research_runs/0be8200f-….json` | 200 |
| ced58401-2e98-4746-833b-5192e8365f3b | MSFT | completed | `reports/research_runs/ced58401-….json` | 200 |
| 5c401240-2996-43fb-803b-71af2c2a1028 | MSFT | completed | `reports/research_runs/5c401240-….json` | 200 |

The report and file hashes are listed under `files` in both baseline JSON files. **0 of 87 tracked files changed:** reports, forecast caches, job journals and job backup status files.

## Backups

All 24 SQLite files in the export pass integrity checks before and after: the primary DB, 16 in `data/backups/` and 7 in `data/sqlite_backups/`. One verified backup was **added** (`insightiq_20261122T202450795958Z_5282062e63`, made from the 75-article runtime DB). No backup was modified or deleted.

## Forecast cache

All 12 cache files (6 companies × revenue and operating cash flow) validate with the app's own `validate_cached_result`, and their `result_sha256` matches. Their source **fingerprint**, recomputed locally from the SQLite data plus the SHA-256 of `financial_normalization.py`, `forecasting.py` and `reports/financial_evidence_reconciliation_final.json`, equals the stored fingerprint for all 12. That proves the inputs and code are identical to Colab. The caches' 24-hour TTL will lapse and the app will then recompute them, which is the designed behaviour.

## File permissions

`data/insightiq.sqlite3` is accessible only to SYSTEM, Administrators and your account (normal Windows user-profile ACL). `.env` and `.env.test` have inheritance removed and grant read/write to your account.

## Runtime database note (designed behaviour)

The app runs on a working copy at `%TEMP%\insightiq_sqlite\InsightIQ\insightiq.sqlite3`. It snapshots that copy to `data/insightiq.sqlite3` after SEC or news ingestion and makes verified backups after research jobs. If Windows cleans `%TEMP%`, startup recovery restores the copy from the newest verified backup in `data/sqlite_backups`.
