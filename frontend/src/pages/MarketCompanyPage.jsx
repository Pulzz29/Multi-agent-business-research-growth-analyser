import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, FileText, Layers, RefreshCw } from "lucide-react";
import { Badge, Card, EmptyState, ErrorState, EvidencePanel, ExternalLink, Notice, Skeleton, Stat } from "../components/ui.jsx";
import { api } from "../lib/api.js";
import { formatDate, formatDateTime, relativeTime } from "../lib/format.js";
import { createOpenGuard, lastUpdatedLabel } from "../lib/freshness.js";
import { MARKET_LABELS, formatMetric } from "../lib/sectorModel.js";
import { ResearchOptions, ResearchTab } from "./CompanyPage.jsx";
import { GrowthAnalysisCard } from "../components/GrowthAnalysis.jsx";

const PENDING = ["queued", "running"];

const QUOTE_FIELDS = [
  ["market_cap", "Market cap", "currency"],
  ["pe_ratio", "P/E ratio", "x"],
  ["pb_ratio", "Price-to-book", "x"],
  ["dividend_yield_pct", "Dividend yield", "%"],
  ["beta", "Beta", "x"],
];

const STATEMENT_METRICS = [
  ["revenue", "Revenue"],
  ["operating_income", "Operating income"],
  ["net_income", "Net income"],
  ["operating_cash_flow", "Operating cash flow"],
  ["total_assets", "Total assets"],
  ["total_equity", "Total equity"],
  ["total_debt", "Total debt"],
];

const COMPUTED = ["revenue_growth_pct", "net_margin_pct", "operating_margin_pct", "roe_pct", "roa_pct", "debt_to_equity"];

function formatPrice(value, currency) {
  const n = Number(value);
  if (value === null || value === undefined || !Number.isFinite(n)) return "—";
  const symbol = currency === "INR" ? "₹" : currency === "USD" ? "$" : `${currency || ""} `;
  return `${symbol}${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function MarketCompanyPage({
  token,
  market,
  symbol,
  runId,
  showResearch,
  navigate,
  activeJob,
  jobError,
  onStartResearch,
  startBusy,
  onAuthError,
}) {
  const [state, setState] = useState({ loading: true, error: "", status: 0, data: null });
  const [refresh, setRefresh] = useState({ busy: false, info: null, error: "" });
  const running = useRef(false);
  const openGuard = useRef(createOpenGuard());
  const activeKey = useRef(null);
  const [now, setNow] = useState(() => new Date());
  const [runs, setRuns] = useState(null);
  const [runsKey, setRunsKey] = useState(0);
  const [researchOptions, setResearchOptions] = useState({ refresh: "if_stale", on_refresh_failure: "continue_with_cached" });
  const researchRef = useRef(null);
  const registered = Boolean(state.data?.registered);

  // Research history (provider companies use the same job and report storage).
  useEffect(() => {
    if (!registered || state.data?.research_profile !== "provider") return undefined;
    const controller = new AbortController();
    api
      .runs(token, symbol, controller.signal)
      .then(setRuns)
      .catch((err) => {
        if (err?.name !== "AbortError") setRuns({ runs: [], completed_report_count: 0 });
      });
    return () => controller.abort();
  }, [token, symbol, registered, state.data?.research_profile, runsKey]);

  // When this company's research run finishes, show its report.
  const lastJob = useRef(null);
  useEffect(() => {
    if (!activeJob || activeJob.ticker !== symbol) return;
    const key = `${activeJob.run_id}:${activeJob.status}`;
    if (lastJob.current === key) return;
    lastJob.current = key;
    if (activeJob.status === "completed") {
      setRunsKey((k) => k + 1);
      navigate({ page: "company", ticker: symbol, tab: "research", runId: activeJob.run_id });
    } else if (activeJob.status === "failed") {
      setRunsKey((k) => k + 1);
    }
  }, [activeJob, symbol, navigate]);

  useEffect(() => {
    if (showResearch && researchRef.current && !state.loading) {
      researchRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [showResearch, state.loading, runId]);

  // One data request at a time. The server decides whether providers are
  // called ("auto": only when never refreshed or older than the TTL).
  const requestData = useCallback(
    async (mode) => {
      if (running.current) return;
      running.current = true;
      const key = `${market}:${symbol}`;
      setRefresh((r) => ({ ...r, busy: true, error: "" }));
      setState((s) => (s.data ? s : { loading: true, error: "", status: 0, data: null }));
      try {
        const data = await api.companyData(token, market, symbol, mode);
        if (activeKey.current !== key) return;
        setState({ loading: false, error: "", status: 200, data });
        setRefresh({ busy: false, info: data.data_refresh || null, error: "" });
        setNow(new Date());
      } catch (err) {
        if (err.status === 401) onAuthError?.();
        if (activeKey.current !== key) return;
        setRefresh({ busy: false, info: null, error: err.message });
        setState((s) =>
          s.data
            ? { ...s, loading: false }
            : { loading: false, error: err.message, status: err.code === "route_missing" ? 0 : err.status, data: null }
        );
      } finally {
        running.current = false;
      }
    },
    [token, market, symbol, onAuthError]
  );

  // Automatic request when the company is opened (once per company: re-renders
  // and React StrictMode's double effect do not send a second request).
  useEffect(() => {
    const key = `${market}:${symbol}`;
    activeKey.current = key;
    if (openGuard.current(key)) requestData("auto");
  }, [market, symbol, requestData]);

  // Keep the relative "Last updated" text current.
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const back = (
    <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigate({ page: "market", market })}>
      <ArrowLeft size={14} aria-hidden="true" /> {MARKET_LABELS[market]} market
    </button>
  );

  if (state.loading) {
    return (
      <div className="page">
        {back}
        <Card title={symbol} subtitle="Fetching latest data…">
          <p className="small muted" role="status">
            <RefreshCw size={13} className="spin" aria-hidden="true" /> Fetching latest data…
          </p>
          <Skeleton lines={6} />
        </Card>
      </div>
    );
  }
  if (state.error) {
    return (
      <div className="page">
        {back}
        {state.status === 404 ? (
          <EmptyState title={`${symbol} is not in the ${MARKET_LABELS[market]} universe yet`}>
            Use “Find a company” on the market page and search the provider to add it.
          </EmptyState>
        ) : (
          <ErrorState message={state.error} onRetry={() => requestData("auto")} />
        )}
      </div>
    );
  }

  const d = state.data;
  const quote = d.quote;
  const defs = d.metric_definitions || {};
  const periods = Object.keys(d.annual_statements || {});
  const inputs = d.metric_inputs || {};

  return (
    <div className="page">
      {back}
      <header className="company-head card">
        <div className="company-head-top">
          <div className="company-head-title">
            <p className="eyebrow">{MARKET_LABELS[market]} · {d.exchange || "exchange n/a"}</p>
            <h1>
              {d.name} <span className="ticker-chip">{d.symbol}</span>
            </h1>
            <div className="company-head-badges">
              {d.sector_label ? <Badge tone="info">{d.sector_label}</Badge> : <Badge tone="warning">Sector not classified</Badge>}
              {d.provider_industry && <Badge tone="neutral">{d.provider_industry}</Badge>}
              <Badge tone="neutral">{d.currency}</Badge>
              {d.research_profile === "sec_validated" && <Badge tone="positive">SEC-validated research available</Badge>}
            </div>
            <p className="last-updated small muted" aria-live="polite">
              {lastUpdatedLabel(d.last_refreshed_at, now)}
              {refresh.busy ? " · refreshing…" : ""}
            </p>
          </div>
          <div className="company-head-actions">
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => requestData("force")}
              disabled={refresh.busy}
              title="Fetch the latest data from the market's providers now"
            >
              <RefreshCw size={15} className={refresh.busy ? "spin" : ""} aria-hidden="true" />
              {refresh.busy ? "Refreshing…" : "Refresh data"}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={!d.sector_key}
              onClick={() => navigate({ page: "sectors", market, sector: d.sector_key })}
              title={d.sector_key ? "Compare this company with its sector" : "This company has no mapped sector"}
            >
              <Layers size={15} aria-hidden="true" /> Compare with sector
            </button>
            {d.research_profile === "sec_validated" ? (
              <button type="button" className="btn btn-primary" onClick={() => navigate({ page: "company", ticker: d.symbol, tab: "research" })}>
                <FileText size={15} aria-hidden="true" /> Research this company
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary"
                disabled={!d.registered || PENDING.includes(activeJob?.status) || startBusy}
                title={
                  !d.registered
                    ? "Load the company's data first"
                    : PENDING.includes(activeJob?.status)
                      ? `A research run for ${activeJob.ticker} is in progress`
                      : "Run the Financial, News and Strategic agents on this company's provider data (uses Azure OpenAI tokens; can take several minutes)"
                }
                onClick={() => {
                  onStartResearch?.(d.symbol, researchOptions);
                  navigate({ page: "company", ticker: d.symbol, tab: "research" });
                }}
              >
                <FileText size={15} aria-hidden="true" />
                {activeJob?.ticker === d.symbol && PENDING.includes(activeJob?.status) ? "Research running…" : "Research this company"}
              </button>
            )}
          </div>
        </div>
      </header>

      {refresh.error && <Notice tone="error">Refresh failed: {refresh.error} The stored data below is unchanged.</Notice>}
      {refresh.info?.stale && (
        <Notice tone="warning" title="Showing stored data">
          The latest refresh failed ({refresh.info.refresh_error}). {lastUpdatedLabel(d.last_refreshed_at, now)}.
        </Notice>
      )}
      {refresh.info?.decision === "force_cooldown" && <Notice tone="info">{refresh.info.message}</Notice>}
      {refresh.info?.warnings?.length > 0 && <Notice tone="info">{refresh.info.warnings.join(" ")}</Notice>}

      {quote && (
        <div className="stats-row">
          <Stat label="Price" value={formatPrice(quote.price, quote.currency)} sub={`${quote.provider} · ${relativeTime(quote.fetched_at)}`} />
          {QUOTE_FIELDS.map(([key, label, unit]) => (
            <Stat key={key} label={label} value={formatMetric(quote[key] === null ? null : Number(quote[key]), unit, quote.currency)} />
          ))}
        </div>
      )}

      <Card title="Key ratios" subtitle={inputs.fiscal_period_end ? `Computed by InsightIQ from the fiscal year ending ${formatDate(inputs.fiscal_period_end)} (${inputs.statement_provider})` : "Computed from stored annual statements"}>
        {!inputs.fiscal_period_end ? (
          <EmptyState title="No annual statements stored yet" />
        ) : (
          <div className="stats-row">
            {COMPUTED.map((key) => (
              <Stat key={key} label={defs[key]?.label || key} value={formatMetric(d.metrics?.[key], defs[key]?.unit, d.currency)} />
            ))}
          </div>
        )}
        {inputs.roe_basis && (
          <p className="small muted">
            ROE and ROA use {inputs.roe_basis}.{" "}
            {inputs.prior_period_end
              ? `Revenue growth compares with the fiscal year ending ${formatDate(inputs.prior_period_end)}.`
              : "Revenue growth is not shown: no prior fiscal year is stored for comparison."}
          </p>
        )}
        {inputs.newer_incomplete_period_end && (
          <Notice tone="warning">
            A newer fiscal year ending {formatDate(inputs.newer_incomplete_period_end)} is stored without revenue or net
            income, so these ratios use the last complete year.
          </Notice>
        )}
      </Card>

      {d.registered && <GrowthAnalysisCard token={token} ticker={symbol} />}

      {periods.length > 0 && (
        <Card title="Annual statements" subtitle={`Provider values in ${inputs.statement_currency || d.currency}, exactly as stored`}>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Item</th>
                  {periods.map((p) => (
                    <th key={p} scope="col" className="num">
                      FY ending {formatDate(p)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {STATEMENT_METRICS.map(([key, label]) => (
                  <tr key={key}>
                    <th scope="row">{label}</th>
                    {periods.map((p) => {
                      const raw = d.annual_statements[p]?.[key];
                      return (
                        <td key={p} className="num">
                          {raw === undefined ? "—" : formatMetric(Number(raw), "currency", inputs.statement_currency || d.currency)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <Card title="Company news" subtitle="Stored articles; publisher reports are not independently verified">
        {!d.news?.length ? (
          <EmptyState title="No verified company-specific news is available for the selected period.">
            Only articles in which this company is the subject of the headline are shown. Use “Refresh data” to check for new articles.
          </EmptyState>
        ) : (
          <ul className="news-list">
            {d.news.map((n) => (
              <li key={n.evidence_id} className="news-item">
                <ExternalLink href={n.source_url}>{n.title}</ExternalLink>
                <p className="small muted">
                  {n.publisher || "Unknown publisher"} · {formatDateTime(n.published_at)} · via {n.provider}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {d.research_profile === "provider" && d.registered && (
        <section ref={researchRef} id="research" className="market-research">
          <Card
            title="AI research"
            subtitle="Financial, News and Strategic agents on this company's provider data; the summary agent writes the executive summary"
            action={<ResearchOptions options={researchOptions} onChange={setResearchOptions} />}
          >
            <p className="small muted">
              Figures come from {market === "US" ? "Financial Modeling Prep" : "Yahoo Finance"} statements (not SEC filings). Every number in AI-written text is checked against the stored data before it is shown.
            </p>
          </Card>
          <ResearchTab
            token={token}
            ticker={d.symbol}
            runs={runs}
            runId={runId}
            navigate={navigate}
            activeJob={activeJob}
            jobError={activeJob?.ticker === d.symbol ? jobError : ""}
            company={null}
            onSummaryChange={() => setRunsKey((k) => k + 1)}
          />
        </section>
      )}

      <EvidencePanel
        label="Data sources and freshness"
        rows={Object.entries(d.freshness || {}).map(([kind, f]) => [
          `${kind} (${f.source})`,
          f.last_successful_refresh_at ? `${formatDateTime(f.last_successful_refresh_at)} · ${f.state} (policy ${f.policy_max_age_hours} h)` : "never refreshed",
        ])}
      />
    </div>
  );
}
