# InsightIQ deployment package: setup on another Windows PC

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

   This creates `.venv`, installs the pinned Python packages and the frontend packages, and creates `.env` with a new access key. `.env` also gets `INSIGHTIQ_LEGACY_PROJECT_ROOTS`, which lets paths recorded on the source PC resolve to this folder.
3. Open `.env` and fill in `GROQ_API_KEY`, `NEWSDATA_API_KEY` and `SEC_USER_AGENT` (an app name plus a contact email). Save the file.
4. Prepare the runtime database. This seeds it from `data\insightiq.sqlite3` and creates a verified recovery point for this PC:

   ```powershell
   .\.venv\Scripts\python.exe scripts\prepare_local_runtime.py
   ```

5. Start InsightIQ:

   ```powershell
   .\scripts\start_all.ps1
   ```

   Open http://127.0.0.1:5173 and enter `API_ACCESS_KEY` from `.env`.

## Checks

- `http://127.0.0.1:8000/health/ready` should return `"status":"ready"`.
- Offline test suite (with the backend running): `.\.venv\Scripts\python.exe -m pytest -q`. The API tests read the access key from `.env.test`; to use `.env` instead, set `INSIGHTIQ_TEST_ENV_FILE=.env` first.

## Notes

- Clicking **Start research** or **Refresh news** uses your Groq or NewsData.io quota.
- `deployment_manifest.json` lists every file's SHA-256, the database row counts and the research runs at packaging time.
- Documentation: `migration_reports\` (start with `MIGRATION_SUMMARY.md` and `LOCAL_STARTUP_GUIDE.md`).
