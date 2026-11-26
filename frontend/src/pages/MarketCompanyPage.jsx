import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  BarChart3,
  FileText,
  Gauge,
  Layers,
  LineChart as LineIcon,
  Newspaper,
  RefreshCw,
  TrendingUp,
} from "lucide-react";
import {
  Badge,
  Card,
  CompanyMark,
  EmptyState,
  ErrorState,
  EvidencePanel,
  Notice,
  Skeleton,
  Stat,
  Tabs,
} from "../components/ui.jsx";
import { BarChart, LineChart } from "../components/charts.jsx";
import { FundamentalsPanel } from "../components/FundamentalsPanel.jsx";
import { GrowthAnalysisCard } from "../components/GrowthAnalysis.jsx";
import { api } from "../lib/api.js";
import { fiscalLabel, formatDateTime, formatMoney, formatPerShare, moneyScale, relativeTime } from "../lib/format.js";
import { createOpenGuard, lastUpdatedLabel } from "../lib/freshness.js";
import { MARKET_LABELS, formatMetric } from "../lib/sectorModel.js";
import { NewsList, ResearchOptions, ResearchTab } from "./CompanyPage.jsx";

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

/** Shared data hook for one market endpoint (fundamentals/financials/forecasts). */
function useMarketResource(loader, deps) {
  const [state, setState] = useState({ loading: true, error: "", data: null });
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true, error: "", data: null });
    loader(controller.signal)
      .then((data) => setState({ loading: false, error: "", data }))
      .catch((err) => err?.name !== "AbortError" && setState({ loading: false, error: err.message, data: null }));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

function axisFormatter(currency) {
  const { divisor } = moneyScale(currency);
  return (v) => formatMoney(v * divisor, currency, { digits: 0 });
}

function MarketOverviewTab({ d, financials, market }) {
  const quote = d.quote;
  const defs = d.metric_definitions || {};
  const inputs = d.metric_inputs || {};
  const annual = financials?.annual || {};
  const currency = inputs.statement_currency || d.currency;
  const { divisor, unit } = moneyScale(currency);
  const ends = (annual.revenue?.points || []).map((p) => p.period_end).slice(-5);
  const bars = ends.map((end) => ({
    key: end,
    label: fiscalLabel(end, market, "annual"),
    values: [
      { id: "rev", value: (annual.revenue?.points.find((p) => p.period_end === end)?.value ?? NaN) / divisor, tone: "primary", title: "Revenue" },
      { id: "ni", value: (annual.net_income?.points.find((p) => p.period_end === end)?.value ?? NaN) / divisor, tone: "secondary", title: "Net income" },
    ],
  }));
  return (
    <div className="tab-panel">
      {quote ? (
        <div className="stats-row">
          <Stat label="Price" value={formatPerShare(quote.price, quote.currency)} sub={`${quote.provider} · ${relativeTime(quote.fetched_at)}`} />
          {QUOTE_FIELDS.map(([key, label, u]) =>
            key === "market_cap" ? (
              <Stat key={key} label={label} value={formatMoney(quote[key], quote.currency)} />
            ) : (
              <Stat key={key} label={label} value={formatMetric(quote[key] === null ? null : Number(quote[key]), u, quote.currency)} />
            )
          )}
        </div>
      ) : (
        <Notice tone="info">No quote is stored for this company yet.</Notice>
      )}
      <div className="grid-2">
        <Card title="Revenue and net income" subtitle={`Annual, ${unit} · provider-reported statements${market === "IN" ? " · Indian fiscal year April-March" : ""}`}>
          {bars.length ? (
            <BarChart
              data={bars}
              yFormat={axisFormatter(currency)}
              ariaLabel="Annual revenue and net income"
              legend={[
                { id: "rev", label: "Revenue", tone: "primary" },
                { id: "ni", label: "Net income", tone: "secondary" },
              ]}
            />
          ) : (
            <EmptyState title="No annual statements stored yet" />
          )}
        </Card>
        <Card
          title="Key ratios"
          subtitle={inputs.fiscal_period_end ? `${fiscalLabel(inputs.fiscal_period_end, market)} · computed by InsightIQ (${inputs.statement_provider})` : "Computed from stored annual statements"}
        >
          {!inputs.fiscal_period_end ? (
            <EmptyState title="No annual statements stored yet" />
          ) : (
            <div className="stats-row">
              {COMPUTED.map((key) => (
                <Stat key={key} label={defs[key]?.label || key} value={formatMetric(d.metrics?.[key], defs[key]?.unit, d.currency)} />
              ))}
            </div>
          )}
          {inputs.newer_incomplete_period_end && (
            <Notice tone="warning">A newer fiscal year is stored without revenue or net income, so these ratios use the last complete year.</Notice>
          )}
        </Card>
      </div>
      <Card title="Latest company news">
        <NewsList articles={(d.news || []).slice(0, 3)} compact />
      </Card>
    </div>
  );
}

function MarketFinancialsTab({ token, d, financials, market }) {
  const [periodType, setPeriodType] = useState("annual");
  const inputs = d.metric_inputs || {};
  const currency = inputs.statement_currency || d.currency;
  const { divisor, unit } = moneyScale(currency);
  const source = financials?.[periodType] || {};
  const series = [
    ["revenue", "primary"],
    ["net_income", "secondary"],
    ["operating_cash_flow", "forecast"],
  ]
    .filter(([metric]) => source[metric]?.points?.length)
    .map(([metric, tone]) => ({
      id: metric,
      label: source[metric].label,
      tone,
      points: source[metric].points.map((p) => ({ x: p.period_end, y: p.value / divisor, note: p.label })),
    }));
  const periods = Object.keys(d.annual_statements || {});
  return (
    <div className="tab-panel">
      <Card
        title="Reported financials"
        subtitle={`${unit} · ${financials?.currencies?.join(", ") || currency} · provider statements, not converted`}
        action={
          <div className="segmented" role="group" aria-label="Period type">
            {["annual", "quarterly"].map((p) => (
              <button key={p} type="button" className={`btn btn-sm ${periodType === p ? "btn-primary" : "btn-ghost"}`} aria-pressed={periodType === p} onClick={() => setPeriodType(p)}>
                {p === "annual" ? "Annual" : "Quarterly"}
              </button>
            ))}
          </div>
        }
      >
        {series.length ? (
          <LineChart
            series={series}
            yFormat={axisFormatter(currency)}
            xFormat={(x) => fiscalLabel(x, market, periodType)}
            ariaLabel={`${periodType} revenue, net income and operating cash flow`}
            footnote={market === "IN" ? "Indian fiscal year runs April to March (FY2025 = Apr 2024 - Mar 2025)." : "Periods follow the company's fiscal calendar."}
          />
        ) : (
          <EmptyState title={`No ${periodType} statements stored`}>Use “Refresh data” to load the provider's statements.</EmptyState>
        )}
      </Card>
      {d.registered && <GrowthAnalysisCard token={token} ticker={d.symbol} />}
      {periods.length > 0 && (
        <Card title="Annual statements" subtitle={`Provider values in ${currency}, exactly as stored`}>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Item</th>
                  {periods.map((p) => (
                    <th key={p} scope="col" className="num">
                      {fiscalLabel(p, market)}
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
                          {raw === undefined ? "—" : formatMoney(raw, currency)}
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
    </div>
  );
}

function MarketForecastsTab({ forecasts, market, currency }) {
  if (forecasts.loading) return <Card title="Forecasts"><Skeleton lines={6} /></Card>;
  if (forecasts.error) return <ErrorState message={forecasts.error} />;
  const { divisor, unit } = moneyScale(currency);
  return (
    <div className="tab-panel">
      <Notice tone="info" title="How forecasts qualify">
        {forecasts.data.engine}. {forecasts.data.note}
      </Notice>
      {forecasts.data.metrics.map((m) => (
        <Card key={m.metric} title={m.label} subtitle={m.status === "qualified" ? "Forecast qualified" : "Forecast unavailable"}>
          {m.quarterly.length || m.annual.length ? (
            <LineChart
              series={[
                {
                  id: "actual",
                  label: m.quarterly.length >= 2 ? "Reported quarters" : "Reported fiscal years",
                  tone: "primary",
                  points: (m.quarterly.length >= 2 ? m.quarterly : m.annual).map((p) => ({ x: p.period_end, y: p.value / divisor, note: p.label })),
                },
              ]}
              yFormat={(v) => formatMoney(v * divisor, currency, { digits: 0 })}
              xFormat={(x) => fiscalLabel(x, market, m.quarterly.length >= 2 ? "quarterly" : "annual")}
              ariaLabel={`${m.label} reported history (${unit})`}
              footnote="Reported history only. No forecast line is drawn because the checks below did not pass."
            />
          ) : (
            <EmptyState title="No reported history stored for this metric" />
          )}
          {m.reasons.length > 0 && (
            <ul className="small">
              {m.reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          )}
          <EvidencePanel
            label="Qualification checks"
            rows={[
              ["Quarters stored", `${m.checks.quarters_stored}`],
              ["Contiguous quarters", `${m.checks.contiguous_quarters} (validation needs ${m.checks.required_for_validation}; model selection ${m.checks.required_for_model_selection})`],
              ["Filing dates", m.checks.filing_dates_available ? "available" : "not provided by the data provider"],
              ["Currencies", m.checks.currencies.join(", ") || "—"],
              [
                "Annual vs quarterly reconciliation",
                m.checks.reconciliation.length
                  ? m.checks.reconciliation.map((r) => `${fiscalLabel(r.fiscal_period_end, market)}: ${r.status.replace(/_/g, " ")}`).join("; ")
                  : "no annual periods",
              ],
            ]}
          />
        </Card>
      ))}
    </div>
  );
}

export function MarketCompanyPage({
  token,
  market,
  symbol,
  runId,
  tab = "overview",
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
  const [dataVersion, setDataVersion] = useState(0);
  const [researchOptions, setResearchOptions] = useState({ refresh: "if_stale", on_refresh_failure: "continue_with_cached" });
  const registered = Boolean(state.data?.registered);
  const go = useCallback((next) => navigate({ page: "market-company", market, symbol, ...next }), [navigate, market, symbol]);

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
      go({ tab: "research", runId: activeJob.run_id });
    } else if (activeJob.status === "failed") {
      setRunsKey((k) => k + 1);
    }
  }, [activeJob, symbol, go]);

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
        setDataVersion((v) => v + 1);
        setNow(new Date());
      } catch (err) {
        if (err.status === 401) onAuthError?.();
        if (activeKey.current !== key) return;
        setRefresh({ busy: false, info: null, error: err.message });
        setState((s) =>
          s.data ? { ...s, loading: false } : { loading: false, error: err.message, status: err.code === "route_missing" ? 0 : err.status, data: null }
        );
      } finally {
        running.current = false;
      }
    },
    [token, market, symbol, onAuthError]
  );

  useEffect(() => {
    const key = `${market}:${symbol}`;
    activeKey.current = key;
    if (openGuard.current(key)) requestData("auto");
  }, [market, symbol, requestData]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  // Statement series and forecast qualification (stored data only; reloaded after a refresh).
  const financials = useMarketResource((signal) => (registered ? api.marketFinancials(token, market, symbol, signal) : Promise.resolve(null)), [token, market, symbol, registered, dataVersion]);
  const forecasts = useMarketResource((signal) => (registered ? api.marketForecasts(token, market, symbol, signal) : Promise.resolve(null)), [token, market, symbol, registered, dataVersion]);

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
  const currency = d.metric_inputs?.statement_currency || d.currency;
  const secValidated = d.research_profile === "sec_validated";
  const jobHere = activeJob?.ticker === d.symbol && PENDING.includes(activeJob?.status);
  const tabs = [
    { id: "overview", label: "Overview", icon: BarChart3 },
    { id: "financials", label: "Financials", icon: TrendingUp },
    { id: "fundamentals", label: "Fundamentals", icon: Gauge },
    { id: "forecasts", label: "Forecasts", icon: LineIcon },
    { id: "news", label: "News", icon: Newspaper, count: d.news?.length },
    { id: "research", label: "Research", icon: FileText, count: runs?.completed_report_count },
  ];

  return (
    <div className="page">
      {back}
      <header className="company-head card">
        <div className="company-head-top">
          <div className="company-head-title" style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
            <CompanyMark ticker={d.symbol} name={d.name} size="lg" />
            <div>
              <p className="eyebrow">
                {MARKET_LABELS[market]} · {d.exchange || "exchange n/a"}
              </p>
              <h1>
                {d.name} <span className="ticker-chip">{d.symbol}</span>
              </h1>
              <div className="company-head-badges">
                {d.sector_label ? <Badge tone="info">{d.sector_label}</Badge> : <Badge tone="warning">Sector not classified</Badge>}
                {d.provider_industry && <Badge tone="neutral">{d.provider_industry}</Badge>}
                <Badge tone="neutral">{currency}</Badge>
                {market === "IN" && <Badge tone="neutral">FY Apr–Mar</Badge>}
                {secValidated && <Badge tone="positive">SEC-validated research available</Badge>}
              </div>
              <p className="last-updated small muted" aria-live="polite">
                {lastUpdatedLabel(d.last_refreshed_at, now)}
                {refresh.busy ? " · refreshing…" : ""}
              </p>
            </div>
          </div>
          <div className="company-head-actions">
            <button type="button" className="btn btn-ghost" onClick={() => requestData("force")} disabled={refresh.busy} title="Fetch the latest data from the market's providers now">
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
            {secValidated ? (
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
                  go({ tab: "research" });
                }}
              >
                <FileText size={15} aria-hidden="true" />
                {jobHere ? "Research running…" : "Research this company"}
              </button>
            )}
            {!secValidated && <ResearchOptions options={researchOptions} onChange={setResearchOptions} />}
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

      <Tabs tabs={tabs} active={tab} onChange={(id) => go({ tab: id, runId: undefined })} label={`${d.name} sections`} />

      {tab === "overview" && <MarketOverviewTab d={d} financials={financials.data} market={market} />}
      {tab === "financials" && <MarketFinancialsTab token={token} d={d} financials={financials.data} market={market} />}
      {tab === "fundamentals" && <FundamentalsPanel key={dataVersion} token={token} market={market} symbol={d.symbol} onAuthError={onAuthError} />}
      {tab === "forecasts" &&
        (registered ? (
          <MarketForecastsTab forecasts={forecasts} market={market} currency={currency} />
        ) : (
          <EmptyState title="Load the company's data first" />
        ))}
      {tab === "news" && (
        <div className="tab-panel">
          <Card title="Company news" subtitle="Stored articles; publisher reports are not independently verified">
            <NewsList articles={d.news || []} />
          </Card>
        </div>
      )}
      {tab === "research" &&
        (secValidated ? (
          <Card title="Research">
            <p>
              This company has SEC-validated research.{" "}
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => navigate({ page: "company", ticker: d.symbol, tab: "research" })}>
                Open SEC research
              </button>
            </p>
          </Card>
        ) : d.registered ? (
          <section id="research" className="market-research">
            <Card title="AI research" subtitle="Financial, News and Strategic agents on this company's provider data; the summary agent writes the executive summary">
              <p className="small muted">
                Figures come from {market === "US" ? "Financial Modeling Prep" : "Yahoo Finance"} statements in {currency} (not SEC filings)
                {market === "IN" ? ", with Indian fiscal years (April-March)" : ""}. Every number in AI-written text is checked against the stored data before it is shown.
              </p>
            </Card>
            <ResearchTab
              token={token}
              ticker={d.symbol}
              runs={runs}
              runId={runId}
              navigate={(next) => (next?.page === "company" ? go({ tab: "research", runId: next.runId }) : navigate(next))}
              activeJob={activeJob}
              jobError={activeJob?.ticker === d.symbol ? jobError : ""}
              company={null}
              onSummaryChange={() => setRunsKey((k) => k + 1)}
            />
          </section>
        ) : (
          <EmptyState title="Load the company's data first" />
        ))}

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
