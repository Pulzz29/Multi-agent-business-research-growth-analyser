# Environment audit (2026-11-23)

| Item | Found | Notes |
|---|---|---|
| OS | Windows 11 Home Single Language 10.0.26200, AMD64 | Not Linux like Colab, so POSIX-only code needs attention |
| Python (default) | 3.14.6 | Not used: Colab ran 3.13.18 |
| Python 3.13 | 3.13.17 via `py -3.13` | Used for the project venv to match Colab |
| Project venv | `InsightIQ/.venv` (Python 3.13.17) | Created; `pip check`: no broken requirements |
| Node.js / npm | v24.19.0 / 11.17.0 | Colab used Node 22.23.4 / npm 10.8.2; Vite 7 supports Node 24 |
| Git | 2.56.0.windows.2 | InsightIQ folder is not yet a Git repository |
| VS Code | 1.147.0, `code` CLI on PATH | Extensions: Python, Pylance, debugpy, Python Envs, ESLint |
| Disk | C: 231 GB free | Sufficient |
| Port 8000 | free | |
| Port 5173 | free | |
| Google Drive for Desktop | not installed / no synced drive | Drive build folder not reachable locally |

## Python dependencies installed (versions recorded by notebook cell 7)

fastapi 0.141.1, uvicorn[standard] 0.54.0, pydantic 2.14.2, pydantic-settings 2.x, httpx 0.28.1, tenacity 9.1.4, aiosqlite 0.22.1, numpy 2.4.6, pandas 2.2.3, scipy 1.16.3, statsmodels 0.15.0, python-dateutil 2.x, langgraph 1.2.19, langchain-core 1.7.4, groq 0.37.1, opentelemetry-api/sdk 1.42.1, opentelemetry-instrumentation-fastapi/httpx 0.63b1, pytest 8.4.2, pytest-asyncio 1.5.0. The pins are in `requirements.lock.txt`.

## Other local projects (not modified)

`Documents\Multi-Agent-Business-Research` contains a **different, earlier implementation** of the same business idea (Alembic migrations, `data/database/local_dev.db`, `api/routes/*`, `frontend/src/components/*`). It does not match the notebook's InsightIQ build: different module names, no `insightiq.sqlite3`, no `research_workflow.py`. It was inspected read-only and left untouched. InsightIQ is being migrated into a separate folder, `Documents\InsightIQ`, so the two cannot be mixed up.

## Windows compatibility issues found in the generated source

| # | Location | Issue | Planned minimal fix |
|---|---|---|---|
| W1 | `services/startup_recovery.py` (`import fcntl`) and dependants (`runtime_startup`, `backup_freshness`, `research_backup`, `backup_reconciliation`, `dashboard_data`, `main.py`) | `fcntl` is Linux-only, so importing it fails on Windows and the backend cannot start | Portable lock helper: `fcntl.flock` on POSIX, `msvcrt.locking` on Windows, same exclusive-lock semantics |
| W2 | `database/sqlite_manager.py` `sync_snapshot_to_drive` (and similar backup code) | `with sqlite3.connect(...)` commits but does **not close** the connection. Windows refuses `os.replace`/`unlink` on an open file (WinError 32). Linux allows it. | Wrap connections in `contextlib.closing(...)`. Behaviour is otherwise unchanged. |
| W3 | Working database in `tempfile.gettempdir()/insightiq_sqlite/<root name>` | Portable (resolves to `%TEMP%`), but `%TEMP%` can be cleaned. Server startup then restores from the newest verified backup in `data/sqlite_backups` (see BUG_REPORT B1). | Keep; document |
| W4 | Notebook startup cells 101/106 hardcode `/content/drive/...`, use `getpass`, Colab Secrets and `serve_kernel_port_as_window` | Colab-only | Replace with `.env`, `scripts/start_*.ps1` and the Vite dev proxy |
| W5 | Node `node_modules` deep paths | Windows MAX_PATH (260) hit when nested deep | Keep the project at a short path (`Documents\InsightIQ`) |

## Update (2026-11-23)

W1 and W2 are fixed (see MIGRATION_CHANGELOG.md). Two more local issues were found in the export's *data*: Colab paths stored in DB rows and backup-status files, and a `/tmp` runtime path in `server_startup_config.json` and the backup manifests. Both are handled without modifying database rows. Frontend: `npm ci` with Node 24.19.0 works. The build output is byte-identical to Colab's Node 22 build.
