# Research intelligence, live refresh and reliability upgrade

Date: 2026-11-23. Scope: the existing InsightIQ backend, LangGraph workflow and React dashboard. Nothing was rebuilt or replaced.

**Live status:** these changes have been verified **offline only**. No Groq, SEC EDGAR or NewsData.io request was made. A one-company live test has been proposed (§13) and has not been run.

---

## 1. Original limitations (confirmed in code)

| Limitation | Where it was |
|---|---|
| Groq only selected and tagged evidence. Python wrote one-line sentences. | `financial_claims.request_grounded_claims`, `news_agent.select_news_with_groq`, `strategic_agent.select_priorities_with_groq` |
| Strategic "what to monitor" text came from fixed templates. | `strategic_agent.MONITORING_QUESTIONS` |
| Start Research never refreshed sources. SEC refresh could not be reached at all. | `sec_ingestion.refresh_sec_company` had no caller and no endpoint |
| No duplicate-job guard per company. | `POST /api/v1/jobs` |
| Saved reports stated, incorrectly, that jobs were not stored recoverably. Several disclosures still said "pilot". | `research_workflow.py` (old L396-401), agent disclosures |
| Forecast-quality figures came from a stored review file, but this was not labelled. | `financial_agent.load_financial_quality` |

---

## 2. What changed

### 2.1 Workflow

Start Research now runs these steps, in this order:

1. **source_preparation** (new graph node; orchestration, not an agent):
   - reads freshness from `source_refreshes`;
   - refreshes stale SEC data, then stale news (news needs the SEC company row);
   - labels every outcome.
2. **financial_agent**:
   - re-verifies 8 standalone quarters per metric against `sec_facts`;
   - reads forecasts from the fingerprinted cache;
   - builds a bounded evidence package;
   - makes **one Groq call** that selects claims (strictly validated) and writes findings (validated one by one).
3. **news_agent**:
   - runs the existing selection call unchanged;
   - applies the existing attribution and source checks;
   - makes **one Groq writing call** over the selected articles only.
4. **strategic_agent**: makes **one Groq call** that selects priorities (existing validator) and writes an interpretation for each (validated one by one).
5. **Report saved.** The file is written with fsync and renamed atomically, and is never overwritten. The existing **executive-summary** sidecar is then generated with one Groq call; its context now includes the validated v2 findings.

That is **5 Groq calls per run**. A failed request can retry within strict limits: 413, invalid output and timeouts move to a smaller request profile, and 429s use the existing bounded retry.

### 2.2 What happens when something fails

| Failure | Result |
|---|---|
| Financial Groq call fails | Claims are chosen deterministically (the latest two quarters per metric, through the same exact validator). The report labels this `claims_selection: deterministic_fallback` and `analysis.status: unavailable`. |
| News writing call fails | The validated article selection is kept. News analysis is marked `unavailable` and the reason is given. |
| One strategic interpretation fails validation | That interpretation is withheld. The template monitoring question is shown and labelled as template. |
| Strategic selection call fails | The job fails as before (`research_execution_failed`). It never stays `running`. |
| Source refresh fails | The default is to continue with stored data, labelled `refresh_failed_using_cached`. If the run was set to fail on refresh errors, the job fails with `source_refresh_failed`. |

### 2.3 Files

**New**

- `backend/app/services/groq_structured.py`: budgeted, bounded structured Groq requests and diagnostics.
- `backend/app/services/narrative_validation.py`: checks on each finding (numbers, percentages, periods, IDs, trend words, causal, advisory and injection language, URLs).
- `backend/app/services/evidence_package.py`: compact validated packages, Python Decimal comparisons, and deterministic observation sentences.
- `backend/app/services/source_refresh.py`: freshness snapshot, per-company/per-source locks, a guard against refreshes in other processes, and honest status mapping.
- Tests:
  - `tests/research_v2_support.py` (isolated runtime and a test-only fake Groq)
  - `tests/test_groq_structured.py`
  - `tests/test_financial_analysis.py`
  - `tests/test_news_strategic_analysis.py`
  - `tests/test_source_refresh.py`
  - `tests/test_research_v2_workflow.py`
  - `frontend/tests/reportModel.test.mjs`

**Modified**

- `backend/app/agents/`:
  - `research_workflow.py`: new node and v2 report; outdated limitations corrected.
  - `financial_agent.py`: combined analysis call, `n_quarters` parameter, forecast provenance.
  - `news_agent.py`: writing call appended; disclosure wording corrected.
  - `strategic_agent.py`: combined analysis call appended; the standalone pilot path is unchanged.
- `backend/app/core/config.py`: new optional settings, all with defaults.
- `backend/app/main.py`:
  - job options;
  - 409 duplicate-job guard;
  - `GET /api/v1/companies/{t}/freshness`;
  - `POST /api/v1/companies/{t}/refresh`;
  - news refresh now shares the refresh lock.
- `backend/app/services/`:
  - `sec_ingestion.py`: reuses the stored CIK; submissions pre-check (`no_new_filings`); records registry failures.
  - `news_ingestion.py`: canonical-URL dedupe.
  - `forecast_cache.py`: TTL is configurable.
  - `research_jobs.py`: fsync, immutability check, error codes, `active_job_for_ticker`.
  - `executive_summary.py`: v2 context and a third budget tier; shared banned-language list.
  - `financial_claims.py`, `groq_client.py`: model name read from `GROQ_MODEL`.
- `frontend/src/`:
  - `lib/api.js`, `lib/reportModel.js`
  - `pages/ReportViewer.jsx`, `pages/CompanyPage.jsx`
  - `App.jsx`, `styles.css`
- `frontend/package.json`: adds the `test` script.
- `.env.example`: new optional variables documented.

**Not modified on purpose:**

- `financial_normalization.py` and `forecasting.py`. Their SHA-256 is part of every forecast fingerprint.
- `.env`, all databases, all saved reports and all forecast caches.

---

## 3. Source freshness policy

All values are configurable with `INSIGHTIQ_*` environment variables (see `.env.example`).

| Setting | Default | Why |
|---|---|---|
| `SEC_CACHE_HOURS` | 24 | SEC filings are periodic. A daily check is enough, and the submissions pre-check keeps it cheap. |
| `NEWS_CACHE_HOURS` | 6 | Same as the existing ingestion default. Each refresh uses at most 2 NewsData pages (30 articles). |
| `AUTO_REFRESH_ON_RESEARCH` | true | Start Research refreshes only stale sources. The dashboard can skip this per run. |
| Manual cooldown | 10 min | Same as the existing news cooldown, now applied to both sources. |
| `REFRESH_RUNNING_STALE_MINUTES` | 15 | A `running` row younger than this is treated as another process refreshing. |
| `FORECAST_CACHE_TTL_HOURS` | 720 | A safety net only. The input fingerprint is what invalidates a forecast. |

Each v2 report records a `source_freshness` block containing:

- the generation time;
- the SEC data cutoff, the latest periodic filing and its date, and the last successful SEC refresh;
- the latest cited news date, the latest stored news date, and the last successful news refresh;
- for each source: the refresh result, whether a provider was called, records added, and the error category;
- for each forecast metric: whether the cache was reused or recomputed, and its data cutoff.

**SEC behaviour**

- SEC refresh first reads the small `submissions/CIK##########.json` index.
- If the latest 10-Q/10-K accession is already stored, it records a success with 0 new rows and reports **no_new_filings**. The large companyfacts file is not downloaded.
- New facts are inserted with the existing `INSERT OR IGNORE` on `source_key`, so nothing is duplicated.
- The restatement rule is unchanged: the latest `filed_at` wins; on ties, the amendment, then the accession, then the row id.
- SEC data is never described as real-time.

**Viewing a saved report is read-only.** A test confirms it makes no Groq, SEC or NewsData call and does not change the file.

---

## 4. Validation method

**Financial findings**

- Prose may only contain these numbers:
  - the cited SEC values, as raw USD, billions to 0–3 decimal places, or millions to 0–1 decimal place, using either common rounding convention;
  - comparisons computed by Python;
  - forecast outputs;
  - dates and fiscal labels belonging to the cited evidence.
- Percentages must equal a comparison that the same finding cites.
- Words such as "trend", "sustained" or "momentum" need at least three consecutive cited quarters.
- The "verified observation" line is **written by Python from the cited evidence**, not by the model.

**News items**

- Must cite articles that were actually selected.
- Must attribute the development to the publisher.
- Numbers must come from the cited article's own headline or excerpt.
- Sentences about financial effects must be hedged.
- Headline-only items are limited to two sentences and must state uncertainty.
- Headline, publisher, date and URL are always copied from SQLite.

**Strategic interpretations**

- Supporting IDs must be in the strategic package.
- A "potential risk" or "potential opportunity" is downgraded to a monitoring priority unless at least two items support it, including SEC evidence or a news article with real content. Nothing can be labelled "confirmed".

**Checks that apply everywhere**

- Causal, certainty and advisory phrases are rejected. In limitation fields, causal words are allowed only in negated sentences ("cannot show what drove…").
- URLs in prose are rejected.
- Text that echoes instructions is rejected.
- Evidence IDs mentioned in prose must be cited.

**Labels.** Text that passes is labelled *"AI interpretation — automated evidence, number, period and language checks passed; not independently verified."* It is never labelled "verified". Text that fails is withheld, and the reason is listed in `withheld`.

### What this cannot guarantee

- These checks prove grounding and wording constraints. They do not prove that an interpretation is correct or complete.
- A sentence that uses only allowed numbers and neutral wording can still be misleading.
- Synonyms that are not on the banned lists can get through.
- Accepting causal words in negated limitation sentences is a heuristic.

Human review of AI prose is still advisable.

---

## 5. Token budgets and HTTP 413 / 429

**Budget**

- Each writing request must satisfy: estimated input (≈3 bytes per token, which over-counts) + reserved completion ≤ `GROQ_REQUEST_TOKEN_BUDGET` (default 7000). This keeps requests under the account's apparent 8,000 TPM limit.
- Measured offline:

| Call | Estimated input | Max completion |
|---|---|---|
| Financial | ≈4,120 | 2,800 |
| News writing | ≈1,960 | 2,400 |
| Strategic | ≈3,070 | 2,800 |

- A test checks that the full financial profile fits the budget for all six companies.

**Request profiles**

- Each call has a `full` and a `reduced` profile. The reduced profile uses fewer quarters, fewer comparisons, shorter excerpts and fewer findings.
- Whole records are dropped. IDs and numbers are never cut.
- `reasoning_effort=low` by default, because gpt-oss reasoning tokens count against the completion limit.

**Error handling**

| Condition | Handling |
|---|---|
| **413** | A sanitised event is recorded (numbers only). The request moves to the smaller profile. An unchanged request is never resent. |
| **429** | Uses the existing `guarded_chat_completion`: provider `Retry-After`, at most 5 retries, at most 120 s in total. |
| Timeout, connection error or 5xx | One retry after 2 s, then the next profile. |
| 400 | No retry. |
| Overall limit | At most `len(profiles) + 1` provider requests per call. |

**Diagnostics.** Each `generation.calls[]` entry records the agent, model, outcome, profile, attempts, estimated input, budget, elapsed time, 429 retries and fallback reason. Token counts are recorded **only when Groq reports them**; otherwise they are `null`. Prompts, responses, provider messages and keys are never recorded.

**Deviation from the plan.** Per-call Groq events are stored in the report and the structured log, not in `research_observability_events`. That table only accepts a small set of metadata fields. Recording recovered retries there as failed agent steps would also distort the reliability metrics.

---

## 6. Forecasts

- Forecasts are recomputed only when the fingerprint changes: SEC inputs, normalization/forecast code, or the reconciliation review.
- A test shows a cache hit with no training, and that changing one input value invalidates the cache.
- Reports show the data cutoff and whether each forecast was reused or recomputed.
- Interval-quality and holdout figures are labelled with their source: `reports/forecast_uncertainty_quality_review.json`, assessed 2026-11-22, not recomputed in the run.

---

## 7. Report schema compatibility

- New reports carry `report_schema_version: 2`. All new fields are additive, and every v1 key is still present.
- Old reports are unchanged.
- The dashboard detects v1 reports and shows "Earlier report format" and "extended AI analysis not available" instead of empty panels.
- Tests: every saved report still builds the dashboard model and the executive-summary context, read-only.

---

## 8. Dashboard

**Company page**

- **Refresh data** opens a *Source data* panel with:
  - a separate SEC row and news row, each showing state, latest data, last successful refresh and policy;
  - per-source "Refresh now" buttons and a "Refresh stale sources" button;
  - a per-source result and the effect on forecasts.
- **Research options** (under Start Research):
  - "Refresh stale SEC and news data first" (on by default);
  - "If a refresh fails: continue with stored data (labelled) / stop".

**Report viewer adds**

- Source freshness and research metadata, including a table of AI requests.
- AI-written financial findings: deterministic observation, interpretation, why it matters, what the figures cannot show, evidence chips, validation label, and any withheld items.
- Comparisons taken from the report itself.
- News intelligence cards:
  - DB headline and link;
  - a headline-only badge where relevant;
  - publisher report versus AI interpretation, kept separate;
  - uncertainty.
- Strategic interpretation: classification badge, indicators to monitor, follow-up, uncertainty. The template question is shown only as a fallback.
- Forecast provenance.

All text and figures come from backend report fields. The frontend never calls Groq and never starts research when a page opens.

---

## 9. Sample output (offline test output, not real model text)

The text below was produced by the **deterministic test-only fake Groq**, on an isolated copy of the database with refresh skipped. It shows report structure and validation, not the quality of real model writing.

```json
{
  "finding_id": "FIN_1",
  "title": "Latest quarterly revenue against the prior-year quarter",
  "verified_observation": "Revenue for the standalone quarter 2026-07-01 to 2026-09-30 (FY2027 Q1): USD 92.28 billion [SEC_revenue_20260930]. Revenue change versus the same quarter one year earlier (quarter ended 2025-09-30): +18.8% [CMP_revenue_20260930_yoy, computed by InsightIQ].",
  "interpretation": "Revenue for the standalone quarter ending 2026-09-30 was USD 92.28 billion. Against the same quarter one year earlier, the InsightIQ comparison shows a change of 18.8%. ...",
  "validation": {"status": "automated_checks_passed", "independently_verified": false}
}
```

Request sizing for the same run:

| Call | Profile used | Estimated input / max completion | Provider requests |
|---|---|---|---|
| financial_analysis | full | 4,127 / 2,800 | 1 |
| news_analysis | full | 1,962 / 2,400 | 1 |
| strategic_analysis | full | 3,072 / 2,800 | 1 |

**Whether Groq actually writes substantive explanations has not been shown yet.** That needs the live test in §13.

---

## 10. Test results (executed)

| Run | Result |
|---|---|
| Baseline, before any change | **175 passed, 3 skipped, 0 failed** (the earlier figure of "137" was out of date) |
| After the change, full suite (`.venv\Scripts\python.exe -m pytest -q`) | **272 passed, 3 skipped, 0 failed** (175 existing + 93 new + 4 new module-import cases) |
| Frontend `npm run build` | Success |
| Frontend `npm test` | **3 passed, 0 failed** |

- The 3 skips are unchanged from the baseline: the original Drive export and the pre-fix backup are not in this package.
- The HTTP API tests in the existing suite ran against the backend process that was **already running on :8000, started before these changes**. The new endpoints, duplicate-job guard and validation were tested in-process with FastAPI `TestClient`. **Restart the backend** to serve the new code.

**Not executed**

- Any live provider call.
- Browser-based visual inspection of the dashboard.
- Rendering tests at React-component level. The view model is unit-tested and the JSX compiles in the production build.

---

## 11. Preservation (verified by SHA-256 manifest before and after)

- **Files:** all 87 files under `reports/**`, `data/forecast_cache_v1/*` and `data/insightiq.sqlite3` are byte-identical.
- **Database rows:** baseline rows are unchanged in both the snapshot and the runtime DB, with identical counts:

| Table | Rows |
|---|---|
| `sec_facts` | 12,737 |
| `news_articles` | 87 |
| `source_refreshes` | 16 |
| `companies` | 6 |
| `research_execution_runs` (runtime / snapshot) | 13 / 10 |

- **Test isolation:** tests use a per-test copy of the database in a uniquely named temp folder, which is removed afterwards. Snapshot syncing is stubbed in tests.
- **Test artifact:** the existing B1 test rewrites `migration_reports/artifacts/isolated_news_recovery_test.json` on every run. It was restored to its committed content.

---

## 12. Known limitations and open items

1. **Untested against the real model.** Real gpt-oss output may have findings withheld more often than the fake does. Prompts and limits may need tuning after the live test.
2. **Time per run.** Five calls under an ~8k TPM limit will probably trigger 429 waits. Expect roughly 2–5 minutes per run. Rate-limit coordination is per process.
3. **Strategic selection failure still fails the job.** No deterministic strategic selection was introduced, because that would mean the system inventing priorities.
4. **Pinned baseline tests will flag a live refresh, by design.** After a live SEC refresh adds data, these will fail until the baseline is re-recorded:
   - `test_forecast_cache_valid_and_fingerprint_matches`
   - the DB-count tests that compare against `baseline_original.json`
   - `test_report_and_cache_files_unchanged`, if forecast caches are recomputed
5. **Reconciliation review is older than new data.** The review (2026-11-22) predates any quarter ingested later. Such quarters pass the normalization rules but not that review.
6. **News quality is limited by the existing relevance filter.** For example, a retail deal headline can count as direct coverage; the writing prompt asks the model to say so.
7. **Not committed to git.**

---

## 13. Launch

1. Stop the backend currently running on port 8000, then start the updated one:
   ```powershell
   .\.venv\Scripts\python.exe scripts\run_backend.py
   ```
   Or start the backend and the dashboard together:
   ```powershell
   .\scripts\start_all.ps1
   ```
2. If you started only the backend, start the dashboard from the `frontend` folder:
   ```powershell
   npm run dev
   ```
   Then open http://127.0.0.1:5173 and enter `API_ACCESS_KEY`.
3. Run the tests offline. Start the backend first, because the existing API tests need it:
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q
   ```
   Then, from the `frontend` folder:
   ```powershell
   npm test
   ```

## 14. Proposed controlled live test (awaiting approval; not run)

Company: **MSFT**. All four credentials are set in `.env` (presence checked only; values not read).

1. Restart the backend so it runs the new code.
2. `GET /api/v1/companies/MSFT/freshness`. Read-only.
3. `POST /api/v1/companies/MSFT/refresh {"sources":["sec","news"],"force":false}`. Provider use:
   - SEC: the submissions index, plus companyfacts only if a new filing exists;
   - NewsData: at most 2 pages, and only if news is stale (it is).
4. `POST /api/v1/jobs {"ticker":"MSFT"}`. Expected cost: about 5 Groq calls, roughly 15–25k tokens, about 2–5 minutes.
5. Review:
   - each `generation.calls[]` entry: tokens, profile, retries, fallbacks;
   - accepted and withheld financial findings, news items and strategic interpretations;
   - the executive-summary sidecar.
6. Confirm a new run ID was saved, and that `GET /api/v1/companies/MSFT/reports/{id}` reopens it with no provider calls.
7. Check the report in the dashboard.
8. Re-run the SHA-256 manifest to confirm historical reports are unchanged. New SEC or news rows, if any, are additions only.
