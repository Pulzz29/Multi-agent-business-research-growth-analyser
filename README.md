# InsightIQ: multi-agent business research (local)

FastAPI + LangGraph (Financial, News, Strategic agents on Groq) + SEC EDGAR + NewsData.io + SARIMAX forecasting + React/Vite dashboard, migrated from Google Colab to run locally on Windows.

**Quick start:** fill in `.env` (copy from `.env.example` if missing), then run:

```powershell
.\scripts\start_all.ps1
```

Open http://127.0.0.1:5173 and enter `API_ACCESS_KEY` from `.env`.

| Path | Contents |
|---|---|
| `backend/app` | FastAPI app, agents, services, SQLite manager |
| `frontend` | React + Vite dashboard |
| `data` | `insightiq.sqlite3`, backups, forecast cache, job journal (not in Git) |
| `reports` | Saved research reports and verification reports (not in Git) |
| `scripts` | `run_backend.py`, `start_all.ps1`, `prepare_local_runtime.py`, `db_baseline.py` |
| `tests` | Offline, network-guarded test suite |
| `migration_reports` | Migration documentation, starting with [MIGRATION_SUMMARY.md](migration_reports/MIGRATION_SUMMARY.md) |
| `migration_inputs` | Original notebook, Drive export ZIP and a read-only extraction (not in Git) |

Full instructions: [migration_reports/LOCAL_STARTUP_GUIDE.md](migration_reports/LOCAL_STARTUP_GUIDE.md).

Forecasts are statistical estimates, not reported results. The system does not give investment advice.
