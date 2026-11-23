# Functionality preservation

"Code unchanged" means byte-identical to the Drive export, except for the listed compatibility edits, which are in [MIGRATION_CHANGELOG.md](MIGRATION_CHANGELOG.md).

| Feature | Code | Verification | Status |
|---|---|---|---|
| FastAPI app and all 13 routes | `main.py` (path compare only) | Live server; every route exercised | VERIFIED |
| Bearer access-key auth | unchanged | 401 matrix tests | VERIFIED |
| Request/response schemas | unchanged | 422 validation tests; report and job shapes | VERIFIED |
| Job retrieval / report retrieval | path compare localized | 6 jobs, 5 reports served | VERIFIED |
| Job submission + background execution | unchanged | Validation path tested; real submission not run (Groq) | PARTIAL: live run needs approval |
| Dashboard statistics | unchanged | `/dashboard/overview` 200 | VERIFIED (API) |
| Observability / traces / reliability / backup health | unchanged | endpoints 200; backup health 5/5 | VERIFIED |
| Financial / News / Strategic agents | unchanged | Import; graph compiles | VERIFIED (structure) |
| LangGraph orchestration | unchanged | `build_research_graph()` nodes and order | VERIFIED |
| Groq integration, prompts, rate-limit recovery | unchanged | Import only | NOT RUN live |
| Financial claim verification | unchanged | Saved reports contain verified claims; SEC fact ids resolve | VERIFIED (saved data) |
| News evidence validation | unchanged | Cited article ids/URLs resolve per ticker | VERIFIED (saved data) |
| Strategic conclusions | unchanged | Report schema; evidence ids resolve | VERIFIED (saved data) |
| SEC EDGAR data | unchanged | 12,737 facts; normalization ≥20 quarters for 6 tickers | VERIFIED |
| Normalization and metric selection | unchanged (byte-identical) | Cache fingerprints match | VERIFIED |
| SARIMAX forecasting, backtesting, baseline | unchanged (byte-identical) | Local recompute = cached forecast | VERIFIED |
| Forecast quality gates | unchanged | Import; cached `forecast_quality` present in reports | VERIFIED (structure) |
| Forecast caching and invalidation | unchanged | 12 caches valid; fingerprints match | VERIFIED |
| NewsData.io integration | unchanged | Import; not called | NOT RUN live |
| Relevance filtering, deduplication | unchanged | Static review | VERIFIED (static) |
| Refresh News API + cooldown + page limit | unchanged | Auth and ticker validation tested; 600 s cooldown, `max_pages=2` present | VERIFIED (no live call) |
| Refresh News button | unchanged | Visible in browser | VERIFIED |
| React/Vite app, pages, company selector | unchanged | Build identical; pages render | VERIFIED |
| Start Research control, progress indicators | unchanged | Visible in browser | VERIFIED (render) |
| Report visualization | unchanged | Needs sign-in | NOT VERIFIED |
| Dark/light theme, responsive layout | unchanged | Browser | VERIFIED |
| SQLite schema and data | unchanged | Byte-identical primary DB; counts | VERIFIED |
| Backup and recovery | lock/close fixes only | Startup recovery, restart test, backup reconciliation | VERIFIED |
| Stored data relationships | unchanged | Report ↔ job ↔ backup ↔ evidence checks | VERIFIED |
