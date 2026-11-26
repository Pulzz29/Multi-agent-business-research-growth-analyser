import { useEffect } from "react";
import { ArrowUpRight, Database, FileText, Newspaper, ShieldCheck } from "lucide-react";
import { Sparkline } from "../components/charts.jsx";
import { MarketOverview } from "../components/MarketOverview.jsx";
import { MARKET_NAMES, readStoredMarket, resolveMarket, storeMarket } from "../lib/marketOverviewModel.js";
import { Badge, Card, CompanyMark, EmptyState, ErrorState, Skeleton, Stat, StatusBadge } from "../components/ui.jsx";
import {
  exactUsd,
  formatCount,
  formatDate,
  formatDateTime,
  formatPct,
  formatUsd,
  periodRange,
  relativeTime,
  shortId,
  trendTone,
} from "../lib/format.js";
import { companyByTicker } from "../lib/reportModel.js";

function MetricLine({ label, metric }) {
  if (!metric) {
    return (
      <div className="metric-line">
        <span className="metric-label">{label}</span>
        <span className="metric-value muted">Unavailable</span>
      </div>
    );
  }
  return (
    <div className="metric-line">
      <span className="metric-label">{label}</span>
      <span className="metric-value" title={`${exactUsd(metric.value_usd)} · ${periodRange(metric.period_start, metric.period_end)}`}>
        {formatUsd(metric.value_usd)}
      </span>
      <span
        className={`metric-delta tone-${trendTone(metric.yoy_change_pct)}`}
        title={metric.yoy_change_pct === null ? "No comparable quarter one year earlier" : "Year-over-year, same fiscal quarter"}
      >
        {metric.yoy_change_pct === null ? "YoY n/a" : `${formatPct(metric.yoy_change_pct)} YoY`}
      </span>
    </div>
  );
}

function CompanyCard({ row, onOpen }) {
  const company = companyByTicker(row.ticker);
  const report = row.latest_completed_report;
  const revenue = row.revenue;
  return (
    <button type="button" className="company-card" onClick={onOpen} aria-label={`Open ${company?.name} company dashboard`}>
      <div className="company-card-head">
        <CompanyMark ticker={row.ticker} name={row.name} />
        <div className="company-card-name">
          <p className="company-name">{company?.name}</p>
          <p className="company-ticker">{row.ticker}</p>
        </div>
        <ArrowUpRight size={17} className="company-card-arrow" aria-hidden="true" />
      </div>

      <div className="company-card-period">
        Latest quarter: {revenue ? `${revenue.fiscal_label} · ${periodRange(revenue.period_start, revenue.period_end)}` : "Unavailable"}
      </div>
      <MetricLine label="Revenue" metric={row.revenue} />
      <MetricLine label="Operating cash flow" metric={row.operating_cash_flow} />
      {revenue?.spark?.length > 1 && (
        <div className="company-card-spark">
          <Sparkline values={revenue.spark} label={`${company?.name} revenue, last eight quarters`} width={220} height={34} />
          <span>Revenue, last 8 quarters</span>
        </div>
      )}

      <div className="company-card-foot">
        {report ? (
          <Badge tone="positive" title={formatDateTime(report.generated_at_utc)}>
            <FileText size={12} aria-hidden="true" /> Report {formatDate(report.generated_at_utc)}
          </Badge>
        ) : (
          <Badge tone="neutral">No saved report</Badge>
        )}
        <Badge tone="neutral" title={`Latest article ${formatDateTime(row.news?.latest_published_at)}`}>
          <Newspaper size={12} aria-hidden="true" /> {formatCount(row.news?.article_count)} articles
        </Badge>
        {row.forecast_available && <Badge tone="info">Forecasts</Badge>}
      </div>
    </button>
  );
}

export function OverviewPage({ token, market: routeMarket, summary, dashboard, runs, loading, error, onRetry, navigate, onAuthError }) {
  const market = resolveMarket(routeMarket, readStoredMarket());
  useEffect(() => storeMarket(market), [market]);
  const rows = summary?.companies || [];
  const completed = runs?.completed_report_count;
  const reliability = dashboard?.reliability;
  const backups = dashboard?.backup_health;
  const recent = (runs?.runs || []).filter((r) => !/\.(NS|BO)$/i.test(r.ticker)).slice(0, 6);

  const toggle = (
    <div className="market-toggle-lg" role="group" aria-label="Market">
      {["US", "IN"].map((m) => (
        <button key={m} type="button" aria-pressed={market === m} onClick={() => navigate({ page: "overview", market: m })}>
          {MARKET_NAMES[m]}
        </button>
      ))}
    </div>
  );

  if (market === "IN") {
    return (
      <div className="page">
        <header className="page-head">
          <div>
            <p className="eyebrow">Portfolio overview · {MARKET_NAMES.IN}</p>
            <h1>Company intelligence</h1>
            <p className="page-lede">
              Indian (NSE/BSE) companies from the provider data stored in InsightIQ. Amounts are in INR with Indian units; fiscal years run
              April to March.
            </p>
          </div>
          {toggle}
        </header>
        <MarketOverview token={token} market="IN" navigate={navigate} onAuthError={onAuthError} showRuns showSectors={false} />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="eyebrow">Portfolio overview · {MARKET_NAMES.US}</p>
          <h1>Company intelligence</h1>
          <p className="page-lede">
            Stored SEC financials, statistical forecasts, company news and saved multi-agent research for {formatCount(rows.length || null)} SEC-validated
            companies, plus the wider US market data below. Opening a company shows what is already stored — no new AI research is started.
          </p>
        </div>
        {toggle}
      </header>

      {error && <ErrorState message={error} onRetry={onRetry} />}

      <div className="stats-row">
        <Stat
          label="SEC-validated companies"
          value={summary ? formatCount(rows.length) : "…"}
          icon={ShieldCheck}
          sub={rows.slice(0, 6).map((r) => r.ticker).join(" · ") + (rows.length > 6 ? " …" : "")}
        />
        <Stat label="Saved research reports" value={completed === undefined ? "…" : formatCount(completed)} icon={FileText} sub={runs ? `${formatCount(runs.run_count)} research runs recorded` : null} />
        <Stat label="SEC financial facts" value={dashboard ? formatCount(dashboard.source_data?.sec_fact_count) : "…"} icon={Database} sub="Stored EDGAR company facts" />
        <Stat label="Stored news articles" value={dashboard ? formatCount(dashboard.source_data?.news_article_count) : "…"} icon={Newspaper} sub="NewsData.io records" />
      </div>

      <section aria-labelledby="companies-heading">
        <div className="section-heading">
          <div>
            <h2 id="companies-heading">Companies</h2>
            <p>Latest standalone quarter from SEC filings. Hover a value for the exact amount.</p>
          </div>
        </div>
        {loading && !rows.length ? (
          <div className="company-grid">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="company-card is-loading">
                <Skeleton lines={5} />
              </div>
            ))}
          </div>
        ) : (
          <div className="company-grid">
            {rows.map((row) => (
              <CompanyCard key={row.ticker} row={row} onOpen={() => navigate({ page: "company", ticker: row.ticker, tab: "overview" })} />
            ))}
          </div>
        )}
      </section>

      <section aria-labelledby="us-market-heading">
        <div className="section-heading">
          <div>
            <h2 id="us-market-heading">US market</h2>
            <p>All stored US companies (provider data), sector coverage and the largest companies by market cap.</p>
          </div>
        </div>
        <MarketOverview token={token} market="US" navigate={navigate} onAuthError={onAuthError} />
      </section>

      <div className="grid-2">
        <Card
          title="Recent research activity"
          subtitle="Saved runs across all companies"
          action={
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigate({ page: "activity" })}>
              View all
            </button>
          }
        >
          {!recent.length ? (
            <EmptyState title="No research runs yet">Start research from any company page.</EmptyState>
          ) : (
            <ul className="activity-list">
              {recent.map((run) => (
                <li key={run.run_id}>
                  <button
                    type="button"
                    className="activity-item"
                    disabled={!run.report_available}
                    onClick={() => navigate({ page: "company", ticker: run.ticker, tab: "research", runId: run.run_id })}
                  >
                    <CompanyMark ticker={run.ticker} size="sm" />
                    <span className="activity-main">
                      <strong>{companyByTicker(run.ticker)?.name || run.ticker}</strong>
                      <span className="muted">
                        {formatDateTime(run.created_at_utc || run.report_generated_at_utc)} · {shortId(run.run_id)}
                      </span>
                    </span>
                    <StatusBadge status={run.status} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="System health" subtitle="Reliability monitoring and verified backups">
          {!dashboard ? (
            <Skeleton lines={4} />
          ) : (
            <dl className="health-list">
              <div>
                <dt>Reliability monitor</dt>
                <dd>
                  <StatusBadge
                    status={reliability?.status === "healthy" ? "completed" : reliability?.status === "insufficient_data" ? "pending" : "unavailable"}
                    label={reliability?.status ? reliability.status.replace(/_/g, " ") : "Unavailable"}
                  />
                </dd>
              </div>
              <div>
                <dt>Recovery points</dt>
                <dd>
                  <StatusBadge
                    status={backups?.recovery_points_complete ? "verified" : "unavailable"}
                    label={backups?.recovery_points_complete ? "Complete" : "Needs attention"}
                  />
                </dd>
              </div>
              <div>
                <dt>Verified research backups</dt>
                <dd>
                  {formatCount(backups?.verified_backup_count)} of {formatCount(backups?.completed_jobs_checked)} database-recorded runs
                </dd>
              </div>
              <div>
                <dt>Latest data</dt>
                <dd>{relativeTime(rows.map((r) => r.news?.latest_retrieved_at).filter(Boolean).sort().pop()) || "Unavailable"} (news retrieval)</dd>
              </div>
            </dl>
          )}
        </Card>
      </div>
    </div>
  );
}
