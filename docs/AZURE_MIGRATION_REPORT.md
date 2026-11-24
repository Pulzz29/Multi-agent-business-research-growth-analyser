# Azure OpenAI GPT-5.4-Pro migration

InsightIQ's AI writing moved from Groq (`openai/gpt-oss-120b`, Chat Completions) to your Azure OpenAI deployment of GPT-5.4-Pro (Responses API). Only the AI provider changed. SEC EDGAR, NewsData.io, Finnhub, FMP and Yahoo Finance remain the sources of every number and article.

## Configuration

| Item | Value |
|---|---|
| `.env` file | `C:\Users\Administrator\Downloads\InsightIQ_Capstone_Project (2)\InsightIQ_Capstone_Project\InsightIQ\.env` |
| `AZURE_OPENAI_API_KEY` | KEY 1 or KEY 2 from Microsoft Foundry → your Azure OpenAI resource → *Keys and Endpoint* |
| `AZURE_OPENAI_ENDPOINT` | the resource endpoint, e.g. `https://<resource>.openai.azure.com/`; `/openai/v1` is optional, and an `api-version` query or deployment-specific URL is rejected |
| `AZURE_OPENAI_DEPLOYMENT` | the **deployment name** shown under Foundry → *Deployments* (yours: `gpt-5.4-pro`) |

No other Azure setting is required. The optional limits are listed in `.env.example`: reasoning effort, output caps, input budget, deadlines and concurrency.

The migration edit to `.env` removed only `GROQ_API_KEY`, `GROQ_MODEL` and two Groq comment lines.
- Your `API_ACCESS_KEY`, NewsData, Finnhub, FMP, SEC identity and `INSIGHTIQ_LEGACY_PROJECT_ROOTS` are unchanged.
- A local git-ignored copy of the previous file is in `.env.backup_before_azure_migration`.
- `.env` and `.env.*` are git-ignored.

## How a call works (`backend/app/services/ai_provider.py`)

Based on Microsoft's and OpenAI's documentation for GPT-5.4-Pro:
- Responses API only.
- Reasoning effort is medium, high or xhigh.
- Reasoning tokens count toward `max_output_tokens`.
- Long generations need background mode, which requires `store=true`.

How a call works:
- **Client:** the official `openai` SDK (2.54.0), `OpenAI(base_url=<endpoint>/openai/v1/, api_key=…, max_retries=0)`.
- **Request:** `responses.create(model=<deployment>, instructions, input, text={format: strict json_schema}, reasoning={effort}, max_output_tokens, background=True, store=True)`, then `responses.retrieve(id)` until a terminal status or the per-call deadline.
- **Cancellation:** on a deadline or a user cancelling the job, `responses.cancel(id)` is called.
- **Strict schemas:** each Pydantic model is converted to a strict JSON schema (closed objects, every field required). Value constraints are enforced by Pydantic after parsing.
- **No silent model substitution:** every response's served `model` must start with `INSIGHTIQ_AZURE_EXPECTED_MODEL_PREFIX` (default `gpt-5.4-pro`). Any other model is rejected and its output never used.

| Outcome | Handling |
|---|---|
| `completed` | parsed, validated by the caller (evidence, numbers, periods, causal and advisory language, company scope) |
| `incomplete` (`max_output_tokens` / `content_filter`) | next, smaller request profile; reported as `output_token_limit` / `content_filtered` |
| `failed` (`server_error`) | retried once |
| 429 | bounded retries honouring `Retry-After` (≤ 3 retries, ≤ 180 s in total) |
| 401 / 403 / 404 | `authentication_failed` / `deployment_unavailable`, with no retry and a clear message |
| 400 context length | next, smaller profile (`payload_too_large`) |
| timeout / connection / 5xx | retried once, then the next profile |

Diagnostics record the provider, deployment, served model, response ID, status, input, output, reasoning and total tokens, attempts, retries and duration. Prompts, model output, provider error text and keys are never recorded.

## What was migrated

| Component | Before | After |
|---|---|---|
| Financial analysis (SEC and provider) | Groq via `groq_structured` | Azure helper; deterministic claim fallback, labelled |
| News selection | direct `Groq(...)` client | Azure helper; labelled deterministic selection fallback |
| News analysis | Groq helper | Azure helper |
| Strategic analysis | Groq helper; a failure failed the whole job | Azure helper; a failure marks the section *unavailable* (no invented priorities) |
| Executive summary | direct `Groq(...)` inside the HTTP request | Azure helper, generated in the background (`POST` → 202, `GET` status) |
| Sector-comparison narrative | Groq helper | Azure helper |
| Concurrency | one process-wide lock around every call | bounded semaphore (`INSIGHTIQ_AZURE_MAX_CONCURRENT_CALLS`, default 2) |
| Jobs | no deadline, no cancellation | job deadline (`INSIGHTIQ_RESEARCH_JOB_DEADLINE_SECONDS`), `POST /api/v1/jobs/{id}/cancel` (also cancels the in-flight Azure response) |
| Startup checks | required `GROQ_API_KEY` | require the three Azure variables; `/health/ready` reports AI configuration (no call) |
| Removed | `groq_structured.py`, `groq_rate_limit.py`, unused `groq_client.py`, the legacy pilot strategic graph, `financial_claims.request_grounded_claims`, `scripts/measure_news_request.py`, the `groq` package | — |
| Frontend | "Groq" hardcoded in 7 places | provider-neutral text; the provider label comes from each report's metadata |

Historical reports, summaries and sector snapshots are never rewritten.
- Reports created before the migration have no `generation.provider`. Their model `openai/gpt-oss-120b` identifies them, and the viewer labels them "Groq · openai/gpt-oss-120b".
- New reports carry `generation.provider = "azure_openai"`, the deployment, the served model(s), response IDs and token usage. They use the key `ai_diagnostics` instead of `groq_diagnostics`.

## Live verification

`scripts/verify_azure.py` made one small structured request. The evidence is in `docs/validation_evidence/azure_connectivity.json`.

| Check | Result |
|---|---|
| Authentication and endpoint (`*.openai.azure.com/openai/v1/`) | succeeded |
| Deployment | `gpt-5.4-pro` |
| Model that served the request | `gpt-5.4-pro` |
| Response ID | `resp_0946556276c74638006b0524f5537c81908cd588717a296a8e` |
| Status / API | `completed`, background mode + polling |
| Strict structured output | parsed; content correct (17 + 25 = 42) |
| Tokens (input / output / reasoning / total) | 87 / 57 / 16 / 144 |
| Duration | 16.2 s |

The live research runs through the browser are recorded in `docs/FINAL_READINESS_REPORT.md`.

## Cost note

GPT-5.4-Pro list price is about $30 per million input tokens and $180 per million output tokens; reasoning tokens bill as output. A research run makes up to five AI calls (financial, news selection, news analysis, strategic, executive summary). The output caps in `.env.example` bound the worst case; billing is for tokens actually used.
