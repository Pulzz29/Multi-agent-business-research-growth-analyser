# InsightIQ: multi-agent business research (local)

FastAPI + LangGraph (Financial, News, Strategic agents on Azure OpenAI, optional Groq fallback) + SEC EDGAR + market data providers + SARIMAX forecasting + PostgreSQL + React/Vite dashboard. Developed and run locally in VS Code; container and AKS deployment files are prepared in `deploy/`.

**Quick start:** fill in `.env` (copy from `.env.example` if missing), then run:

```powershell
.\scripts\start_all.ps1
```

Open http://127.0.0.1:5173 and sign in with your email and password (accounts are created with `scripts/provision_users.py`).

| Path | Contents |
|---|---|
| `backend/app` | FastAPI app, agents, services, database layer (`app/database`) |
| `frontend` | React + Vite dashboard |
| `data` | Original SQLite database (rollback), its backups, PostgreSQL dumps (not in Git) |
| `reports` | Saved research reports and verification reports (not in Git) |
| `scripts` | `run_backend.py`, `start_all.ps1`, `setup_postgres.py`, `backup_postgres.py`, migration and verification scripts |
| `tests` | Offline, network-guarded test suite |
| `deploy` | Container and AKS deployment files (prepared, not applied) |
| `migration_reports` | Historical migration records, kept as verification evidence |

Full instructions: [README_INSIGHTIQ.md](README_INSIGHTIQ.md) and [docs/POSTGRES_MIGRATION.md](docs/POSTGRES_MIGRATION.md).

Forecasts are statistical estimates, not reported results. The system does not give investment advice.
