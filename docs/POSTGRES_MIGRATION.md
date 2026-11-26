# PostgreSQL database and 24-hour company cache

InsightIQ stores its data in PostgreSQL. Company data is cached in the database for 24 hours. The original SQLite database is kept, unchanged, for rollback.

## 1. Architecture

| | Before | Now |
|---|---|---|
| Engine | One SQLite file, copied to `%TEMP%\insightiq_sqlite\…` at startup | Any PostgreSQL-compatible server (local PostgreSQL 16 today) |
| Access | Raw `sqlite3` in about 50 modules, with no pooling | One access layer, `backend/app/database/db.py`: psycopg 3 with a connection pool, timeouts and SSL options. Existing calls keep `with connect() as conn` |
| Schema | `CREATE TABLE IF NOT EXISTS` statements in 7 modules | Versioned SQL migrations in `backend/app/database/migrations/` (recorded in `db_schema_migrations`, applied once even when several instances start together) |
| Backups | SQLite file snapshots and verified file copies | `pg_dump` / `pg_restore` (section 6). The file mechanisms run only with `DB_BACKEND=sqlite` |
| Company freshness | 6-hour check, in-process locks | 24-hour cache in `company_data_cache` with a database lease (safe across instances) and a periodic cleanup |

**Tables.** All 21 original tables keep the same columns, primary keys, unique constraints, foreign keys, CHECKs and indexes.
- **Column types are unchanged in meaning.** Timestamps stay UTC ISO-8601 text and JSON stays text, exactly as the application reads them, so API responses do not change. SQLite `AUTOINCREMENT` ids become identity columns.
- **Index changes.** Two lookup indexes were added: `source_refreshes(company_ticker, source, started_at)` and `research_execution_runs(ticker, created_at_utc)`. A redundant index on the primary key of `reliability_monitor_evaluations` was not recreated.
- **New table, `company_data_cache`:**

| Column | Meaning |
|---|---|
| `cache_kind`, `cache_key` | Primary key. `company` + `US:ORCL`, or `search` + `US:oracle` |
| `market`, `symbol` | Normalised upper case |
| `status` | `ready` or `pending` |
| `payload_json` | The complete result |
| `fetched_at`, `expires_at` | TIMESTAMPTZ, UTC |
| `refresh_lease_until`, `refresh_owner` | Who is fetching, and until when |
| `last_error`, `created_at`, `updated_at` | Bookkeeping |

A CHECK constraint ensures a `ready` row always has a payload and `expires_at > fetched_at`. Indexes: `expires_at`, and `(market, symbol)`.

## 2. Caching workflow (company page and provider search)

1. **The request is normalised.** The market and symbol are trimmed and upper-cased, giving for example `US:ORCL`. A search text is case-folded with single spaces.
2. **A valid row is returned straight away.** A row is valid when `status = 'ready'` and `expires_at > now`. Nothing is fetched: no provider, AI or recompute work. Reading a row never changes `expires_at`.
3. **Data fetched within the last 24 hours is served too.** This covers data that has a recent `last_refreshed_at` but no cache row (for example data migrated from SQLite). It is served from the database and cached until `last_refreshed_at + 24 h`.
4. **Otherwise the data is fetched:**
   - **Lease:** the request takes the fetch lease with one atomic `UPDATE`. Other requests for the same company, in this or any other instance, wait for that result instead of fetching again.
   - **Fetch:** the existing pipeline runs (profile, quote, statements and news).
   - **Store:** only a complete, successful result is stored, with `fetched_at = now` and `expires_at = now + CACHE_TTL_HOURS`.
5. **A failed refresh is never cached.**
   - The lease is released and the error is kept in `last_error`.
   - The page shows the last stored data marked `stale: true` with the error. It is never presented as fresh.
   - The next request tries again.
6. **Expiry is exact.** Data is invalid exactly `CACHE_TTL_HOURS` after its fetch. For example, data fetched at 08:00:00 is expired at 08:00:00 the next day.
7. **Cleanup:**
   - A background thread in every API process runs `DELETE … WHERE expires_at <= now()` every `CACHE_CLEANUP_INTERVAL_MINUTES`.
   - It also deletes abandoned `pending` rows.
   - The delete is idempotent, so running several instances at once is safe, and no cloud scheduler is needed.
   - It never touches companies, market data, news, SEC facts, research runs or any other table.
8. **Unchanged behaviour:**
   - The **Refresh** button still forces a fetch, with its cooldown, and then renews the cache.
   - Research runs keep their own per-source refresh rules.
   - There are no user accounts. All data is shared, behind the existing `API_ACCESS_KEY` check, which is unchanged.

## 3. `.env` values

```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=capstone_project        # your database (the template uses company_db)
DB_USER=postgres
DB_PASSWORD=<your PostgreSQL password>
DB_SSLMODE=prefer               # require (or verify-full) for any remote server
CACHE_TTL_HOURS=24
CACHE_CLEANUP_INTERVAL_MINUTES=5
```

`DATABASE_URL=postgresql://user:password@host:5432/db?sslmode=require` overrides the `DB_*` values. Optional settings (pool size and timeouts, statement timeout, `DB_SCHEMA`, `DB_SSLROOTCERT`) are listed in `.env.example`.

## 4. Local PostgreSQL with pgAdmin

1. **Install PostgreSQL 16 and pgAdmin 4.** Use the EDB installer from postgresql.org. Remember the password you set for the `postgres` user, and keep port 5432.
2. **Start pgAdmin and connect.** Open **Servers → PostgreSQL 16** and enter the password.
3. **Create the database.** Right-click **Databases → Create → Database…**, enter the name `capstone_project` (or the `DB_NAME` you chose), keep the owner `postgres`, and click **Save**. Alternatively, `scripts\setup_postgres.py` creates it for you.
4. **Optional: a dedicated login instead of `postgres`.**
   - Go to **Login/Group Roles → Create**.
   - Enter a name, set a password, and under **Privileges** enable **Can login?**.
   - In the database's **Security** tab, grant it CONNECT, CREATE and TEMP.
   - Put that user in `DB_USER` and `DB_PASSWORD`.
5. **Put the values in `.env`** (section 3).
6. **Find the data in pgAdmin:** **capstone_project → Schemas → public → Tables**.
   - For the cache, right-click `company_data_cache` → **View/Edit Data → All Rows**. `expires_at` is always exactly 24 hours after `fetched_at`.
   - For the applied migrations, open `db_schema_migrations`.
7. **Check that the service is running.** Open Windows **Services**, find `postgresql-x64-16`, and confirm its status is *Running*.

## 5. Commands (PowerShell, in the project folder)

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt   # adds psycopg + psycopg-pool
.\.venv\Scripts\python.exe scripts\setup_postgres.py --with-test-db     # create DB (+ test DB), apply migrations
.\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py        # copy SQLite data, validate, write evidence
.\.venv\Scripts\python.exe scripts\run_backend.py                       # start the API (migrations run at startup)
cd frontend; npm run dev                                                # start the UI -> http://127.0.0.1:5173
```

**What the migration script does:**
- **Picks the source** with the newest recorded activity: the runtime working copy, or `data\insightiq.sqlite3`.
- **Backs it up** to `data\backups\pre_postgres_<UTC>.sqlite3`, checking its integrity.
- **Copies every table** with `ON CONFLICT DO NOTHING`, so running it again never creates duplicates.
- **Compares** row counts and a SHA-256 checksum of every row, then writes `docs\validation_evidence\postgres_migration_<UTC>.json`.
- **Exits with code 0** only when every table matches.

**Tests:** run `.\.venv\Scripts\python.exe -m pytest`. They use the separate database `<DB_NAME>_test`, with one throwaway schema per isolated test, and never the application database. Without a reachable test database, they fall back to isolated SQLite copies.

## 5b. Documents, backups and operations (added later)

- **Documents in PostgreSQL.** Saved research reports with their summary and insight sidecars, saved sector comparisons, research-job journals and forecast caches are stored in `stored_documents`.
  - They are keyed by their former relative path, and SHA-256 is checked on every read.
  - Run `scripts\migrate_files_to_postgres.py` to import the files. It is idempotent and verifies each file against the database. The files stay on disk as the pre-migration archive.
- **Backups:** `scripts\backup_postgres.py` takes a `pg_dump` from an exported snapshot.
  - **Verification:** it restores the dump into a temporary database, compares every table's row count, and drops that database.
  - **Retention:** the newest `BACKUP_RETENTION_COUNT` dumps are kept, and none older than `BACKUP_RETENTION_DAYS`.
  - **Scheduling:** `scripts\register_backup_task.ps1` registers a daily Windows task; on AKS the CronJob in `deploy/aks/insightiq.yaml` does the same.
- **Operations** (Activity page → Operations, or `/api/v1/operations/*`):
  - correlated traces (API, database statements, calculations, forecasts, agents, model calls, validation, PDF);
  - p50/p95 latency;
  - research-run metrics;
  - provider-reported token usage;
  - persistent alerts and audit events.

## 6. Backup and rollback

**Back up PostgreSQL** (with `C:\Program Files\PostgreSQL\16\bin` on PATH):
```powershell
New-Item -ItemType Directory -Force pg_dumps | Out-Null
pg_dump -h localhost -U postgres -d capstone_project -F c -f pg_dumps\insightiq_$(Get-Date -Format yyyyMMddTHHmmss).dump
```

**Restore into a new, empty database:**
```powershell
createdb -h localhost -U postgres capstone_restore
pg_restore -h localhost -U postgres -d capstone_restore pg_dumps\<file>.dump
```
Alternatively, in pgAdmin right-click the database and choose **Backup…** or **Restore…**.

**Roll back to SQLite:**
1. Set `DB_BACKEND=sqlite` in `.env`.
2. Restart the backend.

It then uses the original SQLite working database, which was never modified or deleted. A verified copy also exists in `data\backups\pre_postgres_*.sqlite3`. Changes made only in PostgreSQL after the migration are not in the SQLite file.

**Re-migrate from scratch** (destructive, so only on purpose):
1. In pgAdmin, drop and recreate the database.
2. Run `setup_postgres.py`.
3. Run `migrate_sqlite_to_postgres.py`.

## 7. Cloud readiness (no Azure service is assumed)

- **Configuration:**
  - All connectivity comes from `DATABASE_URL` or `DB_*`. There are no hosts, ports or credentials in code, and no Azure SDK.
  - The layer works with any PostgreSQL-compatible managed service or container.
- **Prerequisites on the chosen service:**
  - PostgreSQL 12 or later (16 is tested).
  - A database and a login with CONNECT and CREATE on the schema.
  - TLS: `DB_SSLMODE=require`, or `verify-full` with `DB_SSLROOTCERT`.
  - The application host allowed through the firewall or private network.
  - Credentials provided by the platform's secret store as environment variables, never committed.
- **Several instances:**
  - Migrations are protected by an advisory lock.
  - Company fetches are coordinated through the cache lease, and cleanup is idempotent.
  - Size `DB_POOL_MAX_SIZE` times the number of instances below the server's connection limit, or use a pooler such as PgBouncer in transaction mode.
  - Note: today `run_backend.py` starts one uvicorn worker bound to 127.0.0.1; a cloud deployment needs its own process manager and host binding.
- **Releases:**
  - Run `scripts\setup_postgres.py`, or simply start the app, once per release so migrations apply before traffic.
  - Run the data migration once, from a machine that has the SQLite file.
- **File-based features to replace or keep:**
  - Research report JSON files, the forecast cache and the job journal still live under the project's `reports\` and `data\` folders.
  - A multi-instance cloud deployment needs shared storage for them.
  - These were not moved to the database, which keeps behaviour and response formats unchanged.
- **Another engine** (for example SQL Server or MySQL) is not automatically compatible. It would need:
  - a driver and connection branch in `db.py`, with its placeholder style;
  - translated migrations (identity, `TIMESTAMPTZ`, `ON CONFLICT`, `RETURNING`, `excluded.`);
  - new `json_text`, `table_exists` and `table_columns` helpers;
  - the advisory lock in `migrations/__init__.py` replaced;
  - the whole test suite re-run.

## 8. Changed and added files

**Added:**
- `backend/app/database/db.py`
- `backend/app/database/migrations/` (`__init__.py`, `0001_initial.sql`, `0002_company_cache.sql`)
- `backend/app/database/sqlite_to_postgres.py`
- `backend/app/services/company_cache.py`
- `backend/app/services/cache_cleanup.py`
- `scripts/setup_postgres.py`
- `scripts/migrate_sqlite_to_postgres.py`
- `tests/test_postgres_layer.py`, `tests/test_sqlite_to_postgres_migration.py`, `tests/test_company_cache.py`
- This document

**Modified:**
- **Configuration:** `backend/app/core/config.py` (database and cache settings).
- **Database:** `backend/app/database/sqlite_manager.py` (delegates to `db.py`; the file features run only on SQLite).
- **Startup:**
  - `backend/app/server_entrypoint.py` (PostgreSQL startup: migrations, then interrupted-job reset);
  - `backend/app/services/startup_recovery.py`;
  - `backend/app/services/runtime_startup.py` (backup health on PostgreSQL).
- **API:** `backend/app/main.py` (health check, cleanup start and stop, search cache, 503 for database outages).
- **Company data:** `backend/app/services/company_data.py` (cache flow); `backend/app/services/data_freshness.py` (expired exactly at the TTL).
- **Services ported to portable SQL:** `market_storage`, `market_ingestion`, `news_ingestion`, `sec_ingestion`, `observability`, `reliability_monitoring`, `research_jobs`, `sec_coverage`.
- **Configuration files:** `backend/requirements.txt`, `.env.example`, `.gitignore`.
- **Scripts:** `scripts/run_backend.py`, plus the SQLite-only scripts, which are now labelled.
- **Tests:** test fixtures and the affected tests.
