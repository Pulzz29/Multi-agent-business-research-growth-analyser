# Phase A — Markets and Sector-wise Comparison: status

Date: 2026-11-23. The feature description, ranking methodology and run instructions are in `README_INSIGHTIQ.md` ("Markets and Sector-wise Comparison"). This page records how it was verified and what is still open.

**Live status:** verified offline only. No Finnhub, FMP, Yahoo Finance, NewsData.io or Groq request has been made for this feature. Live loading needs your API keys and approval.

## Delivered

- **Market switch:** US / India, with the 5 largest companies by market cap.
- **Company search:** across the curated universe and at the market's data provider, plus a company profile page.
- **Sector-wise comparison:**
  - sector list with search;
  - deterministic weighted ranking with a score breakdown for each company;
  - Groq explanation that is validated before display, with a template fallback for each company;
  - three charts, a comparison table and the methodology;
  - immutable saved comparisons;
  - PDF and DOCX downloads.

## Verification

### Tests run

| Check | Result |
|---|---|
| Full backend suite | **334 passed, 3 skipped, 0 failed** (272 before Phase A; the skips are unchanged) |
| New ranking tests (`test_ranking_service.py`) | 16 passed |
| New provider tests (`test_market_providers.py`) | 14 passed |
| New ingestion, comparison, API and report tests (`test_sector_api.py`) | 22 passed |
| Frontend production build | Success |
| Frontend tests (`npm test`) | 7 passed, including 4 for the new comparison screen |
| Dependency check (`pip check`) | Clean after adding reportlab 5.1.0, python-docx 1.2.0 and yfinance 1.8.0 (pinned in `requirements.lock.txt`) |

### Data preserved

A SHA-256 comparison before and after confirmed:
- all 87 report, forecast-cache and database snapshot files are byte-identical;
- every existing database row is unchanged.

The runtime database gained four new, empty tables: `market_companies`, `market_quotes`, `market_statements` and `provider_usage`.

### Not checked

- **The PDF has not been visually inspected.** No PDF viewer is installed on this machine. Tests check that it is a valid PDF and that the DOCX contains the right content.
- **No live provider calls have been made.**

## Gates and open items

1. **API keys.** Add `FINNHUB_API_KEY` and `FMP_API_KEY` to `.env`. Without them, US sources show "not configured".
2. **Live load needs your approval.** The proposed first load:
   - US: "Load market data" (about 2 Finnhub calls per company) and a Technology comparison (about 4 FMP calls per company);
   - India: the same with Yahoo Finance;
   - then one Groq call per comparison.
3. **FMP free tier.** About 250 requests a day; quarterly statements may need a paid plan. The app stores annual statements and labels what is missing.
4. **yfinance.** It is unofficial and may be throttled. Some NSE symbols in the curated universe may have changed (for example after a renaming); such companies appear as "no data" and are not ranked.
5. **Phase B is now delivered (see below).** "Research this company" opens the SEC research dashboard for the six SEC-validated companies, and runs provider-data research for all other loaded companies.
6. **Data source for those six companies.** In sector comparisons they use FMP statements like their peers, for consistency. Their research dashboards still use SEC EDGAR.

## Phase B — research for any company (2026-11-23)

### Delivered

- **Research for any loaded company.** The research pipeline accepts any registered market company:
  - a registry check replaces the hardcoded six tickers in jobs, history, reports, summaries and the dashboard;
  - the SEC-only dashboard routes are unchanged.
- **Provider Financial Agent** (`backend/app/agents/provider_financial.py`):
  - claims are re-checked against stored provider rows;
  - comparisons are computed in Python;
  - findings are validated one by one;
  - claims fall back to deterministic selection if Groq fails;
  - forecasts run only with at least 20 consecutive quarters.
- **News and Strategic Agents** attribute headlines using names from the registry. Provider companies without news continue on financial evidence, and this is labelled.
- **Reports:**
  - each report states its research profile (SEC or provider), market and currency, plus provider freshness and the reasons any forecasts are unavailable;
  - the executive summary writes amounts in the report's currency.
- **Dashboard:**
  - **Research this company** on market company pages, with live agent progress, the report viewer and run history;
  - the report viewer shows amounts in the right currency and labels the data provider.

### Verification (tests run)

| Check | Result |
|---|---|
| Full backend suite (with the backend running on port 8000 for the live API tests) | **345 passed, 3 skipped, 0 failed** |
| Phase B tests (`test_provider_research.py`) | 10 passed |
| Finnhub-to-FMP quote fallback test | 1 passed |
| Frontend production build | Success |
| Frontend tests | 10 passed, including 3 provider-report tests |

**Updated tests.** Two existing tests used `TSLA` as an "unsupported" ticker. TSLA is now a valid registered company, so they were switched to `ZZZZ`. This also stops the security test from ever starting a real research job.

**Data preserved.** All original reports, forecast caches and baseline database rows are unchanged; only additions exist.

### Live observations (from your own use of Phase A)

- **US quotes:** Finnhub rejected all 47 requests with HTTP 401, which means the key is not accepted. Please check `FINNHUB_API_KEY` on the Finnhub dashboard. Its stored value is 32 characters, which is unusual for a Finnhub key.
- **Indian IT sector:** 5 of 6 companies loaded and a comparison was saved. `LTIM.NS` returned no data from Yahoo Finance; the symbol may have changed.

### Not executed

- No live research run for a provider company (it needs your approval and uses Groq).
- No visual browser inspection.
