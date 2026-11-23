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

## Current status

The local application has passed backend, API, agent, data-integrity, integration, and isolated database-recovery tests.

Full financial accuracy certification, remaining visual checks, and production deployment are still pending.
