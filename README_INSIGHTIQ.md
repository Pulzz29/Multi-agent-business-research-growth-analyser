# InsightIQ

InsightIQ is a full-stack business research application built with FastAPI, React, and a three-agent research workflow.

## Features

- SEC financial data for AAPL, AMZN, GOOGL, META, MSFT, and NVDA
- Company news collection
- Quarterly financial normalization and forecasting
- Financial, News, and Strategic research agents
- Saved research jobs and reports
- Execution tracing and reliability monitoring
- SQLite persistence and verified Google Drive backups
- Responsive dashboard with dark and light themes
- US and Indian markets: top companies, company search and provider data
- Sector-wise comparison with an explainable ranking and PDF/DOCX reports

## Application structure

- `backend/app/` — FastAPI, data services, and research agents
- `frontend/src/` — React dashboard
- `frontend/dist/` — Compiled frontend
- `data/` — Stored source data, forecasts, and backups
- `reports/` — Research and verification reports

## Start in Google Colab

Run the notebook's **Cell 44 — Reusable Startup and Recovery**.

It loads the saved project, verifies the database, starts the backend and dashboard, and opens the browser preview.

The dashboard connects using the InsightIQ access key configured in Colab.

## Markets and Sector-wise Comparison

After connecting with the access key, use **Markets → United States / India** or **Sector-wise Comparison** in the sidebar.

### What it does

- **Market page.** Choose US or India to see:
  - the 5 largest companies by stored market capitalisation (**Load market data** fetches the latest quotes);
  - search across the curated universe, plus live search at the market's provider for any other listed company.
- **Company page.** Shows the provider profile, quote, key ratios computed by InsightIQ, annual statements, stored news and data freshness. **Refresh data** reloads stale sources.
- **Sector-wise comparison.**
  - Pick a sector; the list comes from the backend and can be searched.
  - **Generate comparison** refreshes stale company data, ranks the sector with a deterministic weighted model, and asks Groq to write the explanation. Every number in that text is checked against the comparison, and any company's text that fails validation falls back to a template explanation.
  - The page shows a ranking-score bar chart, a radar chart, a key-metric chart, ranked cards with strengths, weaknesses and a per-metric breakdown, the comparison table and the methodology.
  - **Download PDF / DOCX** renders the saved comparison.
- **Saved comparisons are immutable.** Each one is written to `reports/sector_comparisons/<id>.json`; reopening it or downloading its report makes no provider or Groq calls.

### Data sources

| Market | Quotes and metrics | Statements and profile | News |
|---|---|---|---|
| US | Finnhub | Financial Modeling Prep | Finnhub |
| India | Yahoo Finance (yfinance) | Yahoo Finance (yfinance) | NewsData.io |

- Set `FINNHUB_API_KEY` and `FMP_API_KEY` in `.env`. yfinance needs no key, but it is an unofficial client: it can be throttled or break, and Yahoo's terms restrict automated use.
- When a key is missing, that source reports "not configured" instead of failing.
- FMP's free tier is protected by a daily request budget (`INSIGHTIQ_FMP_DAILY_REQUEST_BUDGET`).
- The company universe and sectors live in `backend/app/core/market_universe.json`. Edit that file to change them.

### Ranking methodology (`backend/app/services/ranking_service.py`)

**Metrics**
- Computed by InsightIQ from the latest two annual statements: revenue growth, net margin, operating margin, ROE and ROA (average equity/assets where available), and debt-to-equity.
- From the latest quote: P/E, price-to-book, dividend yield, market cap and beta.

**Scoring**
1. **Normalise:** min–max within the sector to 0–100, inverted where lower is better (debt-to-equity, P/E, P/B, beta). If every company has the same value, all get 50.
2. **Weight:** weights live in `backend/app/core/ranking_weights.json`, with a Banking override that drops debt-to-equity and operating margin and adds P/B. Metrics with data for fewer than two companies are dropped and the remaining weights re-normalised.
3. **Missing values:** replaced by the sector median and flagged `imputed`. A company whose real data covers less than 60% of the applied weight is listed but not ranked.
4. **Ties:** scores are rounded to 2 decimals, and equal scores share a rank (1, 1, 3).

**Edge cases**
- A P/E of zero or below, or debt-to-equity with non-positive equity, counts as missing.
- Fewer than two rankable companies means status `insufficient_companies`.

The LLM never computes or changes a score or a rank.

### AI research for any company (Phase B)

Open a company from the market page. After its data is loaded, click **Research this company** to run the same Financial, News and Strategic agents. The summary agent then writes the executive summary sidecar.

**Where the data comes from**
- **The six original companies** keep their SEC-validated pipeline.
- **Every other company** uses its market provider's statements: FMP for the US, Yahoo Finance for India.
- **Labelling:** the report states the provider and currency (USD or INR) and that the figures are not SEC-validated.

**How the evidence is used**
- **Period type:** quarterly data is used when at least 4 quarters are stored, otherwise annual data. Period starts are derived from the previous reported period and labelled as derived.
- **Re-checking:** every value is re-checked against its stored provider row.
- **AI-written text:** AI claims must copy values exactly, and every AI-written finding passes the same number, period, citation and language checks as the SEC path.
- **Forecasts:** statistical forecasts run only with at least 20 consecutive quarters. Otherwise the report gives the reason they are unavailable.
- **No company news:** if a provider company has no company news stored, the research continues on financial evidence only and says so.

**Quote fallback:** if Finnhub rejects a quote (for example an invalid key), the US quote falls back to FMP's company profile, labelled as FMP.

**Tests:** `tests	est_provider_research.py`.

### Automatic company data refresh

Opening a company page (or picking a company from search) sends one request to `POST /api/v1/markets/{market}/companies/{symbol}/data`. The **server** decides what happens, in `backend/app/services/company_data.py` using `needs_refresh()` from `data_freshness.py`:

| Stored data | What happens |
|---|---|
| Never refreshed | Fetched immediately; the page shows "Fetching latest data…" |
| Last refresh **more than** `DATA_REFRESH_TTL_HOURS` ago (default 6) | Fetched automatically |
| 6 h or newer | Served from storage; no provider call |

- **Refresh data button:** sends `mode: "force"` and always refreshes. A second click within `INSIGHTIQ_FORCE_REFRESH_COOLDOWN_SECONDS` (default 60) shows a message instead of calling the providers again.
- **What counts as a successful refresh:** profile, quote and statements all refreshed. A news failure is only a warning.
- **Timestamp:** only a successful refresh updates `last_refreshed_at`, which is stored in UTC on `market_companies`.
- **When a refresh fails:**
  - If data is stored, the page keeps showing it with a stale-data warning (`stale: true`, `refresh_error`).
  - If nothing is stored, the API returns 502 with a clear message.
- **Duplicate requests:** concurrent requests for the same company (double click, two tabs, React StrictMode) share one refresh.
- **Freshness line:** the page always shows "Last updated: <relative> (<local time>)".
- **Logging:** each decision is logged as `company_data symbol=… decision=…`.

**Try it manually**
1. Search for and open a new company → loading message, then "Last updated: just now".
2. Reopen it → instant, with no provider call.
3. Set `DATA_REFRESH_TTL_HOURS=0.0167` in `.env`, restart the backend, wait about 1 minute and reopen → automatic refresh.
4. Click Refresh data twice quickly → one refresh, then the cooldown message.

**Tests:** `tests	est_company_data_refresh.py` and `frontend	estsreshness.test.mjs`.

### Run and test

```powershell
.\.venv\Scripts\python.exe scripts\run_backend.py      # backend (reads .env)
cd frontend; npm run dev                               # dashboard at http://127.0.0.1:5173
.\.venv\Scripts\python.exe -m pytest -q tests\test_ranking_service.py tests\test_market_providers.py tests\test_sector_api.py
cd frontend; npm test
```

The new tests run fully offline. They use test-only fake providers and a fake Groq client on an isolated copy of the database.

## Current status

The local application has passed backend, API, agent, data-integrity, integration, and isolated database-recovery tests.

Full financial accuracy certification, remaining visual checks, and production deployment are still pending.
