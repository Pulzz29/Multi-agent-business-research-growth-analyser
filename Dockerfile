# InsightIQ image (PostgreSQL backend): FastAPI plus the production React build,
# served same-origin by the API. Build from the project root:
#   docker build -t insightiq:local .      (or: az acr build -t insightiq:<tag> .)
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    INSIGHTIQ_PROJECT_ROOT=/app \
    INSIGHTIQ_API_HOST=0.0.0.0 \
    INSIGHTIQ_API_PORT=8000 \
    DB_BACKEND=postgres \
    PG_BIN_DIR=/usr/bin \
    INSIGHTIQ_STATIC_DIR=/app/web \
    BACKUP_DIR=/tmp/insightiq/pg_backups

# PostgreSQL client tools are used by the backup CronJob (pg_dump/pg_restore).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --upgrade pip && pip install -r backend/requirements.txt

COPY backend/ backend/
COPY scripts/ scripts/
# Reconciliation evidence read at runtime by the forecast cache (no secrets).
COPY reports/financial_evidence_reconciliation_final.json reports/forecast_uncertainty_quality_review.json reports/
COPY --from=web /web/dist /app/web

RUN useradd --system --uid 10001 --home /app insightiq \
    && mkdir -p /app/data /app/logs /tmp/insightiq \
    && chown -R insightiq /app/data /app/logs /tmp/insightiq
USER 10001

WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "app.container_entrypoint"]
