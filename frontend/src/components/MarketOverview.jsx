import { useEffect, useRef, useState } from "react";
import { Search } from "lucide-react";
import { Card, CompanyMark, EmptyState, ErrorState, Skeleton, StatusBadge } from "./ui.jsx";
import { api } from "../lib/api.js";
import { formatDateTime, formatMoney, formatPerShare, relativeTime, shortId } from "../lib/format.js";
import { MARKET_NAMES, forMarket } from "../lib/marketOverviewModel.js";

/** Market-specific Overview section (US or IN): all values come from the backend. */
// Overview shows company search and the largest companies (genuine stored market
// caps only); sector analysis stays on the Sector-wise Comparison pages.
export function MarketOverview({ token, market, navigate, onAuthError, showRuns }) {
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
  const top = data.top_companies;

  return (
    <section aria-label={`${MARKET_NAMES[market]} overview`}>
        <Card title="Find a company" subtitle={`Search the ${MARKET_NAMES[market]} (stored and curated companies)`}>
          <label className="search-field">
            <Search size={15} aria-hidden="true" />
            <input type="search" value={query} placeholder={market === "IN" ? "e.g. Infosys or INFY.NS" : "e.g. Apple or AAPL"} onChange={(e) => setQuery(e.target.value)} aria-label="Search companies" />
          </label>
          {results.loading && <Skeleton lines={2} />}
          {results.market === market && results.items.length > 0 && (
            <ul className="activity-list search-results-wide">
              {results.items.slice(0, 12).map((r) => (
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

      <Card
        title={`Top ${top.length || ""} largest companies`.replace("  ", " ")}
        subtitle={`Ranked by latest stored market capitalisation (${data.currency})${
          top.length && data.companies_with_market_cap !== undefined ? ` · ${top.length} of ${data.companies_with_market_cap} companies with a stored market cap` : ""
        }`}
      >
        {!top.length ? (
          <EmptyState title="No market capitalisation data stored yet">Open the {MARKET_NAMES[market]} page and load market data.</EmptyState>
        ) : (
          <ol className="top-company-list">
            {top.map((t, index) => (
              <li key={t.symbol}>
                <button type="button" className="top-company-row" onClick={() => open(t.symbol)}>
                  <span className="top-rank" aria-label={`Rank ${index + 1}`}>{index + 1}</span>
                  <CompanyMark ticker={t.symbol} name={t.name} size="sm" />
                  <span className="top-company-main">
                    <strong>{t.name}</strong>
                    <span className="small muted">
                      {t.symbol} · {t.sector_label || "sector n/a"}
                    </span>
                  </span>
                  <span className="top-company-values">
                    <strong>{formatMoney(t.market_cap, t.currency)}</strong>
                    <span className="small muted">
                      {formatPerShare(t.price, t.currency)} · {relativeTime(t.quote_fetched_at)}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ol>
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
