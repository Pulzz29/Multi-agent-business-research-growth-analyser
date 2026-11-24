# InsightIQ

InsightIQ is a full-stack business research application built with FastAPI, React, and a three-agent research workflow.

## Features

- SEC financial data, reconciled to 10-K filings, for 44 of the 47 US universe companies (WFC, GS and XOM are not reconcilable from their SEC data)
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
  - **Generate comparison** refreshes stale company data, ranks the sector with a deterministic weighted model, and asks Azure OpenAI (GPT-5.4-Pro) to write the explanation. Every number in that text is checked against the comparison, and any company's text that fails validation falls back to a template explanation.
  - The page shows a ranking-score bar chart, a radar chart, a key-metric chart, ranked cards with strengths, weaknesses and a per-metric breakdown, the comparison table and the methodology.
  - **Download PDF / DOCX** renders the saved comparison.
- **Saved comparisons are immutable.** Each one is written to `reports/sector_comparisons/<id>.json`; reopening it or downloading its report makes no provider or AI calls.

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

### Compare selected companies (custom comparison)

On a sector page, switch from **Entire sector** to **Selected companies** to compare a hand-picked subset of that sector.

**Using it**
- Filter the sector's companies by name or symbol, tick the ones you want, or use **Select all** (capped at the maximum) and **Clear**.
- A live counter shows the selection ("3 selected (min 2, max 6)"). **Compare** stays disabled, with the reason shown, until the count is within the limits.
- Your selection is kept when you switch modes and cleared when you change sector.
- After a comparison, the address bar holds the selection (for example `#/markets/US/sectors/technology?companies=AAPL,MSFT,NVDA`). Opening that link starts in this mode with those companies ticked; it does not run a comparison by itself.

**What is different from a whole-sector comparison**
- Only the selected companies are ranked, and scores are normalised **over the selection**: min–max, medians for missing values, and ranks are computed among those companies only. The sector's weights are still used.
- The result and the report carry the note: *"Scores are relative to the selected companies and may differ from the full sector ranking."*
- Only the selected companies' data is refreshed, with the same sources and freshness rules as a normal comparison (profile, quote and statements; no news). The rest of the sector is never fetched.
- The snapshot is saved with `comparison_type: "custom"`, plus `selected_companies` and `duplicates_removed`. It never replaces the sector's latest comparison; the history lists both kinds.
- The PDF/DOCX is titled **"Custom comparison: \<Sector\>, N companies"**, lists the selected companies and repeats the note. The file name ends in `_custom_<date>`.

**Limits and validation**
- `INSIGHTIQ_CUSTOM_COMPARISON_MIN_COMPANIES` (default 2) and `INSIGHTIQ_CUSTOM_COMPARISON_MAX_COMPANIES` (default 6) are the only place the limits are set. The frontend reads them from `GET /api/v1/markets/{market}/sectors` (`custom_comparison`).
- API: `POST /api/v1/markets/{market}/sectors/{sector}/comparison` with `{"refresh": true, "companies": ["AAPL", "MSFT"]}`. Without `companies`, the whole sector is compared as before.
- Symbols are trimmed, upper-cased and de-duplicated. The server answers **400** with a clear message when:
  - a symbol is invalid or not in the sector;
  - fewer than the minimum distinct companies remain;
  - there are more than the maximum.
- Companies without stored data are listed and flagged, not ranked. If fewer than two of them can be ranked, the status is `insufficient_companies`.

**Companies from other sectors.** These cannot be added. Weights are set per sector (Banking, for example, scores price-to-book instead of leverage), so ranking a company from another sector against these peers would be misleading. A company found through provider search *is* selectable when its provider sector maps to this sector, because it then appears in the sector's list (marked "added").

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

**Tests:** `tests\test_provider_research.py`.

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

**Tests:** `tests\test_company_data_refresh.py` and `frontend\tests\freshness.test.mjs`.

### Run and test

```powershell
.\.venv\Scripts\python.exe scripts\run_backend.py      # backend (reads .env)
cd frontend; npm run dev                               # dashboard at http://127.0.0.1:5173
.\.venv\Scripts\python.exe -m pytest -q tests\test_ranking_service.py tests\test_market_providers.py tests\test_sector_api.py
cd frontend; npm test
```

The new tests run fully offline. They use test-only fake providers and a fake Azure OpenAI Responses client on an isolated copy of the database.

## Research report features

Every saved research report (`#/companies/<TICKER>/reports/<run_id>`) now has charts, a growth analysis, an AI summary with thorough research, and a PDF download. One frozen dataset per report feeds all four, so they always show the same numbers.

**Per-report dataset.** `GET /api/v1/companies/{ticker}/reports/{run_id}/insights` computes it once, point-in-time (SEC facts filed and provider rows stored by the report's generation time), and saves it to `reports/research_runs/insights/{run_id}.json`. It makes no external or AI calls.

**Charts** (custom SVG, `frontend/src/components/ReportCharts.jsx`). When there is not enough data, a chart shows "Not enough data to chart" with the reason.

| Chart | Notes |
|---|---|
| Revenue and net income | Quarterly or annual toggle |
| Margin trends | Gross, operating and net; gross is omitted with a reason when gross profit is not stored |
| Growth rates | Year-over-year revenue and net profit growth |
| Key ratios vs sector median | P/E, ROE, margins and debt-to-equity; the median covers the sector's stored companies, with N shown |
| Price history | Always an empty state with the latest stored quote: InsightIQ stores no price history, and fetching one would be a new external call |

**Growth analysis** (`backend/app/services/growth_analysis.py`; also `GET /api/v1/companies/{ticker}/growth` and the company pages).

| Metric | Definition |
|---|---|
| Revenue QoQ | Against the adjacent quarter |
| Revenue YoY, net profit YoY, EPS YoY | Against the same fiscal quarter one year earlier |
| Annual YoY | Against the prior fiscal year |
| CAGR 3Y / 5Y | Needs fiscal years exactly N years apart |

- **Base of zero or a loss:** gives "n/a" with the reason, never NaN or Infinity.
- **Short history:** shows "Insufficient history (need N, have M)". Quarterly YoY needs 5 quarters; CAGR over N years needs N+1 fiscal years.
- **Trend label:** accelerating, stable or decelerating, by a ±2 percentage-point rule comparing the latest YoY rate with the average of the previous two.
- **Data age:** stale data is flagged.
- **EPS:** stored from existing SEC and provider responses at each company's next refresh.

**AI summary and thorough research** (`executive_summary.generate_brief`). One Azure OpenAI call returns two parts:
- the 150–250 word summary;
- research sections: business overview, financial performance, growth analysis, valuation snapshot, recent news, key risks, outlook.

The model receives InsightIQ's computed figures and must use them as given, say "not available" when something is missing, and cite evidence IDs. Numbers, evidence IDs, language and company scope are validated; a failing research section is withheld individually.

How generation runs:
- **Automatic:** starts in the background when the **latest** report is opened.
- **Button:** "Generate summary" / "Regenerate" forces a new version.
- **Research job:** a new research run generates it on completion.
- **Shared:** all three use the same code and a per-report registry, so a second request reuses the running one.

A cached result is reused while the report and its dataset are unchanged. A failure keeps the previous version visible, with Retry, and up to three earlier versions are kept. The panel shows "Generated on … · based on data as of …", a Sources list and an "AI-generated, not investment advice" note.

**PDF.** The **Download report** button calls `GET /api/v1/companies/{ticker}/reports/{run_id}/pdf` and saves `<Company>_Research_Report_<YYYY-MM-DD>.pdf`.
- Contents: company and dates, AI summary and research, charts, the growth and ratio tables, news sources and a disclaimer.
- Charts are drawn server-side with reportlab vector graphics from the same dataset, so no browser is needed and the output is consistent. Pages are numbered.
- If the AI summary is not generated yet, the PDF says so; download again later.

**Try it:** open `#/companies/MSFT/financials` for the growth card, or the latest MSFT report for the charts, summary and download. Tests: `tests/test_growth_analysis.py`, `tests/test_report_insights_pdf.py`, `tests/test_research_brief.py` and `frontend/tests/{growthModel,reportCharts,briefModel}.test.mjs`.

## AI provider: Azure OpenAI GPT-5.4-Pro

All AI text (financial, news, strategic, executive summary, sector narrative) is generated by an Azure OpenAI GPT-5.4-Pro deployment through the Responses API, from the backend only. Configure it in `.env`:

```
AZURE_OPENAI_API_KEY=<key from Microsoft Foundry>
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/
AZURE_OPENAI_DEPLOYMENT=<your deployment name, e.g. gpt-6.2-sol>
INSIGHTIQ_AZURE_REASONING_EFFORT=low   # minimal | low | medium | high | xhigh, as the model supports
```

Responses must come from the model named by the deployment; if the deployment name differs from the model name, also set `INSIGHTIQ_AZURE_EXPECTED_MODEL_PREFIX`. Restart the backend after editing `.env`.

Actions that use AI or providers (Generate summary, sector comparison, Load market data) run on click with no browser confirmation dialog; what each one costs is shown as a small note beside the button, and failures appear inline on the page.

Optional tuning (`INSIGHTIQ_AZURE_*`) is documented in `.env.example`. `scripts\verify_azure.py` makes one small live call to check the configuration. Details: `docs/AZURE_MIGRATION_REPORT.md`.

## Current status

See `docs/FINAL_READINESS_REPORT.md` for the latest validation results, known limitations and external blockers, and `docs/DEMO_RUNBOOK.md` for running a demo.
