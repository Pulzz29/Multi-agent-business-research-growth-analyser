# InsightIQ notebook analysis

Input: `InsightIQ_Multi_Agent_Business_Research.ipynb` (136 cells, SHA-256 `815d36a5…b3e8ed`, copy kept in `migration_inputs/`).
Also supplied: `InsightIQ_Claude_Code_QA_Package.zip`. It holds only QA documents (master prompt, test matrix, report template), **no source code, database or reports**.

## How the notebook builds the application

1. **Cell 1** mounts Google Drive and sets `PROJECT_ROOT = /content/drive/MyDrive/Multi-Agent-Business-Research`.
2. **Cell 3** creates an isolated build workspace `builds/<UTC timestamp>/`. The recorded run used build `20261122T173459146790Z`. Every later cell writes into `ACTIVE_ROOT` = that build folder.
3. **Cell 5** reads `GROQ_API_KEY`, `NEWSDATA_API_KEY` and `SEC_USER_AGENT` from Colab Secrets (`google.colab.userdata`).
4. **Cells 7 and 9** pip-install dependencies. The recorded resolved versions are pinned in `requirements.lock.txt`.
5. **Cell 11** defines `write_source(relative_path, content, replace=False)`, an atomic writer confined to `ACTIVE_ROOT`. Cells 11 to 91 then generate the backend and frontend as Python string constants passed to `write_source`.
6. Later cells **patch** earlier files. They either re-write a whole module (`replace=True`), or read the file, `str.replace` an anchor string (asserting that it occurs exactly once) and write it back. Many patches only apply if an earlier verification in the same cell passed.
7. **Cells 101 to 134** reconnect to the hardcoded build `builds/20261122T173459146790Z` and apply the final changes: the news refresh API (cell 126) and the React Refresh News button (cell 130). Cell 130 builds the patched frontend in a temporary directory and copies the source back only if `npm run build` succeeds.

## Final intended project structure (from the generated sources)

```
<build root>/
├── backend/
│   ├── requirements.txt
│   └── app/
│       ├── main.py                      FastAPI app (auth, research jobs, traces, reliability, dashboard, news refresh)
│       ├── server_entrypoint.py         uvicorn launcher with startup recovery
│       ├── core/config.py               pydantic-settings, env prefix INSIGHTIQ_
│       ├── database/sqlite_manager.py   SQLite schema/migrations, working copy in %TEMP%, snapshot to data/insightiq.sqlite3
│       ├── agents/                      financial_agent, news_agent, strategic_agent, research_workflow (LangGraph)
│       └── services/                    sec_ingestion, news_ingestion, financial_normalization, forecasting (SARIMAX),
│                                        forecast_cache, forecast_quality, groq_client, groq_rate_limit, financial_claims,
│                                        news_quality, research_jobs, sqlite_recovery, startup_recovery, server_bootstrap,
│                                        backup_freshness, research_backup, backup_reconciliation, runtime_startup,
│                                        observability, agent_tracing, trace_explorer, reliability_monitoring, dashboard_data
├── frontend/                            React + Vite (package.json, vite.config.js, src/App.jsx, src/api.js, src/styles.css)
├── data/                                insightiq.sqlite3, sqlite_backups/, research_job_journal/, forecast_cache_v1/
├── reports/                             verification JSON reports
├── logs/  observability/  releases/
```

## Cell map

The full per-cell table is in [artifacts/cell_map.md](artifacts/cell_map.md). It was produced by an offline replay (see below) and lists which source files each cell writes or patches, plus its Colab-specific calls. Summary:

| Category | Cells |
|---|---|
| Colab setup (Drive, Secrets, pip) | 1, 3, 5, 7, 9 |
| Source generation (backend) | 11–89 (odd-numbered code cells) |
| Source generation (frontend) | 91 (React dashboard), 130 (Refresh News button) |
| Backend final patch | 126 (news refresh API, adds `main.py.before_news_refresh.bak`) |
| Data ingestion (live SEC / NewsData calls) | 15, 17, 21 (and live tests in 134) |
| Verification only | 23, 29, 31, 35, 41, 75, 93–99, 108–122, 124, 128, 132 |
| Colab runtime start (uvicorn + `serve_kernel_port_as_*` proxy) | 93, 95, 101, 106 |
| Interactive helpers (not application code) | 103 (generates `API_ACCESS_KEY`), 104 (ipywidgets copy button) |
| Not executable (Markdown text inside a code cell) | 2, 127 |
| Empty | 135 |

## Critical finding: the notebook does not contain the complete final source

Two modules that the final application imports were created by cells that are **no longer in the notebook**:

| Module | Evidence | Impact |
|---|---|---|
| `backend/app/services/forecast_cache.py` | Cell 37 asserts it already exists ("Run the original Cell 15 source creation first") and only patches one line. No cell defines `source_fingerprint`, `financial_review_digest`, `validate_cached_result` or `CACHE_VERSION`. Cell 114's output prints about 60% of its lines as excerpts. | `financial_agent`, `strategic_agent` and `news_agent` import it, so the research workflow cannot load. |
| `backend/app/agents/research_workflow.py` | Cell 59 asserts it exists ("Run the successful replacement Cell 21 first") and patches two lines. No cell writes it. Cell 112's output lists only its class and function names. | `main.py` imports it at module level, so **the FastAPI backend cannot start**. |

Rebuilding these two modules by hand would mean writing new business logic: cache fingerprinting and the LangGraph orchestration. That would break the "do not rewrite / do not invent" rule. So I did **not** reconstruct them.

## Data that exists only on Google Drive

Cell 101's recorded output shows 22 SQLite files under `builds/20261122T173459146790Z/data/`. The newest valid backup held 12,737 `sec_facts`, 69 `news_articles` and 6 `research_execution_runs`, and later news tests raised the article count to 73. There are also 21 verification reports and the forecast cache. None of this is on this computer: there is no Google Drive for Desktop sync, and the supplied ZIP has no data.

## Offline replay (how the cell map was produced)

`artifacts/notebook_replay.py` executes the notebook's code cells statement by statement in a sandbox:

- all non-loopback network connections and DNS lookups are blocked;
- `google.colab` is faked with dummy credentials (no real secrets read);
- `subprocess` is limited to `npm` and `node`;
- `/content` is remapped into a scratch folder.

It recovered 33 backend modules and the frontend. However:

- Data-dependent verifications fail without the Drive data. Because of that, some conditional patches did not apply. The cell 130 Refresh News patch also failed (Windows path length).
- Executing statements after a failure lets stale variables from earlier cells leak in. For example, the replay wrote `news_ingestion.py` content into `forecast_cache.py`.

**Therefore the replayed source is a diagnostic artifact only. It is not a trustworthy copy of the final application and was not installed as the project.** The authoritative source is the Drive build folder.

## Update after receiving the Drive export (2026-11-23)

The Drive export of `builds/20261122T173459146790Z` contains both missing modules: `forecast_cache.py` (9,328 bytes) and `research_workflow.py` (10,856 bytes). It also contains the final news-refresh state (`main.py` with `/api/v1/news/refresh`, plus `main.py.before_news_refresh.bak`) and the patched React app (`App.jsx` with Refresh News). The export was installed as the project. The replayed source above was only a diagnostic and was not used.
