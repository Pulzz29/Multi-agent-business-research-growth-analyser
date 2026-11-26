import { useEffect, useRef, useState } from "react";
import { Building2, Database, FileText, Search } from "lucide-react";
import { BarChart } from "./charts.jsx";
import { Card, CompanyMark, EmptyState, ErrorState, Skeleton, Stat, StatusBadge } from "./ui.jsx";
import { api } from "../lib/api.js";
import { formatCount, formatDateTime, formatMoney, formatPerShare, relativeTime, shortId } from "../lib/format.js";
import { MARKET_NAMES, forMarket, sectorBars } from "../lib/marketOverviewModel.js";

/** Market-specific Overview section (US or IN): all values come from the backend. */
// showSectors=false (Indian Overview) hides the sector breakdown chart and gives
// the company search the full width; sector data and pages are unchanged.
export function MarketOverview({ token, market, navigate, onAuthError, showRuns, showSectors = true }) {
  const [state, setState] = useState({ loading: true, error: "", data: null });
  const [query, setQuery] = useState("");
  const [results, setResults] = useState({ market, items: [], loading: false });
  const searchSeq = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, error: "", data: null });
    setQuery("");
    setResults({ market, items: [], loading: false });
    api
      .marketOverview(token, market, controller.signal)
      .then((data) => setState({ loading: false, error: "", data }))
      .catch((err) => {
        if (err?.name === "AbortError") return;
        if (err.status === 401) onAuthError?.();
        setState({ loading: false, error: err.message, data: null });
      });
    return () => controller.abort();
  }, [token, market, onAuthError]);

  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setResults({ market, items: [], loading: false });
      return undefined;
    }
    const seq = ++searchSeq.current;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setResults((r) => ({ ...r, loading: true }));
      api
        .searchCompanies(token, market, q, controller.signal)
        .then((d) => seq === searchSeq.current && setResults({ market, items: d.results || [], loading: false }))
        .catch((e) => e?.name !== "AbortError" && setResults({ market, items: [], loading: false }));
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [token, market, query]);

  const open = (symbol) => navigate({ page: "market-company", market, symbol });
  const data = forMarket(state.data, market);
  if (state.loading) return <Card title={MARKET_NAMES[market]}><Skeleton lines={5} /></Card>;
  if (state.error) return <ErrorState message={state.error} />;
  if (!data) return null;
  const c = data.counts;

  return (
    <section aria-label={`${MARKET_NAMES[market]} overview`}>
      <div className="stats-row">
        <Stat label="Companies in universe" value={formatCount(c.universe_companies)} icon={Building2} sub={`${formatCount(c.sectors)} sectors`} />
        <Stat label="Companies with data" value={formatCount(c.with_statements)} icon={Database} sub={`${formatCount(c.stored_companies)} loaded · ${formatCount(c.with_quotes)} with quotes`} />
        <Stat label="Research runs" value={formatCount(c.research_runs)} icon={FileText} sub={`${formatCount(c.completed_research_runs)} completed`} />
        <Stat label="Currency" value={data.currency} sub={market === "IN" ? "Indian units: crore / lakh crore" : "Billions / trillions"} />
      </div>

      <div className={showSectors ? "grid-2" : undefined}>
        {showSectors && (
        <Card title="Sector breakdown" subtitle="Curated universe vs companies with data loaded">
          {data.sectors.length ? (
            <BarChart
              data={sectorBars(data)}
              yFormat={(v) => String(Math.round(v))}
              ariaLabel={`${MARKET_NAMES[market]} companies per sector`}
              legend={[
                { id: "universe", label: "Universe", tone: "secondary" },
                { id: "loaded", label: "Loaded", tone: "primary" },
              ]}
            />
          ) : (
            <EmptyState title="No sectors configured" />
          )}
        </Card>
        )}
        <Card title="Find a company" subtitle={`Search the ${MARKET_NAMES[market]} (stored and curated companies)`}>
          <label className="search-field">
            <Search size={15} aria-hidden="true" />
            <input type="search" value={query} placeholder={market === "IN" ? "e.g. Infosys or INFY.NS" : "e.g. Apple or AAPL"} onChange={(e) => setQuery(e.target.value)} aria-label="Search companies" />
          </label>
          {results.loading && <Skeleton lines={2} />}
          {results.market === market && results.items.length > 0 && (
            <ul className={showSectors ? "activity-list" : "activity-list search-results-wide"}>
              {results.items.slice(0, showSectors ? 8 : 12).map((r) => (
                <li key={r.symbol}>
                  <button type="button" className="activity-item" onClick={() => open(r.symbol)}>
                    <CompanyMark ticker={r.symbol} name={r.name} size="sm" />
                    <span className="activity-main">
                      <strong>{r.name}</strong>
                      <span className="muted">{r.symbol}{r.sector_label ? ` · ${r.sector_label}` : ""}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {query.trim().length >= 2 && !results.loading && results.market === market && !results.items.length && (
            <p className="small muted">No {MARKET_NAMES[market]} companies match “{query.trim()}”.</p>
          )}
        </Card>
      </div>

      <Card title="Largest companies" subtitle={`By latest stored market capitalisation (${data.currency})`}>
        {!data.top_companies.length ? (
          <EmptyState title="No market data loaded yet">Open the {MARKET_NAMES[market]} page and load market data.</EmptyState>
        ) : (
          <div className="top-company-grid">
            {data.top_companies.map((t) => (
              <button key={t.symbol} type="button" className="top-company-card" onClick={() => open(t.symbol)}>
                <CompanyMark ticker={t.symbol} name={t.name} />
                <span>
                  <strong>{t.name}</strong>
                  <span className="small muted" style={{ display: "block" }}>
                    {t.symbol} · {t.sector_label || "sector n/a"}
                  </span>
                  <span className="small">
                    {formatMoney(t.market_cap, t.currency)} · {formatPerShare(t.price, t.currency)} · {relativeTime(t.quote_fetched_at)}
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
      </Card>

      {showRuns && (
        <Card title="Recent research" subtitle={`${MARKET_NAMES[market]} research runs`}>
          {!data.recent_runs.length ? (
            <EmptyState title="No research runs for this market yet" />
          ) : (
            <ul className="activity-list">
              {data.recent_runs.map((run) => (
                <li key={run.run_id}>
                  <button
                    type="button"
                    className="activity-item"
                    disabled={run.status !== "completed"}
                    onClick={() => navigate({ page: "market-company", market, symbol: run.ticker, tab: "research", runId: run.run_id })}
                  >
                    <CompanyMark ticker={run.ticker} name={run.name} size="sm" />
                    <span className="activity-main">
                      <strong>{run.name || run.ticker}</strong>
                      <span className="muted">
                        {formatDateTime(run.created_at_utc)} · {shortId(run.run_id)}
                      </span>
                    </span>
                    <StatusBadge status={run.status} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}
    </section>
  );
}
