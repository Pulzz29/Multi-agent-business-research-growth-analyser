import { useEffect } from "react";
import { MarketOverview } from "../components/MarketOverview.jsx";
import { ChatWidget } from "../components/ChatWidget.jsx";
import { MARKET_NAMES, readStoredMarket, resolveMarket, storeMarket } from "../lib/marketOverviewModel.js";
import { Card, CompanyMark, EmptyState, ErrorState, Skeleton, StatusBadge } from "../components/ui.jsx";
import { formatCount, formatDateTime, relativeTime, shortId } from "../lib/format.js";
import { companyByTicker } from "../lib/reportModel.js";

export function OverviewPage({ token, market: routeMarket, summary, dashboard, runs, loading, error, onRetry, navigate, onAuthError }) {
  const market = resolveMarket(routeMarket, readStoredMarket());
  useEffect(() => storeMarket(market), [market]);
  const rows = summary?.companies || [];
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
            <h1>Company Intelligence</h1>
            <p className="page-lede">
              Indian (NSE/BSE) companies from the provider data stored in InsightIQ. Amounts are in INR with Indian units; fiscal years run
              April to March.
            </p>
          </div>
          {toggle}
        </header>
        <MarketOverview token={token} market="IN" navigate={navigate} onAuthError={onAuthError} showRuns />
        <ChatWidget token={token} navigate={navigate} onAuthError={onAuthError} />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="eyebrow">Portfolio overview · {MARKET_NAMES.US}</p>
          <h1>Company Intelligence</h1>
          <p className="page-lede">
            Stored SEC financials, statistical forecasts, company news and saved multi-agent research for {formatCount(rows.length || null)} SEC-validated
            companies, plus the wider US market data below. Opening a company shows what is already stored — no new AI research is started.
          </p>
        </div>
        {toggle}
      </header>

      {error && <ErrorState message={error} onRetry={onRetry} />}

      <section aria-labelledby="us-market-heading">
        <div className="section-heading">
          <div>
            <h2 id="us-market-heading">US market</h2>
            <p>Search every stored US company and see the largest companies by market capitalisation.</p>
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
      <ChatWidget token={token} navigate={navigate} onAuthError={onAuthError} />
    </div>
  );
}
