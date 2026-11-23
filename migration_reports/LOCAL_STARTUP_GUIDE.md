# Local startup guide (Windows, PowerShell)

Project folder: `Documents\InsightIQ`. Run commands from that folder.

## 1. One-time: credentials

Open `.env` and fill in the three blank values. `API_ACCESS_KEY` is already generated.

```
GROQ_API_KEY=...
NEWSDATA_API_KEY=...
SEC_USER_AGENT=InsightIQ research you@example.com
```

Until then, you can run everything except live research and news refresh with the offline profile `.env.test`.

## Which environment file is used

| File | Purpose | Status (2026-11-23) |
|---|---|---|
| `.env` | **Real use.** Default for `scripts\run_backend.py`, `start_all.ps1`, the VS Code "Backend (FastAPI, .env)" launcher, and Python's `envFile`. | `API_ACCESS_KEY` set; `GROQ_API_KEY`, `NEWSDATA_API_KEY`, `SEC_USER_AGENT` **blank**: fill these in before any live call |
| `.env.test` | Offline testing only. Provider keys are deliberately invalid placeholders, so no live call can succeed. | Used by the backend **currently running** |

Rules: never commit either file (both are git-ignored); keep their ACL limited to your account; never paste keys into chat or reports. Values already set in the real environment take precedence over the file (`load_dotenv(override=False)`). To see which file a running backend uses, check its command line for `--env-file`; no flag means `.env`.

To switch to live mode: stop the backend, fill in `.env`, then run `.\.venv\Scripts\python.exe scripts\run_backend.py`. In the browser, use the `API_ACCESS_KEY` from `.env`.

## 2. Start

Option A, one command (opens two windows):

```powershell
.\scripts\start_all.ps1
```

Offline profile:

```powershell
.\scripts\start_all.ps1 -EnvFile .env.test
```

Option B, VS Code: open the folder, then either **Run and Debug → "InsightIQ: Full stack"**, or **Terminal → Run Task → "InsightIQ: Start full stack"**.

Option C, manual (two terminals):

```powershell
.\.venv\Scripts\python.exe scripts\run_backend.py
```

```powershell
npm --prefix frontend run dev
```

Then open **http://127.0.0.1:5173**, paste `API_ACCESS_KEY` from the env file you started with, and click Connect.

Health check: `http://127.0.0.1:8000/health/ready` should return `"status":"ready"`.

## 3. Tests (backend must be running with `.env.test`)

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 4. Rebuilding environments (only if needed)

```powershell
py -3.13 -m venv .venv
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

```powershell
npm --prefix frontend ci
```

## 5. If the runtime database was cleaned from %TEMP%

```powershell
.\.venv\Scripts\python.exe scripts\prepare_local_runtime.py
```

This seeds the runtime DB from `data\insightiq.sqlite3` and never overwrites existing data (see BUG_REPORT B1).

## Notes

- Port 8000 and 5173 must be free; the frontend uses `strictPort`.
- Clicking **Start research** calls Groq and **Refresh news** calls NewsData.io. Both use your quota.
- Never commit `.env` or `.env.test`.
