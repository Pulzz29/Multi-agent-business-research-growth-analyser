# InsightIQ demo runbook

## 1. Start the application (verified 2026-11-24)

Run from the project folder `C:\Users\Administrator\Downloads\InsightIQ_Capstone_Project (2)\InsightIQ_Capstone_Project\InsightIQ`.

```powershell
# Terminal 1: backend (reads .env; startup recovery and verification run first)
.\.venv\Scripts\python.exe scripts\run_backend.py

# Terminal 2: frontend
cd frontend
npm run dev
```

Alternatively, `scripts\start_all.ps1` opens both windows.

| Check | URL / result |
|---|---|
| Backend ready | `http://127.0.0.1:8000/health/ready` reports `"status":"ready"` and `"ai_provider":{"provider":"azure_openai","deployment":"gpt-5.4-pro","configured":true}` |
| Application | `http://127.0.0.1:5173` |
| Sign in | paste the `API_ACCESS_KEY` value from `.env` into the password field and click **Connect**. The key is kept only for that browser tab. |

## 2. Suggested demo path (about 10 minutes)

1. **Overview:** the portfolio of SEC-validated companies (44 US companies).
2. **Oracle (`#/companies/ORCL`):**
   - SEC-validated financials (48 reconciled revenue quarters), with the CIK in the header.
   - **Forecasts tab:** model chosen on point-in-time folds and the held-out error against the baseline. The 95% interval is shown only if its out-of-sample coverage passed the gate.
   - **News tab:** only headlines in which Oracle Corporation is the subject.
3. **Nvidia forecasts (`#/companies/NVDA/forecasts`):** an example of an interval **withheld**. The interval covered only 3 of 12 out-of-sample quarters, and the page says so.
4. **Microsoft research (`#/companies/MSFT/research`):**
   - Open **Report history**: older reports are labelled "Groq · openai/gpt-oss-120b".
   - The new report is labelled "Azure OpenAI · gpt-5.4-pro", with response IDs and token usage under *Generation and refresh details*.
   - **Executive summary:** the AI summary plus **Thorough research** (seven sections), "Generated on … · based on data as of …", the **Sources** list, and **Regenerate**, which costs tokens.
   - **Charts:** revenue and net income, margins, growth rates, and ratios vs the Technology median. The price chart is an explained empty state, because price history is not stored.
   - **Growth analysis:** metrics, trend label and rule.
   - **Download report** gives the PDF with charts.
   - **Start research** creates a new run. GPT-5.4-Pro can take several minutes per step; the run can be cancelled.
5. **Growth Analyser fixes:**
   - `#/companies/RIVN/financials`: net-profit growth is "n/a: base is a loss" instead of a meaningless percentage.
   - `#/companies/JPM`: a warning that the latest stored SEC quarter is from 2014.
   - `#/markets/IN/companies/NESTLEIND.NS`: a note that a newer fiscal year is incomplete.
6. **Markets → India (`#/markets/IN`):** top 5 by market capitalisation, then open **ITC.NS**. GST "input tax credit" stories are not shown as ITC Ltd news.
7. **Sector-wise Comparison → US → Technology:** whole-sector ranking, then **Selected companies** to compare 2–6 companies. PDF/DOCX download.

## 3. Things to say accurately

- Forecasts are statistical estimates from SEC-reported quarters, never reported results.
- AI text passes automated checks (evidence IDs, numbers, periods, causal and advisory language, company scope). It is not independently verified analysis.
- Indian companies have provider financials and news but no forecasts: provider statements have no filing dates, so a point-in-time validated forecast cannot be built.
- Finnhub currently rejects the configured key (HTTP 401). US quotes on Nov 23 came from Finnhub; today's fallback is FMP within its daily budget.
- Opening a company's **latest** report generates its AI summary and research automatically if none is current (one Azure call, a few minutes). Older reports only generate when you press the button.

## 4. Useful commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q                       # isolated + live-API suite (backend must be running)
.\.venv\Scripts\python.exe -m pytest -m e2e tests\e2e         # real-browser checks (backend + frontend running)
cd frontend; npm test; npm run build                           # frontend tests and production build
.\.venv\Scripts\python.exe scripts\verify_azure.py            # one small live Azure call (costs tokens)
.\.venv\Scripts\python.exe scripts\company_readiness.py       # regenerate docs\COMPANY_READINESS_MATRIX.csv
.\.venv\Scripts\python.exe scripts\verify_report_brief_live.py MSFT:<run_id>   # live AI brief check (costs tokens)
```
