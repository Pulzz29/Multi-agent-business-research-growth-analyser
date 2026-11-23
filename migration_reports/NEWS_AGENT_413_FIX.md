# News Agent HTTP 413 fix

**Status: fixed and verified offline (133/133 tests).** No Groq or NewsData.io request was made, and no research job was submitted.

## 1. Root cause (measured, not assumed)

`select_news_with_groq()` sends up to 12 stored articles (`load_news_evidence(max_articles=12)`), each with its **full stored `description`**. NewsData.io sometimes stores the full article body in that field. The 20:17 UTC MSFT refresh stored article `NEWS_MSFT_70` with a **47,591-character** description, which became the newest candidate and so was always included.

Exact request rebuilt offline (`scripts/measure_news_request.py` captures the real `guarded_chat_completion` arguments with a stub; the network is blocked):

| MSFT request | 18:57 UTC DB (News Agent succeeded) | Current DB (News Agent fails with 413) | **After fix** |
|---|---|---|---|
| Articles sent | 12 | 12 | **9** |
| Request body (messages + schema), bytes | 25,745 | **72,546** | **11,819** |
| Largest description, chars | 5,692 | 47,591 | not sent |
| Est. input tokens (chars ÷ 4, typical) | ≈ 6,400 | **≈ 17,800** | ≈ 2,950 |
| Est. input tokens (bytes ÷ 3, conservative, used for the budget) | — | ≈ 24,200 | **3,929** |
| `max_completion_tokens` | 1,200 | 1,200 | 1,200 (unchanged) |
| Est. tokens requested (input + completion) | ≈ 7,600 | **≈ 19,000** | ≤ 5,129 (conservative) |

Raw measurements are in `artifacts/news_request_before_fix_1857.json`, `news_request_before_fix_current.json` and `news_request_after_fix_current.json`.

### Token limit: what is known and what is not

- Groq returns **HTTP 413** when a single request's token estimate (input plus `max_completion_tokens`) is greater than the organisation's per-minute token limit for the model. Ordinary 429 rate limiting is handled separately by `groq_rate_limit.py`.
- **No sanitized error record with the provider's `Limit`/`Requested` values exists locally.** The failed run `ddc47493…` survives only as a journal entry (`error_type: APIStatusError`), and the original code did not keep provider details. So I could not confirm the exact account limit offline.
- The evidence is consistent with an **8,000 tokens-per-minute** limit on `openai/gpt-oss-120b` for this account. The ≈7,600-token request succeeded and the ≈19,000-token request failed. Please check your tier's limit in the Groq console. The new diagnostics (section 4) will record the exact `Limit`/`Requested` numbers if a 413 ever happens again.
- Token counts are **estimates**. An exact count needs the model's tokenizer, which would have to be downloaded, and that was out of scope offline. The budget therefore uses a deliberately conservative estimator (UTF-8 bytes ÷ 3 plus 8 tokens per message), which over-counts typical English/JSON (≈4 characters per token) by about 25–35%.

Other tickers before the fix: AMZN ≈ 9,800 + 1,200 (would also fail), META ≈ 7,000 + 1,200 (borderline), AAPL/GOOGL/NVDA within an 8,000 limit.

## 2. Fix: token-aware evidence budget (`backend/app/agents/news_agent.py`)

- **`INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET`** (optional; default **6,000**; accepted range 1,700–131,072; invalid values fail with a clear `NewsAgentError`) is the total per-request budget. `max_completion_tokens` (1,200, unchanged) is reserved first, which leaves **4,800** estimated input tokens for the system instruction, task text, schema and articles. The estimate covers the complete serialized `messages` and `response_format`, so the system prompt and schema overhead are counted.
- **`fit_articles_to_budget()`**:
  - If all candidates fit, they are sent **unchanged**. A test proves the request is byte-identical to the pre-fix code (GOOGL).
  - Otherwise, whole articles are chosen greedily by **relevance (high first), then recency (newest first)**, and the chosen articles are sent in their original order.
  - An article that cannot fit is excluded **whole**. No description, title, JSON string or evidence ID is truncated or edited.
  - If no article fits, it raises `NewsAgentError` instead of sending an oversized request. An empty collection is rejected the same way.
- A pre-flight check refuses to send any request whose estimate is greater than the budget.
- Unchanged: the system instruction, task wording, article fields, `NewsSelections` strict JSON schema, `temperature=0`, model, `validate_news_selections()` (Groq can only choose IDs that were offered, and excluded IDs are rejected), database-sourced headline/URL/publisher/date, and all downstream evidence checks (`assess_news_report`, `validate_article_source`).
- Transparency: `groq_diagnostics.request_budget` records the budget, the estimate, the number of candidates, the number sent and the number excluded, plus `request_body_bytes`. When articles are excluded, a disclosure line is added to the News Agent output. Stored records are not modified.

Articles excluded at the default budget with the current data:

| Ticker | Sent / candidates | Excluded |
|---|---|---|
| MSFT | 9 / 12 | `NEWS_MSFT_70` (47,591 chars), `NEWS_MSFT_4` (5,692), `NEWS_MSFT_6` (5,336) |
| AMZN | 2 / 3 | `NEWS_AMZN_24` (24,634 chars) |
| META | 6 / 12 | 6 older equal-relevance articles |
| NVDA | 9 / 12 | 3 oldest equal-relevance short articles (the budget was almost exactly used) |
| AAPL, GOOGL | all | none |

### Why 6,000

- It stays well under the inferred 8,000-token limit even if the conservative estimator is somehow low by about 25%.
- The research workflow makes Financial, News and Strategic Groq calls within seconds of each other, and they share the same per-minute window. A smaller News request lowers the chance of 429 waits.
- It still leaves room for 6–11 articles of normal length.

If your tier allows more, raise `INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET` in `.env` (for example 7,000 for an 8,000 TPM limit). Lower it if your limit is smaller.

## 3. Accurate agent statuses on failure (`backend/app/services/research_jobs.py`)

Before: any exception in `execute_queued_research_job` marked **all three agents `failed`**, even when Financial had completed and Strategic had never started. The `ddc47493…` journal shows exactly that.

After: the new `observed_agent_statuses(run_id)` derives each agent's outcome from the LangGraph trace events that `agent_tracing` already writes (`step_completed` → completed, `step_failed` → failed). Agents with no execution record stay **`pending`**, which is the same convention `recover_interrupted_jobs()` already uses. If no trace evidence exists, the original conservative marking is kept. The job status, error type and error code are unchanged.

Verified in an isolated database copy: a simulated 413 from the News node gives `financial: completed, news: failed, strategic: pending`, in both SQLite and the job journal, with `error_type: APIStatusError`.

## 4. Sanitized HTTP 413 diagnostics

On `groq.APIStatusError` with status 413, `record_request_size_error()` logs (`WARNING groq_request_too_large …`) and keeps an in-process record (`recent_request_size_errors()`, last 50). It contains: timestamp, component `news_agent.groq_request`, status code, exception name, model, provider limit type (TPM/TPD), and the provider's **limit and requested token numbers only** (parsed as integers, with no message text kept). It also records request body bytes, estimated input tokens, `max_completion_tokens`, budget and number of articles. **It contains no API key, no prompt and no article content.** The original exception is re-raised unchanged, so job error handling behaves as before.

## 5. What was not changed

Financial Agent, Strategic Agent (including prompts), forecasting, financial normalization, the Groq client, the rate limiter, news ingestion, existing database records and saved reports. All of these were verified byte-identical to the Drive export, as were the 5 report files and the primary database.

## 6. Files

| File | Change |
|---|---|
| `backend/app/agents/news_agent.py` | Budget configuration, estimator, `fit_articles_to_budget`, `build_news_messages` (the same message content, moved into a helper), pre-flight check, 413 diagnostics, budget diagnostics and disclosure |
| `backend/app/services/research_jobs.py` | `observed_agent_statuses()`; used in the failure branch |
| `.env.example` | Documents the optional `INSIGHTIQ_NEWS_REQUEST_TOKEN_BUDGET` |
| `scripts/measure_news_request.py` | Offline request reconstruction and measurement |
| `tests/test_news_agent_413.py` | 16 offline tests |

Backups taken before any change: `migration_inputs/pre_413_fix_20261122T204748Z/` (read-only, SHA-256). Full diff: [artifacts/news_agent_413_fix.diff](artifacts/news_agent_413_fix.diff) (+265 / −42 lines).

## 7. Tests (all offline)

| Requirement | Test(s) | Result |
|---|---|---|
| Large collections | `test_large_collection_fits_budget_without_truncation`, `test_high_relevance_preferred_when_over_budget`, `test_single_oversized_article_is_rejected_not_truncated` | PASS |
| Normal collections | `test_normal_collection_unchanged`, `test_within_budget_request_identical_to_original_code` | PASS |
| Empty collections | `test_empty_collection_rejected` | PASS |
| Valid evidence IDs | `test_evidence_ids_preserved_and_excluded_ids_not_selectable`, field-identity checks in `test_real_request_within_budget` | PASS |
| Request-size compliance | `test_real_request_within_budget[×6]` (real DB, all tickers), `test_configurable_budget` | PASS |
| Sanitized 413 diagnostics | `test_413_diagnostic_is_sanitized_and_error_propagates` | PASS |
| Accurate agent statuses | `test_failed_news_agent_keeps_accurate_agent_statuses` (isolated DB) | PASS |
| Full regression | 133 tests | **133 passed** |
| Report hashes | `test_report_and_cache_files_unchanged`, `test_original_export_untouched`, plus a manual hash of `reports/research_runs/*` | identical |

## 8. Live confirmation (2026-11-23)

One approved MSFT research job (`9dd5df73…`) completed with all three agents. The News request was 11,738 bytes with 9 articles. Groq reported **2,620 prompt tokens + 720 completion tokens = 3,340 total**, about 33% below the conservative estimate (3,929 input) and well within the 6,000 budget. No 413 occurred. gpt-oss-120b used 720 of the 1,200 completion tokens (it spends some on reasoning), so the 1,200 reservation should stay.

### After the live MSFT refresh (11 new articles)

Three new MSFT descriptions are 10,600–12,200 characters. All 12 candidates would now be ≈14,300 estimated input tokens, so the old code would hit 413 again. With the budget: 9 articles, 8,362 bytes, ≈2,777 estimated input + 1,200 completion. Excluded: `NEWS_MSFT_76`, `_77`, `_78` (relevance 0.7). The seven new 0.9-relevance articles are all kept.

## 9. Readiness for one controlled live research test (historical)

Ready from the code side. Remaining preconditions on your side:

1. Fill in `GROQ_API_KEY`, `NEWSDATA_API_KEY` and `SEC_USER_AGENT` in `.env`.
2. Restart the backend with `.env`; it is currently running on the offline `.env.test`.
3. Run **one** MSFT research job. Expect about 5,100 estimated tokens or fewer for the News request.

If a 413 still occurs, the log line `groq_request_too_large` will show the provider's exact limit, so the budget can be set precisely.
