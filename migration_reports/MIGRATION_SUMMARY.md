# Migration summary (updated 2026-11-23)

**Status: migrated, running locally in live mode (`.env`), verified offline and by one approved live MSFT research run, which completed with all three agents (run `9dd5df73…`).** One approved live MSFT news refresh then added 11 articles (75 → 86) and automatically created a verified recovery point (B1 fix confirmed live).

## Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| FastAPI backend starts locally | PASS | Startup recovery verified on every restart |
| React runs locally | PASS | Vite on 127.0.0.1:5173 |
| API authentication functional | PASS | 401 matrix; signed-in UI works |
| All six companies available | PASS | API and UI |
| SEC financial data accessible | PASS | 12,737 facts |
| News records accessible | PASS | 75 articles; per-company status matches DB |
| Saved research reports readable | PASS | 5/5 via API; report opened and rendered in the signed-in UI |
| Agent code intact | PASS | Financial/Strategic byte-identical; News Agent changed only in request sizing (413 fix) |
| LangGraph workflow loads | PASS | 3-agent graph compiles |
| Forecasting modules and caches | PASS | 12/12 cache fingerprints match; SARIMAX recompute matches |
| Manual News Refresh integrated | PASS (no live call) | Route, auth, validation; button verified signed-in |
| React builds | PASS | Byte-identical bundle |
| Database integrity | PASS | All SQLite files ok |
| No research data lost | PASS | Primary DB and reports byte-identical to export |
| No credentials exposed | PASS | Secret scan clean; throwaway UI-test key deleted and revoked |
| Refreshed news survives recovery (B1) | PASS (live + isolated) | Live refresh created a verified backup with all 86 articles; isolated wipe-and-restore keeps refreshed news only with the fix |
| Live news refresh | PASS | MSFT +11 articles, existing 75 unchanged, no new duplicates |
| News Agent request within Groq limit (413) | PASS (live) | Live run: 11,738 bytes, 3,340 actual tokens, no 413 |
| Live research workflow | PASS | One MSFT job completed: 7 claims, 4 articles, 4 priorities; backup verified (6/6) |
| Accurate per-agent failure status | PASS | financial completed / news failed / strategic pending |
| Changes documented | PASS | Changelog, diffs, per-fix reports |

**Automated tests: 136 / 136 passed** after the live research run and live news refresh (network-guarded, offline).

## Answers to the 2026-11-23 follow-up

1. **B1:** fixed. Every successful news or SEC ingestion now creates a SHA-256-verified recovery point. Proven by an isolated wipe-and-restore test.
2. **6 vs 7 jobs:** the 7th run, `ddc47493…` (MSFT, failed at 20:17:40 UTC with `APIStatusError`, almost certainly the 413), exists only in the job journal. It never reached a SQLite snapshot. No records were invented or deleted. See BUG_REPORT B7, which describes an optional import from the journal.
3. **Environment file:** the running backend uses **`.env.test`** (offline placeholders). For live use, fill in `.env` and restart without `--env-file`. See LOCAL_STARTUP_GUIDE.
4. **Signed-in UI:** Overview, Research (company switch, Refresh News panel), Activity and a saved report (Financial/News/Strategy tabs with evidence) are all verified.
5. **Regression:** 133/133.
6. **Isolated recovery:** refreshed news survives restoration with the fix.
7. **HTTP 413:** fixed with a token-aware news budget. See [NEWS_AGENT_413_FIX.md](NEWS_AGENT_413_FIX.md).

## Live test done

See TEST_RESULTS, "Controlled live research test". The research job count is now 7 locally (6 completed + 1 failed), which matches the Colab count by coincidence; the Colab-only failed run `ddc47493…` is still only in the journal (B7).

## Original pre-live checklist (completed)

1. Fill in `GROQ_API_KEY`, `NEWSDATA_API_KEY` and `SEC_USER_AGENT` in `.env`. Optionally set `INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET` to match your Groq tier.
2. Stop the `.env.test` backend and start with `.env`.
3. Approve exactly one live action. Suggested: one MSFT research job, or one MSFT news refresh, which uses at most 2 NewsData pages and will now also create a recovery point.

## Open decisions

- B7: import the 7th job's journal record into SQLite? (Optional.)
- Also create recovery points when research jobs fail? (Optional; prevents B7 recurring.)

## Reports

[NOTEBOOK_ANALYSIS](NOTEBOOK_ANALYSIS.md) · [ENVIRONMENT_AUDIT](ENVIRONMENT_AUDIT.md) · [MIGRATION_PLAN](MIGRATION_PLAN.md) · [MIGRATION_CHANGELOG](MIGRATION_CHANGELOG.md) · [DATABASE_PRESERVATION](DATABASE_PRESERVATION.md) · [TEST_RESULTS](TEST_RESULTS.md) · [BUG_REPORT](BUG_REPORT.md) · [NEWS_AGENT_413_FIX](NEWS_AGENT_413_FIX.md) · [SECURITY_REVIEW](SECURITY_REVIEW.md) · [UI_REVIEW](UI_REVIEW.md) · [FUNCTIONALITY_PRESERVATION](FUNCTIONALITY_PRESERVATION.md) · [LOCAL_STARTUP_GUIDE](LOCAL_STARTUP_GUIDE.md)
