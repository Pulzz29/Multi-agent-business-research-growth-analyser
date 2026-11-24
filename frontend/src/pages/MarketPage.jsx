import { useEffect, useRef, useState } from "react";
import { ArrowRight, Building2, Globe2, Layers, RefreshCw, Search } from "lucide-react";
import { Badge, Card, EmptyState, ErrorState, Notice, Skeleton } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { formatDateTime } from "../lib/format.js";
import { MARKET_LABELS, formatMetric } from "../lib/sectorModel.js";

export function MarketToggle({ market, onChange }) {
  return (
    <div className="segmented" role="tablist" aria-label="Market">
      {["US", "IN"].map((m) => (
        <button
          key={m}
          type="button"
          role="tab"
          aria-selected={market === m}
          className={`segmented-item ${market === m ? "is-active" : ""}`}
          onClick={() => onChange(m)}
        >
          <Globe2 size={14} aria-hidden="true" /> {MARKET_LABELS[m]}
        </button>
      ))}
    </div>
  );
}

function useDebounced(value, delay = 250) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

function TopCompanies({ token, market, navigate, onAuthError }) {
  const [state, setState] = useState({ loading: true, error: "", data: null });
  const [loadState, setLoadState] = useState({ busy: false, message: "", tone: "info" });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, error: "", data: null });
    api
      .topCompanies(token, market, controller.signal)
      .then((data) => setState({ loading: false, error: "", data }))
      .catch((err) => {
        if (err?.name === "AbortError") return;
        if (err.status === 401) onAuthError?.();
        setState({ loading: false, error: err.message, data: null });
      });
    return () => controller.abort();
  }, [token, market, reloadKey, onAuthError]);

  async function loadQuotes() {
    const ok = window.confirm(
      `Load the latest market data for the ${MARKET_LABELS[market]} company universe?\n\n` +
        "This calls the market data provider once per company (it can take a minute or two) and respects the provider's rate limits."
    );
    if (!ok) return;
    setLoadState({ busy: true, message: "Loading market data from the provider…", tone: "info" });
    try {
      const result = await api.refreshMarketQuotes(token, market);
      const counts = Object.entries(result.status_counts || {})
        .map(([status, n]) => `${n} ${status.replace(/_/g, " ")}`)
        .join(", ");
      setLoadState({ busy: false, message: `Market data load finished: ${counts}.`, tone: "success" });
      setReloadKey((k) => k + 1);
    } catch (err) {
      if (err.status === 401) onAuthError?.();
      setLoadState({ busy: false, message: `Market data could not be loaded: ${err.message}`, tone: "error" });
    }
  }

  const companies = state.data?.companies || [];
  return (
    <Card
      title={`Largest ${MARKET_LABELS[market]} companies`}
      subtitle="Top 5 by the latest stored market capitalisation"
      action={
        <button type="button" className="btn btn-ghost btn-sm" onClick={loadQuotes} disabled={loadState.busy}>
          <RefreshCw size={14} className={loadState.busy ? "spin" : ""} aria-hidden="true" />
          {loadState.busy ? "Loading…" : "Load market data"}
        </button>
      }
    >
      {loadState.message && <Notice tone={loadState.tone}>{loadState.message}</Notice>}
      {state.loading && <Skeleton lines={3} />}
      {state.error && <ErrorState message={state.error} onRetry={() => setReloadKey((k) => k + 1)} />}
      {!state.loading && !state.error && !companies.length && (
        <EmptyState title="No market data loaded yet" icon={Building2}>
          Load market data to rank the {state.data?.universe_size ?? ""} companies in this market by market capitalisation.
        </EmptyState>
      )}
      {companies.length > 0 && (
        <div className="top-grid">
          {companies.map((c, i) => (
            <button
              key={c.symbol}
              type="button"
              className="top-card"
              onClick={() => navigate({ page: "market-company", market, symbol: c.symbol })}
            >
              <span className="top-rank">#{i + 1}</span>
              <span className="top-name">{c.name}</span>
              <span className="top-meta">
                {c.symbol} · {c.sector_label || "Unclassified"}
              </span>
              <span className="top-cap">{formatMetric(Number(c.market_cap), "currency", c.currency)}</span>
              <span className="top-meta">
                {c.quote_provider} · {formatDateTime(c.quote_fetched_at)}
              </span>
            </button>
          ))}
        </div>
      )}
      {state.data && companies.length > 0 && (
        <p className="small muted">
          Based on {state.data.companies_with_market_cap} of {state.data.universe_size} universe companies with a stored market capitalisation.
        </p>
      )}
    </Card>
  );
}

function CompanySearch({ token, market, navigate, onAuthError }) {
  const [query, setQuery] = useState("");
  const debounced = useDebounced(query.trim());
  const [local, setLocal] = useState({ loading: false, results: [], error: "" });
  const [remote, setRemote] = useState({ busy: false, results: null, error: "" });
  const [opening, setOpening] = useState(null);
  const busy = useRef(false);

  useEffect(() => {
    setRemote({ busy: false, results: null, error: "" });
    if (!debounced) {
      setLocal({ loading: false, results: [], error: "" });
      return undefined;
    }
    const controller = new AbortController();
    setLocal((s) => ({ ...s, loading: true }));
    api
      .searchCompanies(token, market, debounced, controller.signal)
      .then((data) => setLocal({ loading: false, results: data.results || [], error: "" }))
      .catch((err) => {
        if (err?.name === "AbortError") return;
        if (err.status === 401) onAuthError?.();
        setLocal({ loading: false, results: [], error: err.message });
      });
    return () => controller.abort();
  }, [token, market, debounced, onAuthError]);

  async function searchProviders() {
    if (busy.current || !query.trim()) return;
    busy.current = true;
    setRemote({ busy: true, results: null, error: "" });
    try {
      const data = await api.providerSearch(token, market, query.trim());
      setRemote({ busy: false, results: data.results || [], error: "" });
    } catch (err) {
      if (err.status === 401) onAuthError?.();
      setRemote({ busy: false, results: null, error: err.message });
    } finally {
      busy.current = false;
    }
  }

  async function openProviderResult(result) {
    setOpening(result.symbol);
    try {
      // Registers the company and loads its data (server applies the freshness rules).
      await api.companyData(token, market, result.symbol, "auto", result.name || result.symbol);
      navigate({ page: "market-company", market, symbol: result.symbol });
    } catch (err) {
      if (err.status === 401) onAuthError?.();
      setRemote((s) => ({ ...s, error: `Could not load ${result.symbol}: ${err.message}` }));
    } finally {
      setOpening(null);
    }
  }

  return (
    <Card title="Find a company" subtitle={`Search the ${MARKET_LABELS[market]} universe, or look up any listed company at the data provider`}>
      <label className="search-field">
        <Search size={16} aria-hidden="true" />
        <span className="sr-only">Company name or symbol</span>
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={market === "IN" ? "e.g. Infosys, RELIANCE.NS" : "e.g. Apple, JPM"}
          maxLength={60}
        />
      </label>
      {local.error && <Notice tone="error">{local.error}</Notice>}
      {debounced && !local.loading && (
        <ul className="result-list" aria-label="Matching companies">
          {local.results.map((r) => (
            <li key={r.symbol}>
              <button type="button" className="result-item" onClick={() => navigate({ page: "market-company", market, symbol: r.symbol })}>
                <span className="result-name">{r.name}</span>
                <span className="muted small">
                  {r.symbol}
                  {r.sector_label ? ` · ${r.sector_label}` : ""}
                </span>
                <ArrowRight size={14} aria-hidden="true" />
              </button>
            </li>
          ))}
          {!local.results.length && <li className="muted small">No stored or universe company matches “{debounced}”.</li>}
        </ul>
      )}
      {query.trim() && (
        <button type="button" className="btn btn-ghost btn-sm" onClick={searchProviders} disabled={remote.busy}>
          <Search size={14} aria-hidden="true" />
          {remote.busy ? "Searching provider…" : `Search ${market === "US" ? "Finnhub" : "Yahoo Finance"} for “${query.trim()}”`}
        </button>
      )}
      {remote.error && <Notice tone="error">{remote.error}</Notice>}
      {remote.results && (
        <ul className="result-list" aria-label="Provider results">
          {remote.results.map((r) => (
            <li key={r.symbol}>
              <button type="button" className="result-item" onClick={() => openProviderResult(r)} disabled={opening === r.symbol}>
                <span className="result-name">{r.name || r.symbol}</span>
                <span className="muted small">
                  {r.symbol} · {r.exchange || "exchange n/a"}
                </span>
                {opening === r.symbol ? <span className="small muted">Loading…</span> : <Badge tone="info">Add</Badge>}
              </button>
            </li>
          ))}
          {!remote.results.length && <li className="muted small">The provider found no matching listed equity.</li>}
        </ul>
      )}
    </Card>
  );
}

function SectorShortcuts({ token, market, navigate }) {
  const [sectors, setSectors] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    api
      .sectors(token, market, "", controller.signal)
      .then((d) => setSectors(d.sectors || []))
      .catch(() => setSectors([]));
    return () => controller.abort();
  }, [token, market]);
  return (
    <Card
      title="Sector-wise comparison"
      subtitle="Rank companies within a sector on growth, profitability, returns, leverage, valuation and risk"
      action={
        <button type="button" className="btn btn-primary btn-sm" onClick={() => navigate({ page: "sectors", market })}>
          <Layers size={14} aria-hidden="true" /> All sectors
        </button>
      }
    >
      {!sectors ? (
        <Skeleton lines={2} />
      ) : (
        <div className="chip-row">
          {sectors.map((s) => (
            <button key={s.key} type="button" className="chip-button" onClick={() => navigate({ page: "sectors", market, sector: s.key })}>
              {s.label} <span className="muted">({s.company_count})</span>
            </button>
          ))}
        </div>
      )}
    </Card>
  );
}

export function MarketPage({ token, market, navigate, onAuthError }) {
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <p className="eyebrow">Markets</p>
          <h1>{MARKET_LABELS[market]} companies</h1>
          <p className="page-lede">
            {market === "US"
              ? "Quotes and news from Finnhub; financial statements from Financial Modeling Prep."
              : "Quotes and financial statements from Yahoo Finance (yfinance); news from NewsData.io."}
          </p>
        </div>
        <MarketToggle market={market} onChange={(m) => navigate({ page: "market", market: m })} />
      </header>
      <div className="market-grid">
        <TopCompanies token={token} market={market} navigate={navigate} onAuthError={onAuthError} />
        <CompanySearch token={token} market={market} navigate={navigate} onAuthError={onAuthError} />
      </div>
      <SectorShortcuts token={token} market={market} navigate={navigate} />
    </div>
  );
}
