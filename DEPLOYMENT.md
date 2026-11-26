# InsightIQ deployment package: setup on another Windows PC

InsightIQ is deployed on Microsoft Azure as a complete cloud-hosted application (see the [root README](README.md#15-azure-deployment)). This guide sets up an additional local copy from the deployment package.

This package contains the complete application source, a consistent SQLite snapshot made with SQLite's backup API, saved research reports, the forecast cache, the verified recovery points, and configuration templates. **It contains no credentials**: no `.env`, no `.env.test`, and no API keys.

## Requirements

- Windows 10/11
- Python **3.13** (`py -3.13 --version`)
- Node.js 20 or newer
- Ports 8000 and 5173 free

## Setup (about 5 minutes)

1. Extract the ZIP to a short path, for example `C:\InsightIQ` or `Documents\InsightIQ`. Long paths can exceed the Windows path-length limit when `node_modules` is installed.
2. In PowerShell, from the extracted folder, run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote.ps1
   ```

   This creates `.venv`, installs the pinned Python packages and the frontend packages, and creates `.env` with a new access key. Report and backup paths recorded on another PC or folder are rebased onto this folder automatically (`INSIGHTIQ_LEGACY_PROJECT_ROOTS` is optional).
3. Open `.env` and fill in `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `LLM_MODEL`, `NEWSDATA_API_KEY` and `SEC_USER_AGENT` (an app name plus a contact email). `GROQ_API_KEY` and `GROQ_MODEL` are an optional fallback. Save the file.
4. Prepare the database. InsightIQ uses PostgreSQL (see `docs/POSTGRES_MIGRATION.md` for pgAdmin steps).
   1. Add the `DB_*` values to `.env`.
   2. Create the schema.
   3. Copy the packaged data into PostgreSQL, which validates every table.

   ```powershell
   .\.venv\Scripts\python.exe scripts\setup_postgres.py
   .\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py --source data\insightiq.sqlite3
   ```

   For the original SQLite mode (`DB_BACKEND=sqlite`), run `scripts\prepare_local_runtime.py` instead.

5. Start InsightIQ:

   ```powershell
   .\scripts\start_all.ps1
   ```

   Create login accounts once: `.\.venv\Scripts\python.exe scripts\provision_users.py <email> ...` (hidden password prompt; `--reset` rotates a password). Open http://127.0.0.1:5173 and sign in with email and password. `API_ACCESS_KEY` remains for server-to-server calls and tests only.

## Checks

- `http://127.0.0.1:8000/health/ready` should return `"status":"ready"`.
- Offline test suite (with the backend running): `.\.venv\Scripts\python.exe -m pytest -q`. The API tests read the access key from `.env.test`; to use `.env` instead, set `INSIGHTIQ_TEST_ENV_FILE=.env` first.

## Notes

- Clicking **Start research** or **Refresh news** uses your Azure OpenAI and NewsData.io quota.
- `deployment_manifest.json` lists every file's SHA-256, the database row counts and the research runs at packaging time.
- Documentation: `migration_reports\` (start with `MIGRATION_SUMMARY.md` and `LOCAL_STARTUP_GUIDE.md`).
